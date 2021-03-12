# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
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
"""Defines the Transformer model in TF 2.0.

Model paper: https://arxiv.org/pdf/1706.03762.pdf
Transformer model code source: https://github.com/tensorflow/tensor2tensor
"""

import tensorflow as tf
from official.nlp.modeling.layers import position_embedding

from src.models import beam_search
# from src.models import beam_search_custom as beam_search
from src.models import model_utils
# from src.models import attention_layer
from src.models import attention_layer_custom as attention_layer
from src.models import embedding_layer
from src.models import ffn_layer
from src.models import metrics
from src.utils.tokenizer import EOS_ID

# Disable the not-callable lint error, since it claims many objects are not
# callable when they actually are.
# pylint: disable=not-callable


def create_model(params, is_train=False):
  """Creates transformer model."""
  with tf.name_scope("model"):
    if is_train:
      inputs = tf.keras.layers.Input((None,), dtype="int32", name="inputs")
      segments = tf.keras.layers.Input((None,), dtype="int32", name="segments")
      masks = tf.keras.layers.Input((None,), dtype="int32", name="masks")
      targets = tf.keras.layers.Input((None,), dtype="int32", name="targets")

      internal_model = Transformer(params, name="transformer_v2")
      logits = internal_model([inputs, segments, masks, targets], training=is_train)

      vocab_size = params["vocab_size"]
      label_smoothing = params["label_smoothing"]

      logits = tf.keras.layers.Lambda(
          lambda x: x, name="logits", dtype=tf.float32)(
              logits)
      model = tf.keras.Model([inputs, segments, masks, targets], logits)
      loss = metrics.custom_transformer_loss(logits, targets, vocab_size)
      model.add_loss(loss)
      return model

    else:
      inputs = tf.keras.layers.Input((None,), dtype="int32", name="inputs")
      segments = tf.keras.layers.Input((None,), dtype="int32", name="segments")
      masks = tf.keras.layers.Input((None,), dtype="int32", name="masks")

      internal_model = Transformer(params, name="transformer_v2")
      ret = internal_model([inputs, segments, masks], training=is_train)

      outputs, scores = ret["outputs"], ret["scores"]
      return tf.keras.Model([inputs, segments, masks], [outputs, scores])


