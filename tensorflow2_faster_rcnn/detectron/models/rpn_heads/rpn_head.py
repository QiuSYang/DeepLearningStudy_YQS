"""
# rpn network
"""
import os
import logging
import tensorflow as tf
from detectron.utils.misc import *
from detectron.core.anchor import anchor_generator, anchor_target
from detectron.core.bbox import transforms
from detectron.core.loss import losses

layers = tf.keras.layers


class RPNHead(tf.keras.Model):
    def __init__(self,
                 anchor_scales=(32, 64, 128, 256, 512),
                 anchor_ratios=(0.5, 1, 2),
                 anchor_feature_strides=(4, 8, 16, 32, 64),
                 proposal_count=2000,
                 nms_threshold=0.7,
                 target_means=(0., 0., 0., 0.),
                 target_stds=(0.1, 0.1, 0.2, 0.2),
                 num_rpn_deltas=256,
                 positive_fraction=0.5,
                 pos_iou_thr=0.7,
                 neg_iou_thr=0.3,
                 **kwags):
        """Network head of Region Proposal Network.

                                      / - rpn_cls (1x1 conv)
        input - rpn_conv (3x3 conv) -
                                      \ - rpn_reg (1x1 conv)

        Attributes
        ---
            anchor_scales: 1D array of anchor sizes in pixels.
            anchor_ratios: 1D array of anchor ratios of width/height.
            anchor_feature_strides: Stride of the feature map relative
                to the image in pixels.
            proposal_count: int. RPN proposals kept after non-maximum
                supression.
            nms_threshold: float. Non-maximum suppression threshold to
                filter RPN proposals.
            target_means: [4] Bounding box refinement mean.
            target_stds: [4] Bounding box refinement standard deviation.
            num_rpn_deltas: int.
            positive_fraction: float.
            pos_iou_thr: float.
            neg_iou_thr: float.
        """
        super(RPNHead, self).__init__(**kwags)

        self.proposal_count = proposal_count
        self.nms_threshold = nms_threshold
        self.target_means = target_means
        self.target_stds = target_stds

        # Shared convolutional base of the RPN
        self.rpn_conv_shared = layers.Conv2D(512, (3, 3), padding='same',
                                             kernel_initializer='he_normal',
                                             name='rpn_conv_shared')

        self.rpn_class_raw = layers.Conv2D(len(anchor_ratios) * 2, (1, 1),
                                           kernel_initializer='he_normal',
                                           name='rpn_class_raw')

        self.rpn_delta_pred = layers.Conv2D(len(anchor_ratios) * 4, (1, 1),
                                            kernel_initializer='he_normal',
                                            name='rpn_bbox_pred')

        self.generator = anchor_generator.AnchorGenerator(
            scales=anchor_scales,
            ratios=anchor_ratios,
            feature_strides=anchor_feature_strides)

        self.anchor_target = anchor_target.AnchorTarget(
            target_means=target_means,
            target_stds=target_stds,
            num_rpn_deltas=num_rpn_deltas,
            positive_fraction=positive_fraction,
            pos_iou_thr=pos_iou_thr,
            neg_iou_thr=neg_iou_thr)

        self.rpn_class_loss = losses.RPNClassLoss()
        self.rpn_bbox_loss = losses.RPNBBoxLoss()

    def __call__(self, inputs, training=True):
        """
        Args
        ---
            inputs: [batch_size, feat_map_height, feat_map_width, channels]
                one level of pyramid feat-maps.

        Returns
        ---
            rpn_class_logits: [batch_size, num_anchors, 2]
            rpn_probs: [batch_size, num_anchors, 2]
            rpn_deltas: [batch_size, num_anchors, 4]
        """
        layer_outputs = []
        for feat in inputs:
            # share convolution layer 3X3
            shared = self.rpn_conv_shared(feat)
            shared = tf.nn.relu(shared)

            # classify branch
            x = self.rpn_class_raw(shared)
            # reshape convolution tensor shape size [batch_size, w*h, 2]
            # every anchor two score
            rpn_class_logits = tf.reshape(x, [tf.shape(x)[0], -1, 2])
            rpn_probs = tf.nn.softmax(rpn_class_logits)

            x = self.rpn_delta_pred(shared)
            # reshape convolution tensor shape size [batch_size, w*h, 4]
            # every anchor four revise item(paper corresponding t)
            rpn_deltas = tf.reshape(x, [tf.shape(x)[0], -1, 4])

            layer_outputs.append([rpn_class_logits, rpn_probs, rpn_deltas])

        # zip(*layer_outputs)解压layer_outputs, 将所有feature layer rpn_class_logits,
        # 保存到同一个tuple中, rpn_probs, rpn_deltas同理
        outputs = list(zip(*layer_outputs))
        # 各个层数据组合到一起
        outputs = [tf.concat(list(o), axis=1) for o in outputs]
        rpn_class_logits, rpn_probs, rpn_deltas = outputs

        return rpn_class_logits, rpn_probs, rpn_deltas

    def get_proposals(self,
                      rpn_probs,
                      rpn_deltas,
                      img_metas,
                      with_probs=False):
        """Calculate proposals.

        Args
        ---
            rpn_probs: [batch_size, num_anchors, (bg prob, fg prob)]
            rpn_deltas: [batch_size, num_anchors, (dy, dx, log(dh), log(dw))]
            img_metas: [batch_size, 11]
            with_probs: bool.

        Returns
        ---
            proposals: [batch_size * num_proposals, (batch_ind, y1, x1, y2, x2))] in
                normalized coordinates if with_probs is False.
                Otherwise, the shape of proposals in proposals_list is
                [batch_size * num_proposals, (batch_ind, y1, x1, y2, x2, probs)]

        """
        anchors, valid_flags = self.generator.generate_pyramid_anchors(img_metas)

        # 获取每个anchor的最大得分，取probs的第二个值
        rpn_probs = rpn_probs[:, :, 1]

        pad_shapes = calc_pad_shapes(img_metas)

        proposals_list = [
            self._get_proposals_single(
                rpn_probs[i], rpn_deltas[i], anchors, valid_flags[i], pad_shapes[i], i, with_probs)
            for i in range(img_metas.shape[0])
        ]

        proposals = tf.concat(proposals_list, axis=0)

        # Stops gradient computation
        return tf.stop_gradient(proposals)

    def _get_proposals_single(self,
                              rpn_probs,
                              rpn_deltas,
                              anchors,
                              valid_flags,
                              img_shape,
                              batch_ind,
                              with_probs):
        """Calculate proposals.

        Args
        ---
            rpn_probs: [num_anchors]
            rpn_deltas: [num_anchors, (dy, dx, log(dh), log(dw))]
            anchors: [num_anchors, (y1, x1, y2, x2)] anchors defined in
                pixel coordinates.
            valid_flags: [num_anchors]
            img_shape: np.ndarray. [2]. (img_height, img_width)
            batch_ind: int.
            with_probs: bool.

        Returns
        ---
            proposals: [num_proposals, (batch_ind, y1, x1, y2, x2)] in normalized
                coordinates.
        """

        H, W = img_shape

        # filter invalid anchors
        valid_flags = tf.cast(valid_flags, tf.bool)

        # anchors score
        rpn_probs = tf.boolean_mask(rpn_probs, valid_flags)
        # t, Parameter T used for anchors to proposals conversion
        rpn_deltas = tf.boolean_mask(rpn_deltas, valid_flags)
        # valid anchors
        anchors = tf.boolean_mask(anchors, valid_flags)

        # Improve performance
        pre_nms_limit = min(6000, anchors.shape[0])
        ix = tf.nn.top_k(rpn_probs, pre_nms_limit, sorted=True).indices

        # Gets the element of the corresponding index
        rpn_probs = tf.gather(rpn_probs, ix)
        rpn_deltas = tf.gather(rpn_deltas, ix)
        anchors = tf.gather(anchors, ix)

        """
        # Get refined anchors
        tx = (x − xa)/wa, ty = (y − ya)/ha, tw = log(w/wa), th = log(h/ha)
        x = tx*wa + xa, y = ty*ha + ya, w = e^tw * wa, h = e^th * ha
        """
        proposals = transforms.delta2bbox(anchors, rpn_deltas,
                                          self.target_means, self.target_stds)
        window = tf.constant([0., 0., H, W], dtype=tf.float32)
        proposals = transforms.bbox_clip(proposals, window)

        # Normalize
        proposals = proposals / tf.constant([H, W, H, W], dtype=tf.float32)

        # NMS
        indices = tf.image.non_max_suppression(proposals, rpn_probs,
                                               self.proposal_count, self.nms_threshold)
        proposals = tf.gather(proposals, indices)

        if with_probs:
            proposals_probs = tf.expand_dims(tf.gather(rpn_probs, indices), axis=1)
            proposals = tf.concat([proposals, proposals_probs], axis=1)

        # Pad
        padding = tf.maximum(self.proposal_count - tf.shape(proposals)[0], 0)
        # tf.pad(tensor, paddings), 此处paddings=[(0, padding), (0, 0)],
        # 代表在tensor的第1维两侧分别添加0，padding个片段，第2维两侧分别添加0，0个片段
        proposals = tf.pad(proposals, [(0, padding), (0, 0)])

        batch_inds = tf.ones((proposals.shape[0], 1)) * batch_ind
        proposals = tf.concat([batch_inds, proposals], axis=1)

        return proposals

    def loss(self, rpn_class_logits, rpn_deltas, gt_boxes, gt_class_ids, img_metas):
        """Calculate rpn loss
        """
        anchors, valid_flags = self.generator.generate_pyramid_anchors(img_metas)

        rpn_labels, rpn_label_weights, rpn_delta_targets, rpn_delta_weights = \
            self.anchor_target.build_targets(anchors, valid_flags, gt_boxes, gt_class_ids)

        rpn_class_loss = self.rpn_class_loss(rpn_labels, rpn_class_logits,
                                             rpn_label_weights)
        rpn_bbox_loss = self.rpn_bbox_loss(rpn_delta_targets, rpn_deltas,
                                           rpn_delta_weights)

        return rpn_class_loss, rpn_bbox_loss
