# coding=utf-8
# Copyright 2020 The Robustness Metrics Authors.
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

# Lint as: python3
"""Models module for Uncertainty Baselines."""

import warnings
from edward2.experimental import sngp
import tensorflow as tf
from uncertainty_baselines.baselines.imagenet import utils


def create(model_dir: str,
           dataset: str,
           method: str = "deterministic",
           use_bfloat16: bool = False,
           **kwargs):
  """Creates the uncertainty baseline.

  Args:
    model_dir: Path to SavedModel.
    dataset: Dataset the model was trained on. This determines what data
      preprocessing it needs to make predictions. Supports cifar, imagenet. If
      not supported, the model uses Robustness Metrics' default preprocessing.
    method: Baseline method. Supports deterministic, batchensemble, dropout,
      multihead, sngp. If not supported, the model defaults to deterministic's
      eval.
    use_bfloat16: Whether the model was trained with bfloat16.
    **kwargs: Additional method-specific keyword arguments.

  Returns:
    Tuple of tf.keras.Model and preprocessing function.
  """
  if method == "dropout":
    num_dropout_samples = kwargs.pop("num_dropout_samples")
  elif method in ("batchensemble", "multihead"):
    ensemble_size = kwargs.pop("ensemble_size")
  elif method == "sngp":
    per_core_batch_size = kwargs.pop("per_core_batch_size")
    gp_mean_field_factor = kwargs.pop("gp_mean_field_factor")

  model = tf.keras.models.load_model(model_dir)

  @tf.function
  def model_fn(features):
    images = features["image"]
    if method == "dropout":
      logits_samples = []
      for _ in range(num_dropout_samples):
        logits = model(images, training=False)
        logits_samples.append(logits)

      logits = tf.stack(logits_samples, axis=0)
      if use_bfloat16:
        logits = tf.cast(logits, tf.float32)

      probs = tf.nn.softmax(logits)
      probs = tf.reduce_mean(probs, axis=0)
    elif method == "sngp":
      logits = model(images)
      if isinstance(logits, tuple):
        logits, covmat = logits
      else:
        covmat = tf.eye(per_core_batch_size)
      if use_bfloat16:
        logits = tf.cast(logits, tf.float32)

      logits = sngp.mean_field_logits(
          logits, covmat, mean_field_factor=gp_mean_field_factor)
      probs = tf.nn.softmax(logits)
    else:
      if method == "batchensemble":
        images = tf.tile(images, [ensemble_size, 1, 1, 1])
      elif method == "multihead":
        images = tf.tile(tf.expand_dims(images, 1), [1, ensemble_size, 1, 1, 1])

      logits = model(images)
      if use_bfloat16:
        logits = tf.cast(logits, tf.float32)

      probs = tf.nn.softmax(logits, axis=-1)
      if method == "batchensemble":
        probs = tf.split(probs, num_or_size_splits=ensemble_size, axis=0)
        probs = tf.reduce_mean(probs, axis=0)
      elif method == "multihead":
        probs = tf.reduce_mean(probs, axis=1)
    return probs

  if dataset == "imagenet":
    def preprocess_fn(features):
      features["image"] = utils.preprocess_for_eval(
          features["image"], use_bfloat16=use_bfloat16)
      return features
  elif dataset == "cifar" and use_bfloat16:
    def preprocess_fn(features):
      dtype = tf.bfloat16
      image = features["image"]
      image = tf.image.convert_image_dtype(image, dtype)
      mean = tf.constant([0.4914, 0.4822, 0.4465], dtype=dtype)
      std = tf.constant([0.2023, 0.1994, 0.2010], dtype=dtype)
      features["image"] = (image - mean) / std
      return features
  else:
    warnings.warn("Dataset is not officially supported. Using default "
                  "preprocessing.")
    preprocess_fn = None

  return model_fn, preprocess_fn