class Transformer(tf.keras.Model):
  """Transformer model with Keras.

  Implemented as described in: https://arxiv.org/pdf/1706.03762.pdf

  The Transformer model consists of an encoder and decoder. The input is an int
  sequence (or a batch of sequences). The encoder produces a continuous
  representation, and the decoder uses the encoder output to generate
  probabilities for the output sequence.
  """

  def __init__(self, params, name=None):
    """Initialize layers to build Transformer model.

    Args:
      params: hyperparameter object defining layer sizes, dropout values, etc.
      name: name of the model.
    """
    super(Transformer, self).__init__(name=name)
    self.params = params
    self.embedding_softmax_layer = embedding_layer.EmbeddingSharedWeights(
        params["vocab_size"], params["hidden_size"])
    self.segment_embedding_layer = embedding_layer.SegmentEmbedding(
        16, hidden_size=params["hidden_size"])
    # self.position_embedding_layer = embedding_layer.PositionEmbedding(
    #     512, hidden_size=params["hidden_size"])
    # self.position_embedding_layer = position_embedding.RelativePositionEmbedding(
    #     hidden_size=self.params["hidden_size"])
    self.encoder_stack = EncoderStack(params)
    self.decoder_stack = DecoderStack(params)
    self.distribute_layer = DistributeLayer(params)

  def get_config(self):
    return {
        "params": self.params,
    }

  def call(self, inputs, training):
    """Calculate target logits or inferred target sequences.

    Args:
      inputs: input tensor list of size 1 or 2.
        First item, inputs: int tensor with shape [batch_size, input_length].
        Second item (optional), targets: None or int tensor with shape
          [batch_size, target_length].
      training: boolean, whether in training mode or not.

    Returns:
      If targets is defined, then return logits for each word in the target
      sequence. float tensor with shape [batch_size, target_length, vocab_size]
      If target is none, then generate output sequence one token at a time.
        returns a dictionary {
          outputs: int tensor with shape [batch_size, decoded_length]
          scores: float tensor with shape [batch_size]}
      Even when float16 is used, the output tensor(s) are always float32.

    Raises:
      NotImplementedError: If try to use padded decode method on CPU/GPUs.
    """
    # inputs = inputs if isinstance(inputs, list) else [inputs]
    if not isinstance(inputs, list):
        raise InterruptedError("missing data.")

    if len(inputs) == 4:
      inputs, segments, masks, targets = inputs[0], inputs[1], inputs[2], inputs[3]
    else:
      # Decoding path.
      inputs, segments, masks, targets = inputs[0], inputs[1], inputs[2], None

    # Variance scaling is used here because it seems to work in many problems.
    # Other reasonable initializers may also work just as well.
    with tf.name_scope("Transformer"):
      # Calculate attention bias for encoder self-attention and decoder
      # multi-headed attention layers.
      attention_bias, attention_bias_query, attention_bias_content = model_utils.get_padding_bias(
                                inputs, masks, padding_value=0)  # mask only visible query token

      # Run the inputs through the encoder layer to map the symbol
      # representations to continuous representations.
      encoder_outputs = self.encode(inputs, segments, attention_bias, training)
      # Generate output sequence if targets is None, or return logits if target
      # sequence is known.
      if targets is None:
        # return {"outputs": tf.zeros((2, 2)), "scores": tf.zeros(2, 2)}
        return self.predict(inputs, encoder_outputs,
                            attention_bias_query, attention_bias_content, training)
      else:
        logits = self.decode(inputs, targets, encoder_outputs,
                             attention_bias_query, attention_bias_content, training)
        return logits

  def encode(self, inputs, segments, attention_bias, training):
    """Generate continuous representation for inputs.

    Args:
      inputs: int tensor with shape [batch_size, input_length].
      segments: int tensor with shape [batch_size, input_length].
      attention_bias: float tensor with shape [batch_size, 1, 1, input_length].
      training: boolean, whether in training mode or not.

    Returns:
      float tensor with shape [batch_size, input_length, hidden_size]
    """
    with tf.name_scope("encode"):
      # Prepare inputs to the layer stack by adding positional encodings and
      # applying dropout.
      encoder_inputs = self.embedding_softmax_layer(inputs)
      encoder_inputs = tf.cast(encoder_inputs, self.params["dtype"])
      inputs_padding = model_utils.get_padding(inputs)
      attention_bias = tf.cast(attention_bias, self.params["dtype"])

      with tf.name_scope("add_segment_encoding"):
          segment_inputs = self.segment_embedding_layer(segments)
          encoder_inputs += segment_inputs

      with tf.name_scope("add_pos_encoding"):
        # pos_encoding = self.position_embedding_layer(inputs=encoder_inputs)
        length = tf.shape(encoder_inputs)[1]
        pos_encoding = model_utils.get_position_encoding(length, self.params["hidden_size"])
        pos_encoding = tf.cast(pos_encoding, self.params["dtype"])
        encoder_inputs += pos_encoding

      if training:
        encoder_inputs = tf.nn.dropout(
            encoder_inputs, rate=self.params["layer_postprocess_dropout"])

      return self.encoder_stack(
          encoder_inputs, attention_bias, inputs_padding, training=training)

  def decode(self,
             inputs,
             targets,
             encoder_outputs,
             # attention_bias,  # cross attention don't used
             attention_bias_query,
             attention_bias_content,
             training):
    """Generate logits for each value in the target sequence.

    Args:
      inputs: target values for the output sequence. int tensor with shape
        [batch_size, content_length] 服务于指针网络
      targets: target values for the output sequence. int tensor with shape
        [batch_size, target_length]
      encoder_outputs: continuous representation of input sequence. float tensor
        with shape [batch_size, input_length, hidden_size]
      attention_bias: float tensor with shape [batch_size, 1, 1, input_length]
      attention_bias_query:
      attention_bias_content:
      training: boolean, whether in training mode or not.

    Returns:
      float32 tensor with shape [batch_size, target_length, vocab_size]
    """
    with tf.name_scope("decode"):
      # Prepare inputs to decoder layers by shifting targets, adding positional
      # encoding and applying dropout.
      with tf.name_scope("shift_targets"):
        # Shift targets to the right, and remove the last element
        targets = tf.pad(targets,
                         [[0, 0], [1, 0]])[:, :-1]  # 先对target头部补0, 再进行embedding encode

      decoder_inputs = self.embedding_softmax_layer(targets)
      decoder_inputs = tf.cast(decoder_inputs, self.params["dtype"])
      # attention_bias = tf.cast(attention_bias, self.params["dtype"])

      # with tf.name_scope("shift_targets"):
      #   # Shift targets to the right, and remove the last element
      #   decoder_inputs = tf.pad(decoder_inputs,
      #                           [[0, 0], [1, 0], [0, 0]])[:, :-1, :]

      length = tf.shape(decoder_inputs)[1]
      with tf.name_scope("add_pos_encoding"):
        # pos_encoding = self.position_embedding_layer(decoder_inputs)
        pos_encoding = model_utils.get_position_encoding(length, self.params["hidden_size"])
        pos_encoding = tf.cast(pos_encoding, self.params["dtype"])
        decoder_inputs += pos_encoding
      if training:
        decoder_inputs = tf.nn.dropout(
            decoder_inputs, rate=self.params["layer_postprocess_dropout"])

      # Run values
      decoder_self_attention_bias = model_utils.get_decoder_self_attention_bias(
          length, dtype=self.params["dtype"])
      D, C_query, C_content, M = self.decoder_stack(
                  decoder_inputs,
                  encoder_outputs,
                  decoder_self_attention_bias,
                  # attention_bias,
                  attention_bias_query,
                  attention_bias_content,
                  training=training)

      # logits = self.embedding_softmax_layer(outputs, mode="linear")
      # logits = tf.cast(logits, tf.float32)

      logits = self.distribute_layer(inputs, D, C_query, C_content, M, encoder_outputs,
                                     attention_bias_query, attention_bias_content)
      return logits

  def _get_symbols_to_logits_fn(self, max_decode_length, training):
    """Returns a decoding function that calculates logits of the next tokens."""
    # timing_signal = self.position_embedding_layer(
    #     inputs=None, length=max_decode_length + 1)
    timing_signal = model_utils.get_position_encoding(max_decode_length + 1, self.params["hidden_size"])
    timing_signal = tf.cast(timing_signal, self.params["dtype"])
    decoder_self_attention_bias = model_utils.get_decoder_self_attention_bias(
        max_decode_length, dtype=self.params["dtype"])

    def symbols_to_logits_fn(ids, i, cache):
      """Generate logits for next potential IDs.

      Args:
        ids: Current decoded sequences. int tensor with shape [batch_size *
          beam_size, i + 1].
        i: Loop index.
        cache: dictionary of values storing the encoder output, encoder-decoder
          attention bias, and previous decoder attention values.

      Returns:
        Tuple of
          (logits with shape [batch_size * beam_size, vocab_size],
           updated cache values)
      """
      # Set decoder input to the last generated IDs
      decoder_input = ids[:, -1:]

      # Preprocess decoder input by getting embeddings and adding timing signal.
      decoder_input = self.embedding_softmax_layer(decoder_input)
      # decoder_input += timing_signal[i]
      decoder_input + timing_signal[i:i + 1]

      self_attention_bias = decoder_self_attention_bias[:, :, i:i + 1, :i + 1]

      # encdec_attention_bias = cache.get("encoder_decoder_attention_bias")
      encdec_attention_bias_query = cache.get("attention_bias_query")
      encdec_attention_bias_content = cache.get("attention_bias_content")
      encoder_outputs = cache.get("encoder_outputs")
      inputs = cache.get("inputs")  # encoder inputs

      D, C_query, C_content, M = self.decoder_stack(
                  decoder_input,
                  encoder_outputs,
                  self_attention_bias,
                  # encdec_attention_bias,  # cross attention don't used
                  encdec_attention_bias_query,
                  encdec_attention_bias_content,
                  training=training,
                  cache=cache)

      # logits = self.embedding_softmax_layer(decoder_outputs, mode="linear")
      # logits = tf.squeeze(logits, axis=[1])

      logits = self.distribute_layer(inputs, D, C_query, C_content, M, encoder_outputs,
                                     encdec_attention_bias_query, encdec_attention_bias_content)
      logits = tf.squeeze(logits, axis=[1])

      return logits, cache

    return symbols_to_logits_fn

  def predict(self,
              inputs,
              encoder_outputs,
              # encoder_decoder_attention_bias,  # cross attention don't used
              attention_bias_query,
              attention_bias_content,
              training):
    """Return predicted sequence."""
    encoder_outputs = tf.cast(encoder_outputs, self.params["dtype"])
    batch_size = tf.shape(encoder_outputs)[0]
    input_length = tf.shape(encoder_outputs)[1]
    # max_decode_length = input_length + self.params["extra_decode_length"]
    max_decode_length = self.params["max_length_target"]
    # encoder_decoder_attention_bias = tf.cast(encoder_decoder_attention_bias,
    #                                          self.params["dtype"])

    symbols_to_logits_fn = self._get_symbols_to_logits_fn(
                                max_decode_length, training)

    # Create initial set of IDs that will be passed into symbols_to_logits_fn.
    initial_ids = tf.zeros([batch_size], dtype=tf.int32)

    # Create cache storing decoder attention values for each layer.
    # num_heads = self.params["num_heads"]
    # dim_per_head = self.params["hidden_size"] // num_heads
    # cache = {
    #     "layer_%d" % layer: {
    #         "k":
    #             tf.zeros(
    #                 [batch_size, 0, num_heads, dim_per_head],
    #                 dtype=self.params["dtype"]),
    #         "v":
    #             tf.zeros(
    #                 [batch_size, 0, num_heads, dim_per_head],
    #                 dtype=self.params["dtype"])
    #     } for layer in range(self.params["num_hidden_layers"])
    # }

    # custom attention layer
    cache = {
        "layer_%d" % layer: {
            "k": tf.zeros([batch_size, 0, self.params["hidden_size"]]),
            "v": tf.zeros([batch_size, 0, self.params["hidden_size"]]),
        } for layer in range(self.params["num_hidden_layers"])}

    # Add encoder output and attention bias to the cache.
    cache["encoder_outputs"] = encoder_outputs
    # cache["encoder_decoder_attention_bias"] = encoder_decoder_attention_bias
    cache["attention_bias_query"] = attention_bias_query
    cache["attention_bias_content"] = attention_bias_content
    cache["inputs"] = inputs

    # Use beam search to find the top beam_size sequences and scores.
    decoded_ids, scores = beam_search.sequence_beam_search(
        symbols_to_logits_fn=symbols_to_logits_fn,
        initial_ids=initial_ids,
        initial_cache=cache,
        vocab_size=self.params["vocab_size"],
        beam_size=self.params["beam_size"],
        alpha=self.params["alpha"],
        max_decode_length=max_decode_length,
        eos_id=EOS_ID)

    # Get the top sequence for each batch element
    top_decoded_ids = decoded_ids[:, 0, 1:]
    top_scores = scores[:, 0]

    return {"outputs": top_decoded_ids, "scores": top_scores}


