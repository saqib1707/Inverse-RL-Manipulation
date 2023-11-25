import argparse
import collections
import functools
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone
import pytz
from tqdm import tqdm
import pdb
import copy

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
# os.environ['MUJOCO_GL'] = 'egl'

import numpy as np
import torch
import tensorflow as tf
import tensorflow.keras.mixed_precision as prec
tf.get_logger().setLevel('ERROR')
from tensorflow_probability import distributions as tfd

import models
import tools
import wrappers

utc_dt = datetime.now(timezone.utc).astimezone(pytz.timezone('US/Pacific'))


def define_config():
  config = tools.AttrDict()

  # General.
  config.basedir = 'logs/log_'+utc_dt.strftime('%Y%m%d_%H%M%S')
  config.logdir = pathlib.Path(config.basedir+'/logdir')
  config.model_datadir = pathlib.Path(config.basedir+'/model_data')   # what is model data directory for ?
  config.policy_datadir = pathlib.Path(config.basedir+'/policy_data')  # what is policy data directory for ?
  config.expert_datadir = pathlib.Path('.expert')

  config.seed = 1
  config.steps = 1000000
  config.eval_every = 1000
  config.log_every = 100
  config.log_scalars = True
  config.log_images = True
  config.gpu_growth = True
  config.precision = 32

  # Environment.
  config.env = 'robosuite'
  config.task = 'Lift'
  config.camera_names = 'agentview'
  config.num_envs = 1

  config.use_camera_obs = True
  config.use_depth_obs = False
  config.use_object_obs = True
  config.use_proprio_obs = True
  config.use_touch_obs = True
  config.use_tactile_obs = False
  config.use_shape_obs = True

  config.parallel = 'none'
  config.action_repeat = 1
  config.time_limit = 1000
  config.horizon = 15
  config.prefill = 1000
  config.eval_noise = 0.0
  config.clip_rewards = 'none'

  # Model.
  config.deter_size = 200
  config.stoch_size = 30
  config.num_units = 400
  config.dense_act = 'elu'
  config.cnn_act = 'relu'
  config.cnn_depth = 32 if config.use_depth_obs == False else 32
  config.pcont = False    # what is pcont ??
  config.free_nats = 3.0  # what is free nats ??
  config.alpha = 1.0
  config.kl_scale = 1.0
  config.pcont_scale = 10.0
  config.weight_decay = 0.0
  config.weight_decay_pattern = r'.*'

  # Dense Encoder for Proprioceptive observations
  config.hidden_units = 32
  config.out_units = 32

  # Training.
  config.batch_size = 128
  config.batch_length = 50
  config.train_every = 1000
  config.train_steps = 200
  config.pretrain = 100
  config.model_lr = 6e-4
  config.discriminator_lr = 8e-5
  config.value_lr = 8e-5
  config.actor_lr = 8e-5
  config.grad_clip = 100.0
  config.dataset_balance = False
  config.store = True

  # Behavior.
  config.discount = 0.99
  config.disclam = 0.95
  config.action_dist = 'tanh_normal'
  config.action_init_std = 5.0

  # Exploration parameters
  config.expl = 'additive_gaussian'    # exploration algorithm/type
  config.expl_amount = 0.3    # exploration amount
  config.expl_decay = 0.0    # exploration decay
  config.expl_min = 0.0     # exploration minimum

  return config


