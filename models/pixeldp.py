# Copyright 2016 The Pixeldp Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
# Based on https://github.com/tensorflow/models/tree/master/research/resnet

from collections import namedtuple

import numpy as np
import tensorflow as tf
import six
import math
import utils

from tensorflow.python.training import moving_averages

class Model(object):
    """Pixel model base class."""

    def __init__(self, hps, images, labels, mode):
        """Model constructor.

        Args:
          hps: Hyperparameters.
          images: Batches of images. [batch_size, image_size, image_size, 3]
          labels: Batches of labels. [batch_size, num_classes]
          mode: One of 'train' and 'eval'.
        """
        self.hps    = hps
        self.mode   = mode
        self.images = images
        self.labels = labels

        # Differential Privacy parameters
        self._image_size = self.hps.image_size

        # Book keeping for the noise layer
        self._sensitivities   = [1]
        # Extra book keeping for Parseval
        self._parseval_convs  = []
        self._parseval_ws     = []
        self._extra_train_ops = []

    def build_graph(self):
        """Build a whole graph for the model."""
        self.global_step = tf.contrib.framework.get_or_create_global_step()
        self._build_model()
        if self.mode == 'train':
            self._build_train_op()
        self.summaries = tf.summary.merge_all()

    def pre_noise_sensitivity(self):
        return tf.reduce_prod(self._sensitivities)

    def _stride_arr(self, stride):
        """Map a stride scalar to the stride array for tf.nn.conv2d."""
        return [1, stride, stride, 1]

    def _dp_mult(self, sensitivity_norm, output_dim=None):
        dp_eps = self.hps.dp_epsilon
        dp_del = self.hps.dp_delta
        if sensitivity_norm == 'l2':
            # Use the Gaussian mechanism
            return self.hps.attack_norm_bound *  \
                   math.sqrt(2 * math.log(1.25 / dp_del)) / dp_eps
        elif sensitivity_norm == 'l1':
            # Use the Laplace mechanism
            return self.hps.attack_norm_bound / dp_eps
        else:
            return 0

    def _build_parseval_update_ops(self):
          beta = 0.001

          ops  = []
          for kernel in self._parseval_convs:
              #  shape=[3, 3, 3, 16]
              shape = kernel.get_shape().as_list()

              w_t        = tf.reshape(kernel, [-1, shape[-1]])
              w          = tf.transpose(w_t)
              parseval_k = (1 + beta) * w - beta * tf.matmul(w, tf.matmul(w_t, w))

              op = tf.assign(kernel,
                             tf.reshape(tf.transpose(parseval_k), shape),
                             validate_shape=True)

              ops.append(op)

          for w_t in self._parseval_ws:
              w = tf.transpose(w_t)
              parseval_w = (1 + beta) * w - beta * tf.matmul(w, tf.matmul(w_t, w))
              op = tf.assign(w_t, tf.transpose(parseval_w), validate_shape=True)
              ops.append(op)

          return ops

    def _build_train_op(self):
        """Build training specific ops for the graph."""
        self.lrn_rate = tf.constant(self.hps.lrn_rate, tf.float32)
        tf.summary.scalar('learning_rate', self.lrn_rate)

        trainable_variables = tf.trainable_variables()
        grads = tf.gradients(self.cost, trainable_variables)

        if self.hps.optimizer == 'sgd':
            optimizer = tf.train.GradientDescentOptimizer(self.lrn_rate)
        elif self.hps.optimizer == 'mom':
            optimizer = tf.train.MomentumOptimizer(self.lrn_rate, 0.9)

        apply_op = optimizer.apply_gradients(
            zip(grads, trainable_variables),
            global_step=self.global_step, name='train_step')

        train_ops = [apply_op] + self._extra_train_ops

        previous_ops = [tf.group(*train_ops)]

        if len(self._parseval_convs) + len(self._parseval_ws) > 0:
            # Parseval
            with tf.control_dependencies(previous_ops):
                parseval_update = tf.group(*self._build_parseval_update_ops())
                previous_ops    = [parseval_update]

        with tf.control_dependencies(previous_ops):
            self.train_op = tf.no_op(name='train')

    def _decay(self):
      """L2 weight decay loss."""
      costs = []
      for var in tf.trainable_variables():
        if var.op.name.find(r'DW') > 0:
          costs.append(tf.nn.l2_loss(var))
          # tf.summary.histogram(var.op.name, var)

      return tf.multiply(self.hps.weight_decay_rate, tf.add_n(costs))

    def _conv(self, name, x, filter_size, in_filters, out_filters, strides, is_pre_noise=False,
              layer_sensivity_bound=None, sensitivity_control_scheme=None):
        """Convolution, with support for sensitivity bounds when they are
        pre-noise."""

        assert(strides[1] == strides[2])
        stride = strides[1]

        with tf.variable_scope(name):
            n = filter_size * filter_size * out_filters
            kernel = tf.get_variable(
                'DW', [filter_size, filter_size, in_filters, out_filters],
                tf.float32, initializer=tf.random_normal_initializer(
                    stddev=np.sqrt(2.0/n)))

            if is_pre_noise:
                return tf.nn.conv2d(x, kernel, strides, padding='SAME')
            elif layer_sensivity_bound == 'l2_l2':
                # Parseval projection, see: https://arxiv.org/abs/1704.08847
                self._parseval_convs.append(kernel)
                sensitivity_rescaling = math.ceil(filter_size / stride)
                k = kernel / sensitivity_rescaling

                if sensitivity_control_scheme == 'optimize':
                    raise ValueError("Cannot optimize sensitivity for l2_l2.")
                elif sensitivity_control_scheme == 'bound':
                    # Compute the sensitivity and keep it.
                    # Use kernel as we compensate to the reshapre by using k in
                    # the conv2d.
                    shape     = kernel.get_shape().as_list()
                    w_t       = tf.reshape(kernel, [-1, shape[-1]])
                    w         = tf.transpose(w_t)
                    sing_vals = tf.svd(w, compute_uv=False)
                    self._sensitivities.append(tf.reduce_max(sing_vals))

                    return tf.nn.conv2d(x, k, strides, padding='SAME')
            elif layer_sensivity_bound == 'l1_l2':
                if sensitivity_control_scheme == 'optimize':
                    k = kernel
                elif sensitivity_control_scheme == 'bound':
                    # Sensitivity 1 by L2 normalization
                    k = tf.nn.l2_normalize(kernel, dim=[0, 1, 3])

                # Compute the sensitivity
                sqr_sum  = tf.reduce_sum(tf.square(k), [0, 1, 3], keep_dims=True)
                l2_norms = tf.sqrt(sqr_sum)
                self._sensitivities.append(tf.reduce_max(l2_norms))

                return tf.nn.conv2d(x, k, strides, padding='SAME')
            elif layer_sensivity_bound == 'l1_l1':

                if sensitivity_control_scheme == 'optimize':
                    k = kernel
                elif sensitivity_control_scheme == 'bound':
                    # Sensitivity 1 by L1 normalization
                    k = utils.l1_normalize(kernel, dim=[0, 1, 3])

                # Compute the sensitivity
                l1_norms = tf.reduce_sum(tf.abs(k), [0, 1, 3], keep_dims=True)
                self._sensitivities.append(tf.reduce_max(l1_norms))

                return tf.nn.conv2d(x, k, strides, padding='SAME')
            else:
                raise ValueError("Pre-noise with unsupported sensitivity.")


    def _noise_layer(self, x, sensitivity_norm, sensitivity_control_scheme):
        """Pixeldp noise layer."""
        # This is a factor applied to the noise layer,
        # used to rampup the noise at the beginning of training.
        self.noise_scale = tf.placeholder(tf.float32, shape=(),
                                          name='noise_scale')

        dp_mult = self._dp_mult(sensitivity_norm)
        if sensitivity_control_scheme == 'optimize':
            sensitivity = tf.reduce_prod(self._sensitivities)
        elif sensitivity_control_scheme == 'bound':
            sensitivity = 1

        noise_scale = self.noise_scale * dp_mult * sensitivity
        if sensitivity_norm == 'l1':
            laplace_shape = tf.shape(x)
            loc           = tf.zeros(laplace_shape, dtype=tf.float32)
            scale         = tf.ones(laplace_shape,  dtype=tf.float32)
            noise         = tf.distributions.Laplace(loc, scale).sample()
            noise         = noise_scale * noise
        elif sensitivity_norm == 'l2':
            noise = tf.random_normal(tf.shape(x), mean=0, stddev=1)
            noise = noise_scale * noise

        return x + noise

    def _maybe_add_noise_layer(self, x, sensitivity_norm, sensitivity_control_scheme, position):
        if position == self.hps.noise_after_n_layers:
            return self._noise_layer(x, sensitivity_norm, sensitivity_control_scheme)
        else:
            return x

    def _relu(self, x, leakiness=0.0):
        """Relu, with optional leaky support."""
        return tf.where(tf.less(x, 0.0), leakiness * x, x, name='leaky_relu')

    def _fully_connected(self, x, out_dim, sensivity_control=None):
      """FullyConnected layer."""
      x = tf.reshape(x, [self.hps.batch_size * self.hps.n_draws, -1])
      w = tf.get_variable(
          'DW', [x.get_shape()[1], out_dim],
          initializer=tf.uniform_unit_scaling_initializer(factor=1.0))
      b = tf.get_variable('biases', [out_dim],
                          initializer=tf.constant_initializer())
      return tf.nn.xw_plus_b(x, w, b)

