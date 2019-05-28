from baselines.common import Dataset, explained_variance, fmt_row, zipsame
from baselines import logger
import os, os.path as osp
import baselines.common.tf_util as U
import tensorflow as tf, numpy as np
import yaml
import time
from baselines.common.mpi_adam import MpiAdam
from baselines.common.mpi_moments import mpi_moments
from mpi4py import MPI
from collections import deque


# GRASPING
SIMPLE_AC = np.array([-0.2, 0.7, 0.7, 1.4])
# /GRASPING

def pretrain(pi, env):
    print("Running {} initialization episodes...".format(env.warm_init_eps), flush=True)
    n_rollouts = env.warm_init_eps
    tf_ob = U.get_placeholder_cached(name="ob")
    ob = env.reset()
    obs = np.array([ob for _ in range(n_rollouts * (env.spec.max_episode_steps + 1))])
    obs_len = 0

    graph = tf.get_default_graph()
    pdparam = graph.get_tensor_by_name("pi/pdparam:0")
    pdparam_shape = pdparam.shape[1].value
    mean, _, logstd, _ = tf.split(pdparam, [len(SIMPLE_AC), pdparam_shape // 2 - len(SIMPLE_AC),
                                            len(SIMPLE_AC), pdparam_shape // 2 - len(SIMPLE_AC)], 1)

    ac_mean = tf.constant(SIMPLE_AC, dtype=tf.float32)
    ac_logstd = tf.constant(np.array([0] * len(SIMPLE_AC)), dtype=tf.float32)

    print("Completed:", flush=True)
    for ep in range(n_rollouts):
        ob = env.reset()
        obs[obs_len] = ob
        obs_len += 1
        done = False
        while not done:
            ac, vpred = pi.act(True, ob)
            ac[:4] = SIMPLE_AC + 0.01 * np.random.randn(4)
            ac[4:] = 0
            ob, _, done, _ = env.step(ac)
            obs[obs_len] = ob
            obs_len += 1
        print(ep + 1, flush=True)

    obs = obs[:obs_len]

    with tf.variable_scope("pretrain"):
        loss = tf.nn.l2_loss(mean - ac_mean) + tf.nn.l2_loss(logstd - ac_logstd)
        opt = tf.train.AdamOptimizer(learning_rate=1e-3).minimize(loss)
        batch_size = 32
        num_epochs = 10
        U.get_session().run(tf.variables_initializer(set(tf.global_variables()) - U.ALREADY_INITIALIZED))
        for ep in range(num_epochs):
            for i in range(len(obs) // batch_size):
                idx = np.random.choice(len(obs), batch_size)
                U.get_session().run([opt, loss], feed_dict={tf_ob: obs[idx]})

    env.n_episodes = 0
    print("Policy initialized!", flush=True)


def traj_segment_generator(pi, env, horizon, stochastic):
    t = 0
    ac = env.action_space.sample()  # not used, just so we have the datatype
    new = True  # marks if we're on first timestep of an episode
    ob = env.reset()

    cur_ep_ret = 0  # return in current episode
    cur_ep_len = 0  # len of current episode
    ep_rets = []  # returns of completed episodes in this segment
    ep_lens = []  # lengths of ...

    # Initialize history arrays
    obs = np.array([ob for _ in range(horizon)])
    rews = np.zeros(horizon, 'float32')
    vpreds = np.zeros(horizon, 'float32')
    news = np.zeros(horizon, 'int32')
    acs = np.array([ac for _ in range(horizon)])
    prevacs = acs.copy()

    while True:
        prevac = ac
        ac, vpred = pi.act(stochastic, ob)
        # Slight weirdness here because we need value function at time T
        # before returning segment [0, T-1] so we get the correct
        # terminal value
        if t > 0 and t % horizon == 0:
            yield {"ob": obs, "rew": rews, "vpred": vpreds, "new": news,
                   "ac": acs, "prevac": prevacs, "nextvpred": vpred * (1 - new),
                   "ep_rets": ep_rets, "ep_lens": ep_lens}
            # Be careful!!! if you change the downstream algorithm to aggregate
            # several of these batches, then be sure to do a deepcopy
            ep_rets = []
            ep_lens = []
        i = t % horizon
        obs[i] = ob
        vpreds[i] = vpred
        news[i] = new
        acs[i] = ac
        prevacs[i] = prevac

        ob, rew, new, _ = env.step(ac)
        rews[i] = rew

        cur_ep_ret += rew
        cur_ep_len += 1
        if new:
            ep_rets.append(cur_ep_ret)
            ep_lens.append(cur_ep_len)
            cur_ep_ret = 0
            cur_ep_len = 0
            ob = env.reset()
        t += 1


def add_vtarg_and_adv(seg, gamma, lam):
    """
    Compute target value using TD(lambda) estimator, and advantage with GAE(lambda)
    """
    new = np.append(
        seg["new"], 0)  # last element is only used for last vtarg, but we already zeroed it if last new = 1
    vpred = np.append(seg["vpred"], seg["nextvpred"])
    T = len(seg["rew"])
    seg["adv"] = gaelam = np.empty(T, 'float32')
    rew = seg["rew"]
    lastgaelam = 0
    for t in reversed(range(T)):
        nonterminal = 1 - new[t + 1]
        delta = rew[t] + gamma * vpred[t + 1] * nonterminal - vpred[t]
        gaelam[t] = lastgaelam = delta + gamma * lam * nonterminal * lastgaelam
    seg["tdlamret"] = seg["adv"] + seg["vpred"]


def learn(env, policy_func, *,
          timesteps_per_batch,  # timesteps per actor per update
          log_every=None,
          log_dir=None,
          episodes_so_far=0, timesteps_so_far=0, iters_so_far=0,
          clip_param, entcoeff,  # clipping parameter epsilon, entropy coeff
          optim_epochs, optim_stepsize, optim_batchsize,  # optimization hypers
          gamma, lam,  # advantage estimation
          max_timesteps=0, max_episodes=0, max_iters=0, max_seconds=0,  # time constraint
          callback=None,  # you can do anything in the callback, since it takes locals(), globals()
          adam_epsilon=1e-5,
          schedule='constant',  # annealing for stepsize parameters (epsilon and adam)
          **kwargs
          ):
    # Setup losses and stuff
    # ----------------------------------------
    ob_space = env.observation_space
    ac_space = env.action_space
    pi = policy_func("pi", ob_space, ac_space)  # Construct network for new policy
    oldpi = policy_func("oldpi", ob_space, ac_space)  # Network for old policy
    # Target advantage function (if applicable)
    atarg = tf.placeholder(dtype=tf.float32, shape=[None])
    ret = tf.placeholder(dtype=tf.float32, shape=[None])  # Empirical return

    # learning rate multiplier, updated with schedule
    lrmult = tf.placeholder(name='lrmult', dtype=tf.float32, shape=[])
    clip_param = clip_param * lrmult  # Annealed cliping parameter epislon

    ob = U.get_placeholder_cached(name="ob")
    ac = pi.pdtype.sample_placeholder([None])

    kloldnew = oldpi.pd.kl(pi.pd)
    ent = pi.pd.entropy()
    meankl = U.mean(kloldnew)
    meanent = U.mean(ent)
    pol_entpen = (-entcoeff) * meanent

    ratio = tf.exp(pi.pd.logp(ac) - oldpi.pd.logp(ac))  # pnew / pold
    surr1 = ratio * atarg  # surrogate from conservative policy iteration
    surr2 = U.clip(ratio, 1.0 - clip_param, 1.0 + clip_param) * atarg
    pol_surr = - U.mean(tf.minimum(surr1, surr2))  # PPO's pessimistic surrogate (L^CLIP)
    vf_loss = U.mean(tf.square(pi.vpred - ret))
    total_loss = pol_surr + pol_entpen + vf_loss
    losses = [pol_surr, pol_entpen, vf_loss, meankl, meanent]
    loss_names = ["pol_surr", "pol_entpen", "vf_loss", "kl", "ent"]

    var_list = pi.get_trainable_variables()
    lossandgrad = U.function([ob, ac, atarg, ret, lrmult], losses +
                             [U.flatgrad(total_loss, var_list)])
    adam = MpiAdam(var_list, epsilon=adam_epsilon)

    assign_old_eq_new = U.function([], [], updates=[tf.assign(oldv, newv)
                                                    for (oldv, newv) in zipsame(oldpi.get_variables(), pi.get_variables())])
    compute_losses = U.function([ob, ac, atarg, ret, lrmult], losses)

    U.initialize()
    adam.sync()

    # Prepare for rollouts
    # ----------------------------------------
    # GRASPING
    saver = tf.train.Saver(var_list=U.ALREADY_INITIALIZED, max_to_keep=1)
    checkpoint = tf.train.latest_checkpoint(log_dir)
    if checkpoint:
        print("Restoring checkpoint: {}".format(checkpoint))
        saver.restore(U.get_session(), checkpoint)
    if hasattr(env, "set_actor"):
        def actor(obs):
            return pi.act(False, obs)[0]
        env.set_actor(actor)
    if not checkpoint and hasattr(env, "warm_init_eps"):
        pretrain(pi, env)
        saver.save(U.get_session(), osp.join(log_dir, "model"))
    # /GRASPING
    seg_gen = traj_segment_generator(pi, env, timesteps_per_batch, stochastic=True)

    tstart = time.time()

    assert sum([max_iters > 0, max_timesteps > 0, max_episodes > 0, max_seconds > 0]
               ) == 1, "Only one time constraint permitted"

    while True:
        if callback:
            callback(locals(), globals())
        should_break = False
        if max_timesteps and timesteps_so_far >= max_timesteps:
            should_break = True
        elif max_episodes and episodes_so_far >= max_episodes:
            should_break = True
        elif max_iters and iters_so_far >= max_iters:
            should_break = True
        elif max_seconds and time.time() - tstart >= max_seconds:
            should_break = True

        if log_every and log_dir:
            if (iters_so_far + 1) % log_every == 0 or should_break:
                # To reduce space, don't specify global step.
                saver.save(U.get_session(), osp.join(log_dir, "model"))

            job_info = {'episodes_so_far': episodes_so_far,
                        'iters_so_far': iters_so_far, 'timesteps_so_far': timesteps_so_far}
            with open(osp.join(log_dir, "job_info_new.yaml"), 'w') as file:
                yaml.dump(job_info, file, default_flow_style=False)
                # Make sure write is instantaneous.
                file.flush()
                os.fsync(file)
            os.rename(osp.join(log_dir, "job_info_new.yaml"), osp.join(log_dir, "job_info.yaml"))

        if should_break:
            break

        if schedule == 'constant':
            cur_lrmult = 1.0
        elif schedule == 'linear':
            cur_lrmult = max(1.0 - float(timesteps_so_far) / max_timesteps, 0)
        else:
            raise NotImplementedError

        logger.log("********** Iteration %i ************" % iters_so_far)

        seg = seg_gen.__next__()
        add_vtarg_and_adv(seg, gamma, lam)

        # ob, ac, atarg, ret, td1ret = map(np.concatenate, (obs, acs, atargs, rets, td1rets))
        ob, ac, atarg, tdlamret = seg["ob"], seg["ac"], seg["adv"], seg["tdlamret"]
        vpredbefore = seg["vpred"]  # predicted value function before udpate
        atarg = (atarg - atarg.mean()) / (atarg.std() + 1e-10)  # standardized advantage function estimate
        d = Dataset(dict(ob=ob, ac=ac, atarg=atarg, vtarg=tdlamret), shuffle=not pi.recurrent)
        optim_batchsize = optim_batchsize or ob.shape[0]

        if hasattr(pi, "ob_rms"):
            pi.ob_rms.update(ob)  # update running mean/std for policy

        assign_old_eq_new()  # set old parameter values to new parameter values
        # logger.log("Optimizing...")
        # logger.log(fmt_row(13, loss_names))
        # Here we do a bunch of optimization epochs over the data
        for _ in range(optim_epochs):
            losses = []  # list of tuples, each of which gives the loss for a minibatch
            for batch in d.iterate_once(optim_batchsize):
                *newlosses, g = lossandgrad(batch["ob"], batch["ac"],
                                            batch["atarg"], batch["vtarg"], cur_lrmult)
                adam.update(g, optim_stepsize * cur_lrmult)
                losses.append(newlosses)
            # logger.log(fmt_row(13, np.mean(losses, axis=0)))

        logger.log("Evaluating losses...")
        losses = []
        for batch in d.iterate_once(optim_batchsize):
            newlosses = compute_losses(batch["ob"], batch["ac"], batch[
                                       "atarg"], batch["vtarg"], cur_lrmult)
            losses.append(newlosses)
        meanlosses, _, _ = mpi_moments(losses, axis=0)
        logger.log(fmt_row(13, meanlosses))
        for (lossval, name) in zipsame(meanlosses, loss_names):
            logger.record_tabular("loss_" + name, lossval)
        logger.record_tabular("ev_tdlam_before", explained_variance(vpredbefore, tdlamret))
        lrlocal = (seg["ep_lens"], seg["ep_rets"])  # local values
        listoflrpairs = MPI.COMM_WORLD.allgather(lrlocal)  # list of tuples
        lens, rews = map(flatten_lists, zip(*listoflrpairs))
        logger.record_tabular("EpLenMean", np.mean(lens))
        logger.record_tabular("EpRewMean", np.mean(rews))
        logger.record_tabular("EpThisIter", len(lens))
        episodes_so_far += len(lens)
        timesteps_so_far += sum(lens)
        iters_so_far += 1
        logger.record_tabular("EpisodesSoFar", episodes_so_far)
        logger.record_tabular("TimestepsSoFar", timesteps_so_far)
        logger.record_tabular("TimeElapsed", time.time() - tstart)
        if MPI.COMM_WORLD.Get_rank() == 0:
            logger.dump_tabular()


def flatten_lists(listoflists):
    return [el for list_ in listoflists for el in list_]