class PrePostProcessingWrapper(tf.keras.layers.Layer):
  """Wrapper class that applies layer pre-processing and post-processing."""

  def __init__(self, layer, params, residual=True):
    super(PrePostProcessingWrapper, self).__init__()
    self.layer = layer
    self.params = params
    self.residual = residual
    self.postprocess_dropout = params["layer_postprocess_dropout"]

  def build(self, input_shape):
    # Create normalization layer
    self.layer_norm = tf.keras.layers.LayerNormalization(
        epsilon=1e-6, dtype="float32")
    super(PrePostProcessingWrapper, self).build(input_shape)

  def get_config(self):
    return {
        "params": self.params,
    }

  def call(self, x, *args, **kwargs):
    """Calls wrapped layer with same parameters."""
    # Preprocessing: apply layer normalization
    training = kwargs["training"]

    y = self.layer_norm(x)

    # Get layer output
    y = self.layer(y, *args, **kwargs)

    # Postprocessing: apply dropout and residual connection
    if training:
      y = tf.nn.dropout(y, rate=self.postprocess_dropout)
    if not self.residual:
        return y
    return x + y


class EncoderStack(tf.keras.layers.Layer):
  """Transformer encoder stack.

  The encoder stack is made up of N identical layers. Each layer is composed
  of the sublayers:
    1. Self-attention layer
    2. Feedforward network (which is 2 fully-connected layers)
  """

  def __init__(self, params):
    super(EncoderStack, self).__init__()
    self.params = params
    self.layers = []

  def build(self, input_shape):
    """Builds the encoder stack."""
    params = self.params
    for _ in range(params["num_hidden_layers"]):
      # Create sublayers for each layer.
      self_attention_layer = attention_layer.SelfAttention(
          params["hidden_size"], params["num_heads"], params["attention_dropout"])
      feed_forward_network = ffn_layer.FeedForwardNetwork(
          params["hidden_size"], params["filter_size"], params["relu_dropout"])

      self.layers.append([
          PrePostProcessingWrapper(self_attention_layer, params),
          PrePostProcessingWrapper(feed_forward_network, params)
      ])

    # Create final layer normalization layer.
    self.output_normalization = tf.keras.layers.LayerNormalization(
        epsilon=1e-6, dtype="float32")

    super(EncoderStack, self).build(input_shape)

  def get_config(self):
    return {
        "params": self.params,
    }

  def call(self, encoder_inputs, attention_bias, inputs_padding, training):
    """Return the output of the encoder layer stacks.

    Args:
      encoder_inputs: tensor with shape [batch_size, input_length, hidden_size]
      attention_bias: bias for the encoder self-attention layer. [batch_size, 1,
        1, input_length]
      inputs_padding: tensor with shape [batch_size, input_length], inputs with
        zero paddings.
      training: boolean, whether in training mode or not.

    Returns:
      Output of encoder layer stack.
      float32 tensor with shape [batch_size, input_length, hidden_size]
    """
    for n, layer in enumerate(self.layers):
      # Run inputs through the sublayers.
      self_attention_layer = layer[0]
      feed_forward_network = layer[1]

      with tf.name_scope("layer_%d" % n):
        with tf.name_scope("self_attention"):
          encoder_inputs = self_attention_layer(
              encoder_inputs, attention_bias, training=training)
        with tf.name_scope("ffn"):
          encoder_inputs = feed_forward_network(
              encoder_inputs, training=training)

    return self.output_normalization(encoder_inputs)