class VMAIL(tools.Module):
  def __init__(self, config, model_datadir, policy_datadir, expert_datadir, actspace, writer):
    self._c = config
    self._camview_rgb = self._c.camera_names + "_image"
    self._camview_depth = self._c.camera_names + "_depth"
    self._actspace = actspace
    self._actdim = actspace.n if hasattr(actspace, 'n') else actspace.shape[0]  # 7
    self._writer = writer
    self._random = np.random.RandomState(config.seed)
    
    with tf.device('cpu:0'):
      self._step = tf.Variable(count_steps(policy_datadir, config), dtype=tf.int64)
    
    self._should_pretrain = tools.Once()
    self._should_train = tools.Every(config.train_every)
    self._should_log = tools.Every(config.log_every)
    self._last_log = None
    self._last_time = time.time()
    self._metrics = collections.defaultdict(tf.metrics.Mean)
    self._metrics['expl_amount']  # Create variable for checkpoint.
    self._float = prec.global_policy().compute_dtype
    self._strategy = tf.distribute.MirroredStrategy()
    
    with self._strategy.scope():
      # create tensorflow python distributed iterator objects
      self._model_dataset = iter(self._strategy.experimental_distribute_dataset(load_dataset(model_datadir, self._c)))  # tensorflow.python.distribute.input_lib.DistributedIterator object
      self._expert_dataset = iter(self._strategy.experimental_distribute_dataset(load_dataset(expert_datadir, self._c)))  # tensorflow.python.distribute.input_lib.DistributedIterator object
      self._build_model()


  def __call__(self, obs, reset, state=None, training=True):
    step = self._step.numpy().item()
    tf.summary.experimental.set_step(step)
    
    if state is not None and reset.any():
      mask = tf.cast(1 - reset, self._float)[:, None]
      state = tf.nest.map_structure(lambda x: x * mask, state)
    
    if self._should_train(step):
      log = self._should_log(step)
      n = self._c.pretrain if self._should_pretrain() else self._c.train_steps
      
      print(f'Training for {n} steps.')
      with self._strategy.scope():
        for train_step in range(n):
          log_images = self._c.log_images and log and train_step == 0
          self.train(next(self._model_dataset), next(self._expert_dataset), log_images)
      if log:
        self._write_summaries()
    
    action, state = self.policy(obs, state, training)
    if training:
      self._step.assign_add(len(reset) * self._c.action_repeat)
    
    return action, state


  @tf.function
  def policy(self, obs, state, training:bool=True):
    if state is None:
      latent = self._dynamics_model.initialize(batch_size=len(obs[self._camview_rgb]))
      action = tf.zeros((len(obs[self._camview_rgb]), self._actdim), self._float)
    else:
      latent, action = state
    
    embed = self._encode(preprocess(obs, self._c))   # [128,50,1024]
    if self._c.use_proprio_obs == True:
      embed_proprio = self._encode_proprio(obs)        # [128,50,32]
      embed = tf.concat([embed, embed_proprio], axis=-1)   # [128,50,1056]
    
    latent, _ = self._dynamics_model.obs_step(latent, action, embed)  # returns (posterior, prior) dictionaries
    feat = self._dynamics_model.get_feat(latent)
    
    if training:
      action = self._actor(feat).sample()
    else:
      action = self._actor(feat).mode()
    
    action = self._exploration(action, training)
    state = (latent, action)
    
    return action, state
  

  def load_shape_modules(self):
    sys.path.append("/home/saqibcephsharedvol2/ERLab/IRL_Project/SceneGrasp/")
    from common.utils.nocs_utils import load_depth
    from common.utils.misc_utils import (
        convert_realsense_rgb_depth_to_o3d_pcl,
        get_o3d_pcd_from_np,
        get_scene_grasp_model_params,
    )
    from common.utils.scene_grasp_utils import (
        SceneGraspModel,
        get_final_grasps_from_predictions_np,
        get_grasp_vis,
    )
    print("Importing Scenegrasp")
  

  def estimate_object_shape(self):
    """
      Estimates the 3d point cloud shape of each object in the scene. 

      Returns a list
    """
    all_gripper_vis = []
    for pred_idx in range(pred_dp.get_len()):
        (
            pred_grasp_poses_cam_final,
            pred_grasp_widths,
            _,
        ) = get_final_grasps_from_predictions_np(
            pred_dp.scale_matrices[pred_idx][0, 0],
            pred_dp.endpoints,
            pred_idx,
            pred_dp.pose_matrices[pred_idx],
            TOP_K=TOP_K,
        )

        grasp_colors = np.ones((len(pred_grasp_widths), 3)) * [1, 0, 0]
        all_gripper_vis += [
            get_grasp_vis(
                pred_grasp_poses_cam_final, pred_grasp_widths, grasp_colors
            )
        ]

    pred_pcls = pred_dp.get_camera_frame_pcls()
    pred_pcls_o3d = []
    for pred_pcl in pred_pcls:
        pred_pcls_o3d.append(get_o3d_pcd_from_np(pred_pcl))    # convert numpy pcd to o3d pcd
    o3d_pcl = convert_realsense_rgb_depth_to_o3d_pcl(rgb, depth / 1000, camera_k)
    print(">Showing predicted shapes:")
    print("stage:0", o3d_pcl)
    print("stage:1", pred_pcls_o3d)
    print("stage:2", all_gripper_vis)

    return o3d_pcl, pred_pcls_o3d


  def load(self, filename):
    super().load(filename)
    self._should_pretrain()


  @tf.function()
  def train(self, model_data, expert_data, log_images=False):
    self._strategy.run(self._train, args=(model_data, expert_data, log_images))


  def _train(self, model_data, expert_data, log_images):
    """
      model_data: environment buffer trajectory samples (observation, action, next observation)
      expert_data: expert trajectory samples (observation, action)
    """

    with tf.GradientTape() as model_tape:
      # compute embedded features for images sampled from policy
      embed = self._encode(model_data)          # [128, 50, 1024]
      if self._c.use_proprio_obs:
        embed_proprio = self._encode_proprio(model_data)   # [128, 50, 32]
        embed = tf.concat([embed, embed_proprio], axis=-1)  # [128,50,1056]
      
      if self._c.use_shape_obs:
        self.load_shape_modules()
        # embed_shape = self.estimate_object_shape()
      
      post, prior = self._dynamics_model.observe(embed=embed, action=model_data['action'])
      feat = self._dynamics_model.get_feat(post)
      image_pred = self._decode(feat)     # tfp.distributions.Independent("IndependentNormal", batch_shape=[128, 50], event_shape=[84, 84, 3/4], dtype=float32)
      
      likes = tools.AttrDict()
      if self._c.use_depth_obs == True:
        decoder_gt = tf.concat([model_data[self._camview_rgb], model_data[self._camview_depth]], axis=-1)
      else:
        decoder_gt = model_data[self._camview_rgb]
      likes.image = tf.reduce_mean(image_pred.log_prob(decoder_gt))
      
      if self._c.pcont:   # False
        pcont_pred = self._pcont(feat)
        pcont_target = self._c.discount * model_data['discount']
        likes.pcont = tf.reduce_mean(pcont_pred.log_prob(pcont_target))
        likes.pcont *= self._c.pcont_scale
      
      # estimate prior and posterior transition dynamics distribution
      prior_dist = self._dynamics_model.get_distribution(prior)
      post_dist = self._dynamics_model.get_distribution(post)

      # compute divergence
      div = tf.reduce_mean(tfd.kl_divergence(post_dist, prior_dist))  # scalar divergence tensor
      div = tf.maximum(div, self._c.free_nats)
      
      model_loss = self._c.kl_scale * div - sum(likes.values())  # Eq.7 in vmail paper
      model_loss /= float(self._strategy.num_replicas_in_sync)
      
    with tf.GradientTape(persistent=True) as agent_tape:
      imag_feat, actions = self._imagine_ahead(post)
      
      # compute embedded features for images from expert data
      embed_expert = self._encode(expert_data)                   # [128,50,1024]
      if self._c.use_proprio_obs == True:
        embed_expert_proprio = self._encode_proprio(expert_data)   # [128,50,32]
        embed_expert = tf.concat([embed_expert, embed_expert_proprio], axis=-1)   # [128,50,1056]
      
      post_expert, prior_expert = self._dynamics_model.observe(embed=embed_expert, action=expert_data['action'])
      feat_expert = self._dynamics_model.get_feat(post_expert)
     
      feat_expert_dist = tf.concat([feat_expert[:, :-1], expert_data['action'][:, 1:]], axis = -1)  # [128,49,237]
      feat_policy_dist = tf.concat([imag_feat[:-1], actions], axis = -1)

      expert_d, _ = self._discriminator(feat_expert_dist)
      policy_d, _ = self._discriminator(feat_policy_dist)
      
      expert_loss = tf.reduce_mean(expert_d.log_prob(tf.ones_like(expert_d.mean())))
      policy_loss = tf.reduce_mean(policy_d.log_prob(tf.zeros_like(policy_d.mean())))
      
      with tf.GradientTape() as penalty_tape:
        alpha = tf.expand_dims(tf.random.uniform(feat_policy_dist.shape[:2]), -1)
        temp1 = tf.expand_dims(flatten(feat_expert_dist), 0)    # [1,6272,237]
        temp2 = tf.tile(temp1, [self._c.horizon, 1, 1])        # [horizon,6272,237]
        # print("Let's see:", feat_expert_dist.shape, temp1.shape, temp2.shape)

        disc_penalty_input = alpha * feat_policy_dist + (1.0 - alpha) * temp2
        _, logits = self._discriminator(disc_penalty_input)
        discriminator_variables = tf.nest.flatten([self._discriminator.variables])
        inner_discriminator_grads = penalty_tape.gradient(tf.reduce_mean(logits), discriminator_variables)
        inner_discriminator_norm = tf.linalg.global_norm(inner_discriminator_grads)
        grad_penalty = (inner_discriminator_norm - 1)**2

      discriminator_loss = -(expert_loss + policy_loss) + self._c.alpha * grad_penalty
      discriminator_loss /= float(self._strategy.num_replicas_in_sync)

      reward = policy_d.mean()
      if self._c.pcont:
        pcont = self._pcont(imag_feat[1:]).mean()
      else:
        pcont = self._c.discount * tf.ones_like(reward)
      value = self._value(imag_feat[1:]).mode()

      returns = tools.lambda_return(reward[:-1], value[:-1], pcont[:-1], bootstrap=None, lambda_ = 1.0, axis=0)
      
      discount = tf.stop_gradient(tf.math.cumprod(tf.concat([tf.ones_like(pcont[:1]), pcont[:-2]], 0), 0))
      actor_loss = -tf.reduce_mean(discount * returns)
      actor_loss /= float(self._strategy.num_replicas_in_sync)

    with tf.GradientTape() as value_tape:
      value_pred = self._value(imag_feat[1:])[:-1]
      target = tf.stop_gradient(returns)
      value_loss = -tf.reduce_mean(discount * value_pred.log_prob(target))
      value_loss /= float(self._strategy.num_replicas_in_sync)

    model_norm = self._model_opt(model_tape, model_loss)
    discriminator_norm = self._discriminator_opt(agent_tape, discriminator_loss)
    actor_norm = self._actor_opt(agent_tape, actor_loss)
    value_norm = self._value_opt(value_tape, value_loss)

    if tf.distribute.get_replica_context().replica_id_in_sync_group == 0:
      if self._c.log_scalars:
        self._scalar_summaries(model_data, feat, prior_dist, post_dist, likes, div, model_loss, tf.reduce_mean(expert_d.mean()), tf.reduce_mean(policy_d.mean()), tf.reduce_max(tf.reduce_mean(policy_d.mean(), axis = 1)), expert_loss, policy_loss, grad_penalty, discriminator_loss, tf.reduce_mean(reward), value_loss, actor_loss, model_norm, discriminator_norm, value_norm, actor_norm)
      if tf.equal(log_images, True):
        # print("summary:", model_data, embed, image_pred)
        self._image_summaries(model_data, embed, image_pred)


  def _build_model(self):
    acts = dict(elu=tf.nn.elu, relu=tf.nn.relu, swish=tf.nn.swish, leaky_relu=tf.nn.leaky_relu)
    cnn_act = acts[self._c.cnn_act]
    act = acts[self._c.dense_act]

    self._encode = models.ConvEncoder(self._c.cnn_depth, cnn_act, camview_rgb=self._camview_rgb, camview_depth=self._camview_depth, use_depth_obs=self._c.use_depth_obs)

    if self._c.use_proprio_obs == True:
      self._encode_proprio = models.DenseEncoder(self._c.out_units, num_layers=0, hidden_units=self._c.hidden_units, activation=act, camview_rgb=self._camview_rgb)

    self._dynamics_model = models.RSSM(self._c.stoch_size, self._c.deter_size, self._c.deter_size)
    self._decode = models.ConvDecoder(self._c.cnn_depth, cnn_act, use_depth_obs=self._c.use_depth_obs)
    self._reward = models.DenseDecoder((), 2, self._c.num_units, act=act)
    self._discriminator = models.DenseDecoder((), 2, self._c.num_units, 'binary', act=act)
    self._value = models.DenseDecoder((), 3, self._c.num_units, act=act)
    self._actor = models.ActionDecoder(self._actdim, 4, self._c.num_units, self._c.action_dist, init_std=self._c.action_init_std, act=act)
    
    model_modules = [self._encode, self._dynamics_model, self._decode, self._reward]
    if self._c.use_proprio_obs == True:
      model_modules.append(self._encode_proprio)

    if self._c.pcont:
      self._pcont = models.DenseDecoder((), 3, self._c.num_units, 'binary', act=act)
      model_modules.append(self._pcont)
    
    Optimizer = functools.partial(tools.Adam, wd=self._c.weight_decay, clip=self._c.grad_clip, wdpattern=self._c.weight_decay_pattern)
    self._model_opt = Optimizer('model', model_modules, self._c.model_lr)
    self._discriminator_opt = Optimizer('discriminator', [self._discriminator], self._c.discriminator_lr)
    self._value_opt = Optimizer('value', [self._value], self._c.value_lr)
    self._actor_opt = Optimizer('actor', [self._actor], self._c.actor_lr)
    
  
  def _exploration(self, action, training:bool=True):
    if training:
      amount = self._c.expl_amount
      if self._c.expl_decay:   # False
        amount *= 0.5 ** (tf.cast(self._step, tf.float32) / self._c.expl_decay)
      if self._c.expl_min:    # False
        amount = tf.maximum(self._c.expl_min, amount)
      self._metrics['expl_amount'].update_state(amount)
    elif self._c.eval_noise:
      amount = self._c.eval_noise
    else:
      return action
    
    if self._c.expl == 'additive_gaussian':
      return tf.clip_by_value(tfd.Normal(action, amount).sample(), -1, 1)
    elif self._c.expl == 'completely_random':
      return tf.random.uniform(action.shape, -1, 1)
    elif self._c.expl == 'epsilon_greedy':
      indices = tfd.Categorical(0 * action).sample()
      return tf.where(tf.random.uniform(action.shape[:1], 0, 1) < amount, tf.one_hot(indices, action.shape[-1], dtype=self._float), action)
    
    raise NotImplementedError(self._c.expl)

  
  def _imagine_ahead(self, post):
    ''' 
      this function takes as input a [128,50] batch of posterior distribution of latent state and 
      using this as start state, uses current actor policy to sample action, and using this action and state uses transition dynamics to get the next state. returns the states and actions.
    '''
    # post = {mean:[128,50,30], std:[128,50,30], stoch:[128,50,30], deter:[128,50,200]}
    post = {k: v[:, :-1] for k, v in post.items()}  # exclude the last element (why ??)
    # post = {mean:[128,49,30], std:[128,49,30], stoch:[128,49,30], deter:[128,49,200]}
    
    flatten = lambda x: tf.reshape(x, [-1] + list(x.shape[2:]))
    start_state = {k: flatten(v) for k, v in post.items()}   # {mean:[6272,30], std:[6272,30], stoch:[6272,30], deter:[6272,200]}

    policy = lambda state: self._actor(tf.stop_gradient(self._dynamics_model.get_feat(state))).sample()
    
    last_state = start_state
    # print("tf.nest.flatten:", tf.nest.flatten(start_state), tf.nest.flatten(last_state))
    # tf.nest.flatten(start_state) = [tf.tensor (6272,200), tf.tensor (6272,30), tf.tensor (6272,30), tf.tensor (6272,30)]
    outputs = [[] for _ in tf.nest.flatten(start_state)]
    [o.append(l) for o, l in zip(outputs, tf.nest.flatten(last_state))]
    # print("before:", outputs)
    actions = []
    
    for index in range(self._c.horizon):  # why horizon=15 ??
      action = policy(last_state)
      last_state = self._dynamics_model.img_step(last_state, action)   # given current state `last_state` and `action`, find next state using learned transition dynamics
      [o.append(l) for o, l in zip(outputs, tf.nest.flatten(last_state))]
      actions.append(action)
    
    # print("length of outputs:", len(outputs))
    outputs = [tf.stack(x, 0) for x in outputs]  # [tensor(16,6272,200), tensor(16,6272,30), tensor(16,6272,30), tensor(16,6272,30)]
    actions = tf.stack(actions, 0)   # tensor(15, 6272, 7)
    # print("actions:", actions)
    states = tf.nest.pack_sequence_as(start_state, outputs)  # {mean:tensor(16,6272,30), std:tensor(16,6272,30), stoch:tensor(16,6272,30), deter:tensor(16,6272,200)}
    # print("states:", states)
    imag_feat = self._dynamics_model.get_feat(states)  # tensor(16, 6272, 30+200=230)
    # print("image features:", imag_feat)
    return imag_feat, actions

  
  def _scalar_summaries(self, data, feat, prior_dist, post_dist, likes, div, model_loss, expert_d, policy_d, max_policy_d, expert_loss, policy_loss, grad_penalty, discriminator_loss, rewards, value_loss, actor_loss, model_norm, discriminator_norm, value_norm, actor_norm):
    self._metrics['model_grad_norm'].update_state(model_norm)
    self._metrics['discriminator_norm'].update_state(discriminator_norm)
    self._metrics['value_grad_norm'].update_state(value_norm)
    self._metrics['actor_grad_norm'].update_state(actor_norm)
    self._metrics['prior_ent'].update_state(prior_dist.entropy())
    self._metrics['post_ent'].update_state(post_dist.entropy())
    self._metrics['expert_d'].update_state(expert_d)
    self._metrics['policy_d'].update_state(policy_d)
    self._metrics['max_policy_d'].update_state(max_policy_d)
    self._metrics['rewards'].update_state(rewards)
    for name, logprob in likes.items():
      self._metrics[name + '_loss'].update_state(-logprob)
    self._metrics['div'].update_state(div)
    self._metrics['model_loss'].update_state(model_loss)
    self._metrics['expert_loss'].update_state(expert_loss)
    self._metrics['policy_loss'].update_state(policy_loss)
    self._metrics['discriminator_loss'].update_state(discriminator_loss)
    self._metrics['discriminator_penalty'].update_state(grad_penalty)
    self._metrics['value_loss'].update_state(value_loss)
    self._metrics['actor_loss'].update_state(actor_loss)
    self._metrics['action_ent'].update_state(self._actor(feat).entropy())

  
  def _image_summaries(self, data, embed, image_pred):
    # print("inside image summaries:", data[self._camview_rgb].shape, data[self._camview_depth].shape, image_pred.mode().shape)
    if self._c.use_depth_obs == True:
      truth = tf.concat([data[self._camview_rgb][:6] + 0.5, data[self._camview_depth][:6] + 0.5], axis=-1)   # [6,50,84,84,4]
    else:
      truth = data[self._camview_rgb][:6] + 0.5       # [6,50,84,84,3]
    
    recon = image_pred.mode()[:6]   # [6,50,84,84,3/4]
    init, _ = self._dynamics_model.observe(embed[:6, :5], data['action'][:6, :5])
    init = {k: v[:, -1] for k, v in init.items()}
    
    prior = self._dynamics_model.imagine(data['action'][:6, 5:], init)
    openl = self._decode(self._dynamics_model.get_feat(prior)).mode()
    model = tf.concat([recon[:, :5] + 0.5, openl + 0.5], 1)
    error = (model - truth + 1) / 2
    openl = tf.concat([truth, model, error], 2)
    tools.graph_summary(self._writer, tools.video_summary, 'agent/openl', openl)

  
  def _write_summaries(self):
    step = int(self._step.numpy())
    metrics = [(k, float(v.result())) for k, v in self._metrics.items()]
    
    if self._last_log is not None:
      duration = time.time() - self._last_time
      self._last_time += duration
      metrics.append(('fps', (step - self._last_log) / duration))
    
    self._last_log = step
    [m.reset_states() for m in self._metrics.values()]
    
    with (self._c.logdir / 'metrics.jsonl').open('a') as f:
      f.write(json.dumps({'step': step, **dict(metrics)}) + '\n')
    
    [tf.summary.scalar('agent/' + k, m) for k, m in metrics]
    print(f'[{step}]', ' / '.join(f'{k} {v:.1f}' for k, v in metrics))
    self._writer.flush()


