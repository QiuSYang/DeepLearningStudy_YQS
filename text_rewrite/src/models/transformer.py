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
from src.utils.inference_utils import *

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
      logits = tf.keras.layers.Lambda(
            lambda x: x, name="logits", dtype=tf.float32)(logits)

      model = tf.keras.Model([inputs, segments, masks, targets], logits)

      vocab_size = params["vocab_size"]
      loss = metrics.custom_transformer_loss(logits, targets, vocab_size)
      model.add_loss(loss)

      return model

    else:
      inputs = tf.keras.layers.Input((None,), dtype="int32", name="inputs")
      segments = tf.keras.layers.Input((None,), dtype="int32", name="segments")
      masks = tf.keras.layers.Input((None,), dtype="int32", name="masks")

      internal_model = Transformer(params, name="transformer_v2")
      results = internal_model([inputs, segments, masks], training=is_train)

      if params["is_beam_search"]:
        outputs, scores = results["outputs"], results["scores"]
        return tf.keras.Model([inputs, segments, masks], [outputs, scores])

      return tf.keras.Model([inputs, segments, masks], results)


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
    #                   inputs=None, length=max_decode_length + 1)
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
    if self.params["is_beam_search"]:
      # 使用beam search 方法进行推理
      encoder_outputs = tf.cast(encoder_outputs, self.params["dtype"])
      batch_size = tf.shape(encoder_outputs)[0]
      input_length = tf.shape(encoder_outputs)[1]
      # max_decode_length = input_length + self.params["max_length_target"]
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
    elif self.params["is_custom_beam_search"]:
      # 使用beam search 方法做生成, 参考transformers库tf生成代码(generation_tf_utils.py)
      encoder_outputs = tf.cast(encoder_outputs, self.params["dtype"])
      batch_size = tf.shape(encoder_outputs)[0]
      vocab_size = self.params["vocab_size"]
      pad_token_id = 0

      # 数据扩充到bts*num_beam倍
      num_beams = self.params["beam_size"]
      decoder_ids = tf.zeros([batch_size*num_beams, 1], dtype=tf.int32)  # 初始化start id
      expanded_batch_idxs = tf.reshape(
          tf.repeat(tf.expand_dims(tf.range(batch_size), -1), repeats=num_beams, axis=1),
          shape=(-1,))
      # expand encoder_outputs
      encoder_outputs = tf.gather(encoder_outputs, expanded_batch_idxs, axis=0)
      # expand attention_bias_query
      attention_bias_query = tf.gather(attention_bias_query, expanded_batch_idxs, axis=0)
      # expand attention_bias_content
      attention_bias_content = tf.gather(attention_bias_content, expanded_batch_idxs, axis=0)
      # expand inputs
      inputs = tf.gather(inputs, expanded_batch_idxs, axis=0)

      # generated hypotheses
      max_decode_length = self.params["max_length_target"]
      generated_hyps = [
          BeamHypotheses(num_beams, max_decode_length, self.params["length_penalty"],
                         early_stopping=self.params["early_stopping"]) for _ in range(batch_size)]
      beam_scores = tf.zeros((batch_size, num_beams), dtype=tf.float32)
      beam_scores = tf.reshape(beam_scores, (batch_size * num_beams,))
      # done sentences
      done = [False for _ in range(batch_size)]

      cur_len = shape_list(decoder_ids)[-1]
      while cur_len < max_decode_length:
        decoder_inputs = self.embedding_softmax_layer(decoder_ids)
        decoder_inputs = tf.cast(decoder_inputs, self.params["dtype"])

        length = tf.shape(decoder_inputs)[1]  # 获取当前序列长度
        with tf.name_scope("add_pos_encoding"):
            pos_encoding = model_utils.get_position_encoding(length, self.params["hidden_size"])
            pos_encoding = tf.cast(pos_encoding, self.params["dtype"])
            decoder_inputs += pos_encoding
        if training:
            decoder_inputs = tf.nn.dropout(decoder_inputs, rate=self.params["layer_postprocess_dropout"])

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

        # (batch_size * num_beams, cur_len, vocab_size)
        logits = self.distribute_layer(inputs, D, C_query, C_content, M, encoder_outputs,
                                       attention_bias_query, attention_bias_content)

        next_token_logits = logits[:, -1, :]  # (batch_size * num_beams, vocab_size)

        repetition_penalty = self.params["repetition_penalty"]
        if repetition_penalty != 1.0:
            next_token_logits_penalties = create_next_token_logits_penalties(
                decoder_ids, next_token_logits, repetition_penalty
            )
            next_token_logits = tf.math.multiply(next_token_logits, next_token_logits_penalties)

        temperature = self.params["temperature"]
        if temperature != 1.0:
            next_token_logits = next_token_logits / temperature
        #  calculate log softmax score
        scores = tf.nn.log_softmax(next_token_logits, axis=-1)  # (batch_size * num_beams, vocab_size)

        assert shape_list(scores) == [batch_size * num_beams, vocab_size]
        # Add the log prob of the new beams to the log prob of the beginning of the sequence
        # (sum of logs == log of the product)
        next_scores = scores + tf.broadcast_to(
                  beam_scores[:, None], (batch_size * num_beams, vocab_size))  # (batch_size * num_beams, vocab_size)
        # re-organize to group the beam together (we are keeping top hypothesis across beams)
        next_scores = tf.reshape(
                  next_scores, (batch_size, num_beams * vocab_size))  # (batch_size, num_beams * vocab_size)
        next_scores, next_tokens = tf.math.top_k(next_scores, k=2 * num_beams, sorted=True)
        assert shape_list(next_scores) == shape_list(next_tokens) == [batch_size, 2*num_beams]

        # next batch beam content
        next_batch_beam = []
        # for each sentence
        for batch_idx in range(batch_size):
          # if we are done with this sentence
          if done[batch_idx]:
            assert (len(generated_hyps[batch_idx]) >= num_beams), \
                "Batch can only be done if at least {} beams have been generated".format(num_beams)
            assert (
                    EOS_ID is not None and pad_token_id is not None
            ), "generated beams >= num_beams -> eos_token_id and pad_token have to be defined"
            next_batch_beam.extend([(0, pad_token_id, 0)] * num_beams)  # pad the batch
            continue
          # next sentence beam content
          next_sent_beam = []
          # next tokens for this sentence
          for beam_token_rank, (beam_token_id, beam_token_score) in enumerate(
                  zip(next_tokens[batch_idx], next_scores[batch_idx])):
            # get beam and token IDs
            beam_id = beam_token_id // vocab_size
            token_id = beam_token_id % vocab_size
            effective_beam_id = batch_idx * num_beams + beam_id
            # add to generated hypotheses if end of sentence or last iteration
            if (EOS_ID is not None) and (token_id.numpy() == EOS_ID):
              # if beam_token does not belong to top num_beams tokens, it should not be added
              is_beam_token_worse_than_top_num_beams = beam_token_rank >= num_beams
              if is_beam_token_worse_than_top_num_beams:
                  continue
              generated_hyps[batch_idx].add(
                  tf.identity(decoder_ids[effective_beam_id]), beam_token_score.numpy())
            else:
              # and next predicted token if it is not eos_token
              next_sent_beam.append((beam_token_score, token_id, effective_beam_id))

            # the beam for next step is full
            if len(next_sent_beam) == num_beams:
              break

          # Check if we are done so that we can save a pad step if all(done)
          done[batch_idx] = done[batch_idx] or generated_hyps[batch_idx].is_done(
              tf.reduce_sum(next_scores[batch_idx]).numpy(), cur_len)
          # update next beam content
          assert len(next_sent_beam) == num_beams, "Beam should always be full"
          next_batch_beam.extend(next_sent_beam)
          assert len(next_batch_beam) == num_beams * (batch_idx + 1)
        # stop when we are done with each sentence
        if all(done):
            break

        # sanity check / prepare next batch
        assert len(next_batch_beam) == batch_size * num_beams
        beam_scores = tf.convert_to_tensor([x[0] for x in next_batch_beam], dtype=tf.float32)
        beam_tokens = tf.convert_to_tensor([x[1] for x in next_batch_beam], dtype=tf.int32)
        beam_idx = tf.convert_to_tensor([x[2] for x in next_batch_beam], dtype=tf.int32)

        # re-order batch and update current length
        decoder_ids = tf.stack([tf.identity(decoder_ids[x, :]) for x in beam_idx])
        decoder_ids = tf.concat([decoder_ids, tf.expand_dims(beam_tokens, 1)], axis=-1)
        cur_len = cur_len + 1

      # finalize all open beam hypotheses and end to generated hypotheses
      for batch_idx in range(batch_size):
        # Add all open beam hypothesis to generated_hyps
        if done[batch_idx]:
            continue
        # test that beam scores match previously calculated scores if not eos and batch_idx not done
        if EOS_ID is not None and all(
                (token_id % vocab_size).numpy().item() != EOS_ID for token_id in next_tokens[batch_idx]):
          assert tf.reduce_all(
              next_scores[batch_idx, :num_beams] == tf.reshape(beam_scores, (batch_size, num_beams))[batch_idx]
          ), "If batch_idx is not done, final next scores: {} have to equal to accumulated beam_scores: {}".format(
              next_scores[:, :num_beams][batch_idx], tf.reshape(beam_scores, (batch_size, num_beams))[batch_idx])
        # need to add best num_beams hypotheses to generated hyps
        for beam_id in range(num_beams):
          effective_beam_id = batch_idx * num_beams + beam_id
          final_score = beam_scores[effective_beam_id].numpy().item()
          final_tokens = decoder_ids[effective_beam_id]
          generated_hyps[batch_idx].add(final_tokens, final_score)

      output_batch_size = batch_size
      output_num_return_sequences_per_batch = 1  # 仅输出一条数据
      # select the best hypothese
      sent_lengths_list = []
      best = []

      # retrieve best hypotheses
      for i, hypothese in enumerate(generated_hyps):
        sorted_hyps = sorted(hypothese.beams, key=lambda x: x[0])
        for j in range(output_num_return_sequences_per_batch):
          best_hyp = sorted_hyps.pop()[1]
          sent_lengths_list.append(len(best_hyp))
          best.append(best_hyp)
      assert output_batch_size == len(best), "Output batch size {} must match output bean hypotheses {}".format(
                                              output_batch_size, len(best))
      sent_lengths = tf.convert_to_tensor(sent_lengths_list, dtype=tf.int32)

      # shorter batches are filled with pad_token
      if tf.reduce_min(sent_lengths).numpy() != tf.reduce_max(sent_lengths).numpy():
        sent_max_len = min(tf.reduce_max(sent_lengths).numpy() + 1, max_decode_length)
        decoded_list = []

        # fill with hypothesis and eos_token_id if necessary
        for i, hypo in enumerate(best):
          assert sent_lengths[i] == shape_list(hypo)[0]
          # if sent_length is max_len do not pad
          if sent_lengths[i] == sent_max_len:
            decoded_slice = hypo
          else:
            # else pad to sent_max_len
            num_pad_tokens = sent_max_len - sent_lengths[i]
            padding = pad_token_id * tf.ones((num_pad_tokens, ), dtype=tf.int32)
            decoded_slice = tf.concat([hypo, padding], axis=-1)

            # finish sentence with eos token
            if sent_lengths[i] < max_decode_length:
              decoded_slice = tf.where(
                  tf.range(sent_max_len, dtype=tf.int32) == sent_lengths[i],
                  EOS_ID * tf.ones((sent_max_len, ), dtype=tf.int32),
                  decoded_slice)
          # add to list
          decoded_list.append(decoded_slice)
        decoded = tf.stack(decoded_list)
      else:
        # none of the hypotheses have an eos_token
        assert (len(hypo) == max_decode_length for hypo in best)
        decoded = tf.stack(best)

      return decoded[:, 1:]  # 删除[PAD]起始符
    else:
      # 使用贪婪方法进行推理
      encoder_outputs = tf.cast(encoder_outputs, self.params["dtype"])
      batch_size = tf.shape(encoder_outputs)[0]
      decoder_ids = tf.zeros([batch_size, 1], dtype=tf.int32)  # 初始化start id

      max_decode_length = self.params["max_length_target"]
      for i in range(max_decode_length):
        # 依次生成单个token
        decoder_inputs = self.embedding_softmax_layer(decoder_ids)
        decoder_inputs = tf.cast(decoder_inputs, self.params["dtype"])

        length = tf.shape(decoder_inputs)[1]  # 获取当前序列长度
        with tf.name_scope("add_pos_encoding"):
          pos_encoding = model_utils.get_position_encoding(length, self.params["hidden_size"])
          pos_encoding = tf.cast(pos_encoding, self.params["dtype"])
          decoder_inputs += pos_encoding
        if training:
          decoder_inputs = tf.nn.dropout(decoder_inputs, rate=self.params["layer_postprocess_dropout"])

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

        logits = self.distribute_layer(inputs, D, C_query, C_content, M, encoder_outputs,
                                       attention_bias_query, attention_bias_content)

        next_token_logits = logits[:, -1, :]

        repetition_penalty = self.params["repetition_penalty"]
        if repetition_penalty != 1.0:
            next_token_logits_penalties = create_next_token_logits_penalties(
                decoder_ids, next_token_logits, repetition_penalty
            )
            next_token_logits = tf.math.multiply(next_token_logits, next_token_logits_penalties)

        if self.params["do_sample"]:
          # current_id = tf.math.argmax(tf.nn.softmax(next_token_logits, axis=-1), axis=-1)
          temperature = self.params["temperature"]
          if temperature != 1.0:
            next_token_logits = next_token_logits / temperature
          next_token_logits = tf_top_k_top_p_filtering(next_token_logits,
                                                       top_k=self.params["top_k"],
                                                       top_p=self.params["top_p"])
          current_id = tf.squeeze(tf.random.categorical(tf.nn.softmax(next_token_logits, axis=-1),
                                                        dtype=tf.int32, num_samples=1), axis=-1)
          # current_id = tf.math.argmax(tf.nn.softmax(next_token_logits, axis=-1), axis=-1)
        else:
          current_id = tf.math.argmax(tf.nn.softmax(next_token_logits, axis=-1), axis=-1)

        current_id = tf.cast(current_id, dtype=tf.int32)
        decoder_ids = tf.concat([decoder_ids, tf.expand_dims(current_id, axis=-1)], axis=-1)

      return decoder_ids[:, 1:]  # 删除起始符


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