class DecoderStack(tf.keras.layers.Layer):
  """Transformer decoder stack.

  Like the encoder stack, the decoder stack is made up of N identical layers.
  Each layer is composed of the sublayers:
    1. Self-attention layer
    2. Multi-headed attention layer combining encoder outputs with results from
       the previous self-attention layer.
    3. Feedforward network (2 fully-connected layers)
  """

  def __init__(self, params):
    super(DecoderStack, self).__init__()
    self.params = params
    self.layers = []

  def build(self, input_shape):
    """Builds the decoder stack."""
    params = self.params
    for _ in range(params["num_hidden_layers"]):
      self_attention_layer = attention_layer.SelfAttention(
          params["hidden_size"], params["num_heads"], params["attention_dropout"])
      enc_dec_attention_layer = attention_layer.Attention(
          params["hidden_size"], params["num_heads"], params["attention_dropout"])  # cross attention layer
      feed_forward_network = ffn_layer.FeedForwardNetwork(
          params["hidden_size"], params["filter_size"], params["relu_dropout"])

      self.layers.append([
          PrePostProcessingWrapper(self_attention_layer, params),
          PrePostProcessingWrapper(enc_dec_attention_layer, params),
          PrePostProcessingWrapper(feed_forward_network, params, residual=False)
      ])

    # self.output_normalization = tf.keras.layers.LayerNormalization(
    #     epsilon=1e-6, dtype="float32")  # don't used

    super(DecoderStack, self).build(input_shape)

  def get_config(self):
    return {
        "params": self.params,
    }

  def call(self,
           decoder_inputs,
           encoder_outputs,
           decoder_self_attention_bias,
           # attention_bias,  # this network don't used
           attention_bias_query,
           attention_bias_content,
           training,
           cache=None
           ):
    """Return the output of the decoder layer stacks.

    Args:
      decoder_inputs: A tensor with shape [batch_size, target_length,
        hidden_size].
      encoder_outputs: A tensor with shape [batch_size, input_length,
        hidden_size]
      decoder_self_attention_bias: A tensor with shape [1, 1, target_len,
        target_length], the bias for decoder self-attention layer.
      attention_bias: A tensor with shape [batch_size, 1, 1, input_length], the
        bias for encoder-decoder attention layer.
      attention_bias_query: A tensor with shape [batch_size, 1, 1, input_length], the
        bias for encoder-decoder attention layer(only attention query tokens).
      attention_bias_content: A tensor with shape [batch_size, 1, 1, input_length], the
        bias for encoder-decoder attention layer(only attention content tokens).
      training: A bool, whether in training mode or not.
      cache: (Used for fast decoding) A nested dictionary storing previous
        decoder self-attention values. The items are:
          {layer_n: {"k": A tensor with shape [batch_size, i, key_channels],
                     "v": A tensor with shape [batch_size, i, value_channels]},
                       ...}

    Returns:
      Output of decoder layer stack.
      float32 tensor with shape [batch_size, target_length, hidden_size]
    """
    for n, layer in enumerate(self.layers):
      self_attention_layer = layer[0]
      enc_dec_attention_layer = layer[1]
      feed_forward_network = layer[2]

      # Run inputs through the sublayers.
      layer_name = "layer_%d" % n
      layer_cache = cache[layer_name] if cache is not None else None
      with tf.name_scope(layer_name):
        with tf.name_scope("self_attention"):
          decoder_inputs_M = self_attention_layer(
                      decoder_inputs,
                      decoder_self_attention_bias,
                      training=training,
                      cache=layer_cache)
        with tf.name_scope("encdec_attention"):
          decoder_inputs_query = enc_dec_attention_layer(
                      decoder_inputs_M,
                      encoder_outputs,
                      attention_bias_query,
                      training=training)
          decoder_inputs_content = enc_dec_attention_layer(
                      decoder_inputs_M,
                      encoder_outputs,
                      attention_bias_content,
                      training=training)
          # query and content outputs concat
          decoder_inputs = tf.concat([decoder_inputs_query, decoder_inputs_content], axis=-1)
        with tf.name_scope("ffn"):
          decoder_inputs = feed_forward_network(
              decoder_inputs, training=training)

    # 整理解码端输出内容
    # D = self.output_normalization(decoder_inputs)
    D = decoder_inputs
    C_query = decoder_inputs_query
    C_content = decoder_inputs_content
    M = decoder_inputs_M  # decoder self-attention outputs

    return D, C_query, C_content, M