def flatten(x):
  '''
    if x.shape = [a, b, c, d, ...]
    then y.shape = [a * b, c, d, ...]
  '''
  y = tf.reshape(x, [-1] + list(x.shape[2:]))
  # print("inside flatten:", x.shape, y.shape)
  return y


def preprocess(obs, config):
  dtype = prec.global_policy().compute_dtype  # float32
  obs = obs.copy()
  with tf.device('cpu:0'):
    rgb_img_name = config.camera_names+'_image'
    obs[rgb_img_name] = tf.cast(obs[rgb_img_name], dtype) / 255.0 - 0.5
    if config.use_depth_obs == True:
      depth_img_name = config.camera_names + '_depth'
      obs[depth_img_name] = tf.cast(obs[depth_img_name] - 0.5, dtype)
    clip_rewards = dict(none=lambda x: x, tanh=tf.tanh)[config.clip_rewards]
    obs['reward'] = clip_rewards(obs['reward'])
    for k, v in obs.items():
      obs[k] = tf.cast(v, dtype)
  return obs


def count_steps(datadir, config):
  return tools.count_episodes(datadir)[1] * config.action_repeat


def load_dataset(directory, config):
  # print("This should be expert directory:", directory)
  episode = next(tools.load_episodes(directory, 1, config=config))
  # print("Episode:", episode)
  # for k, v in episode.items():
  #   # print(k, ":", v.shape)
  #   if v.dtype == 'float64':
  #     episode[k] = v.astype('float32')
  
  types = {k: v.dtype for k, v in episode.items()}
  shapes = {k: (None,) + v.shape[1:] for k, v in episode.items()}
  generator = lambda: tools.load_episodes(directory, config.train_steps, config.batch_length, config.dataset_balance, config=config)
  dataset = tf.data.Dataset.from_generator(generator, types, shapes)
  dataset = dataset.batch(config.batch_size, drop_remainder=True)
  dataset = dataset.map(functools.partial(preprocess, config=config))
  dataset = dataset.prefetch(10)
  return dataset