class DistributeLayer(tf.keras.layers.Layer):
  """Pointer Network implementation"""

  def __init__(self, params):
    super(DistributeLayer, self).__init__()
    self.params = params

    self.hidden_size = params["hidden_size"]
    self.vocab_size = params["vocab_size"]

  def build(self, input_shape):
    """build pointer network layer"""
    params = self.params
    self.lambda_dense = tf.keras.layers.Dense(
        1, activation=tf.nn.sigmoid, name="lambda_dense")
    self.e_dense_layer = tf.keras.layers.Dense(
        params["hidden_size"], use_bias=False, name="e_dense")
    self.m_dense_layer = tf.keras.layers.Dense(
        params["hidden_size"], use_bias=False, name="m_dense")

    super(DistributeLayer, self).build(input_shape)

  def get_config(self):
    return {
        "params": self.params,
    }

  def _self_attention(self, E, M, attention_bias):
    """
    Args:
      E: flaot tensor with shape (bs, max_len_e, hidden_size)
      M: flaot tensor with shape (bs, max_len_m, hidden_size)
      attention_bias: flaot tensor with shape (bs, max_len_e)

    Returns:
      att_weights: flaot tensor with shape (bs, max_len_m, max_len_e)
    """
    E = self.e_dense_layer(E)
    M = self.m_dense_layer(M)

    M *= self.hidden_size ** -0.5

    # Calculate dot product attention
    logits = tf.matmul(M, E, transpose_b=True)  # (bs, len_m, len_e)
    attention_bias = tf.expand_dims(attention_bias, axis=1)  # 维度扩充
    logits += attention_bias  # 对注意力矩阵进行掩码
    att_weights = tf.nn.softmax(logits, name="attention_weights")  # 就算target每个token与编码端输入的每个token的注意力权重

    return att_weights

  def _get_copy_distribution(self, att_weights, encode_inputs):
    """
    Args:
      att_weights: float tensor with shape (bs, max_len_m, max_len_e)
      encode_inputs: float tensor with shape (bs, max_len_c)

    Returns:
      probs: float tensor with shape (bs, max_len_m, vocab_size)
    """
    att_weights_shape = tf.shape(att_weights)
    batch_size = att_weights_shape[0]
    attn_len = att_weights_shape[-1]  # att_weights.get_shape().as_list()[-1]  # max_len

    def _copy_dist(att_weight, encode_input):
      """
      Args:
        att_weight: float tensor with shape (bs*max_len_m, max_len_e)
        encode_input: float tensor with shape (bs*max_len_m, max_len_e)
      """
      batch_size_ = tf.shape(att_weights)[0]
      batch_nums = tf.range(0, batch_size_, dtype="int32")
      batch_nums = tf.expand_dims(batch_nums, axis=1)
      batch_nums = tf.tile(batch_nums, [1, attn_len])

      indices = tf.stack([batch_nums, encode_input], axis=2)  # (bs, max_len, 2)
      shape = batch_size_, self.vocab_size
      updates = att_weight  # (bs*max_len_m, max_len_e)

      probs = tf.scatter_nd(indices, updates, shape)
      # for value in probs[0]:
      #     print(value)

      return probs

    max_len_tgt = att_weights_shape[1]  # tf.shape(att_weights)[1]
    encode_inputs = tf.expand_dims(encode_inputs, axis=1)
    # 每个target token都配备一个encoder的输入序列
    encode_inputs = tf.tile(encode_inputs, [1, max_len_tgt, 1])  # 计算解码端每个token与编码端所以token attention
    encode_inputs = tf.reshape(encode_inputs, shape=[-1, attn_len])
    att_weights = tf.reshape(att_weights, shape=[-1, attn_len])

    probs = _copy_dist(att_weights, encode_inputs)  # (bs*max_len_m, vocab_size)
    probs = tf.reshape(probs, shape=[batch_size, max_len_tgt, -1])

    return probs

  def call(self,
           inputs,                      # origin context + query ids
           D,                           # decoder outputs
           C_query,                     # decoder cross attention query outputs(仅仅与query token计算attention)
           C_content,                   # decoder cross attention context outputs(仅仅与content token计算attention)
           M,                           # decoder self attention outputs
           E,                           # encoder outputs
           attention_bias_query,        # decoder input mask, only query visible
           attention_bias_content,      # decoder input mask, only content visible
           ):
    """
      Args:
        D: float tensor with shape (bs, len_D, hidden_size)
        C_query: float tensor with shape (bs, len_D, hidden_size)
        C_content: float tensor with shape (bs, len_D, hidden_size)
        M: float tensor with shape (bs, len_D, hidden_size)
        E: float tensor with shape (bs, len_C, hidden_size)

        attention_bias_query: float tensor with shape (bs, 1, 1, len_C)
        attention_bias_content: float tensor with shape (bs, 1, 1, len_C)

        inputs: int tensor with shape (bs, max_len_C)

        Returns:
        probs: float tensor with shape (bs, max_len_m, vocab_size)
        """
    with tf.name_scope("distribute_layer"):
      # calc lambda
      lambda_inputs = tf.concat([D, C_query, C_content], axis=-1)
      lamd = self.lambda_dense(lambda_inputs)

      # calc attention
      attention_bias_query = tf.squeeze(attention_bias_query, axis=[1, 2])
      attention_bias_content = tf.squeeze(attention_bias_content, axis=[1, 2])

      # shape = (bs, max_len_D, max_len_E)
      att_weights_query = self._self_attention(E, M, attention_bias_query)  # 计算出decoder端每个token与encoder端每个token的权重
      att_weights_content = self._self_attention(E, M, attention_bias_content)
      # print(att_weights_content[0][0])

      # calc distribution
      probs_query = self._get_copy_distribution(att_weights_query, inputs)
      probs_content = self._get_copy_distribution(att_weights_content, inputs)

      probs = lamd * probs_query + (1.0 - lamd) * probs_content

      return probs