def summarize_episode(episode, config, datadir, writer, prefix):
  episodes, steps = tools.count_episodes(datadir)
  length = (len(episode['reward']) - 1) * config.action_repeat
  ret = episode['reward'].sum()
  print(f'{prefix.title()} episode of length {length} with return {ret:.1f}.')
  
  metrics = [(f'{prefix}/return', float(episode['reward'].sum())), (f'{prefix}/length', len(episode['reward']) - 1), (f'episodes', episodes)]
  step = count_steps(datadir, config)
  
  with (config.logdir / 'metrics.jsonl').open('a') as f:
    f.write(json.dumps(dict([('step', step)] + metrics)) + '\n')
  
  with writer.as_default():  # Env might run in a different thread.
    tf.summary.experimental.set_step(step)
    [tf.summary.scalar('sim/' + k, v) for k, v in metrics]
    if prefix == 'test':
      tools.video_summary(f'sim/{prefix}/video', episode[config.camera_names+'_image'][None])


def make_env(config, writer, prefix, model_datadir, policy_datadir, store):
  if config.env == 'dmc':
    env = wrappers.DeepMindControl(config.task)
    env = wrappers.ActionRepeat(env, config.action_repeat)
    env = wrappers.NormalizeActions(env)
  elif config.env == 'atari':
    env = wrappers.Atari(config.task, config.action_repeat, (64, 64), grayscale=False, life_done=True, sticky_actions=True)
    env = wrappers.OneHotAction(env)
  elif config.env == 'robosuite':
    env = wrappers.RobosuiteTask(task=config.task, 
                                horizon=config.time_limit, 
                                camview=config.camera_names, 
                                use_camera_obs=config.use_camera_obs, 
                                use_depth_obs=config.use_depth_obs, 
                                use_object_obs=config.use_object_obs, 
                                use_touch_obs=config.use_touch_obs, 
                                use_tactile_obs=config.use_tactile_obs
                              )
    env = wrappers.ActionRepeat(env, config.action_repeat)
    env = wrappers.NormalizeActions(env)
  else:
    raise NotImplementedError(config.env)
  
  env = wrappers.TimeLimit(env, config.time_limit / config.action_repeat)
  callbacks = []
  if store:
    callbacks.append(lambda ep: tools.save_episodes(model_datadir, [ep]))
    callbacks.append(lambda ep: tools.save_episodes(policy_datadir, [ep]))
  callbacks.append(lambda ep: summarize_episode(ep, config, policy_datadir, writer, prefix))
  
  env = wrappers.Collect(env, callbacks, config.precision)
  env = wrappers.RewardObs(env)
  return env


def main(config):
  device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
  if device.type == 'cuda':
    print("Number of GPU devices:", torch.cuda.device_count())
    print("GPU device name:", torch.cuda.get_device_name(0))
    print('Allocated memory:', round(torch.cuda.memory_allocated(0)/1024**3, 3), 'GB')
    print('Cached memory:   ', round(torch.cuda.memory_reserved(0)/1024**3, 3), 'GB')
  else:
    print("Device:", device)

  if config.gpu_growth:
    print("TF GPUs:", tf.config.experimental.list_physical_devices('GPU'))
    for gpu in tf.config.experimental.list_physical_devices('GPU'):
      tf.config.experimental.set_memory_growth(gpu, True)
  
  assert config.precision in (16, 32), config.precision
  if config.precision == 16:
    prec.set_policy(prec.Policy('mixed_float16'))
  config.steps = int(config.steps)

  os.makedirs(config.basedir, exist_ok=True)
  config.logdir.mkdir(parents=True, exist_ok=True)
  config.model_datadir.mkdir(parents=True, exist_ok=True)
  config.policy_datadir.mkdir(parents=True, exist_ok=True)
  config.expert_datadir.mkdir(parents=True, exist_ok=True)

  new_config = copy.deepcopy(config)
  new_config.logdir = str(new_config.logdir)
  new_config.model_datadir = str(new_config.model_datadir)
  new_config.policy_datadir = str(new_config.policy_datadir)
  new_config.expert_datadir = str(new_config.expert_datadir)
  with open(os.path.join(new_config.logdir, 'args.json'), 'w') as f:
    json.dump(vars(new_config), f, sort_keys=True, indent=4)

  # copy expert data to another directory "model_datadir"
  from distutils.dir_util import copy_tree
  copy_tree(str(config.expert_datadir), str(config.model_datadir))

  # Create environments.
  model_datadir = config.model_datadir
  policy_datadir = config.policy_datadir
  expert_datadir = config.expert_datadir
  writer = tf.summary.create_file_writer(str(config.logdir), max_queue=1000, flush_millis=20000)
  writer.set_as_default()
  train_envs = [wrappers.Async(lambda: make_env(
      config, writer, 'train', model_datadir, policy_datadir, store=config.store), config.parallel) for _ in range(config.num_envs)]
  test_envs = [wrappers.Async(lambda: make_env(
      config, writer, 'test', model_datadir, policy_datadir, store=False), config.parallel) for _ in range(config.num_envs)]
  actspace = train_envs[0].action_space

  # Prefill dataset with random episodes.
  step = count_steps(model_datadir, config)
  # print("What is step?", config.prefill - step)
  prefill = max(0, config.prefill - step)
  print(f'Prefill dataset with {prefill} steps.')
  random_agent = lambda o, d, _: ([actspace.sample() for _ in d], None)
  tools.simulate(random_agent, train_envs, prefill / config.action_repeat)
  writer.flush()

  # Train and regularly evaluate the agent.
  step = count_steps(policy_datadir, config)
  print(f'Simulating agent for {config.steps-step} steps.')
  agent = VMAIL(config, model_datadir, policy_datadir, expert_datadir, actspace, writer)

  if (config.logdir / 'variables.pkl').exists():
    print('Load checkpoint.')
    agent.load(config.logdir / 'variables.pkl')

  state = None
  print("Starting Training...")
  while step < config.steps:
    print('{}/{}, Start evaluation.'.format(step, config.steps))
    tools.simulate(functools.partial(agent, training=False), test_envs, episodes=1)
    writer.flush()
    print('Start collection.')
    steps = config.eval_every // config.action_repeat
    state = tools.simulate(agent, train_envs, steps, state=state)
    step = count_steps(policy_datadir, config)
    agent.save(config.logdir / 'variables.pkl')
  
  for env in train_envs + test_envs:
    env.close()


if __name__ == '__main__':
  try:
    import colored_traceback
    colored_traceback.add_hook()
  except ImportError:
    pass
  parser = argparse.ArgumentParser()
  for key, value in define_config().items():
    parser.add_argument(f'--{key}', type=tools.args_type(value), default=value)
  main(parser.parse_args())
