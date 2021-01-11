"""
# transformer结构的一些层
"""
import os
import logging
import torch
import torch.nn as nn
import torch.nn.functional as F

logger = logging.getLogger(__name__)


class ScaledDotProductAttention(nn.Module):
    """Scaled Dot-Product Attention, self-attention计算层"""
    def __init__(self, temperature, attn_dropout=0.1):
        super(ScaledDotProductAttention, self).__init__()
        self.temperature = temperature
        self.dropout = nn.Dropout(attn_dropout)

    def forward(self, q, k, v, mask=None):
        """q shape: [batch, n_head, sequence_len, hidden_size]
           k shape: same q, 因此转置的是2,3维度
           v shape: same q
           Attention(Q,K,V) = softmax(QK^T / sqrt(d_k))*V
        """
        attn = torch.matmul(q / self.temperature, k.transpose(2, 3))

        if mask is not None:
            # F.softmax(-1e9) = 0
            attn = attn.masked_fill(mask == 0, -1e9)

        attn = self.dropout(F.softmax(attn, dim=-1))
        output = torch.matmul(attn, v)

        return output, attn


class MultiHeadAttention(nn.Module):
    """Multi-Head Attention module"""
    def __init__(self, n_head, d_model, d_k, d_v, dropout=0.1):
        super(MultiHeadAttention, self).__init__()
        self.n_head = n_head
        self.d_k = d_k
        self.d_v = d_v

        self.w_qs = nn.Linear(d_model, self.n_head * self.d_k, bias=False)  # 多头权重一起初始化
        self.w_ks = nn.Linear(d_model, self.n_head * self.d_k, bias=False)
        self.w_vs = nn.Linear(d_model, self.n_head * self.d_v, bias=False)
        self.fc = nn.Linear(self.n_head * self.d_v, d_model, bias=False)

        self.attention = ScaledDotProductAttention(temperature=d_k ** 0.5)

        self.dropout = nn.Dropout(dropout)
        self.layer_norm = nn.LayerNorm(d_model, eps=1e-6)

    def forward(self, q, k, v, mask=None):
        d_k, d_v, n_head = self.d_k, self.d_v, self.n_head
        sz_b, len_q, len_k, len_v = q.size(0), q.size(1), k.size(1), v.size(1)
        residual = q

        # Pass through the pre-attention projection: b x lq x (n*dv)
        # Separate different heads: b x lq x nh x dv
        q = self.w_qs(q).view(sz_b, len_q, n_head, d_k)
        k = self.w_ks(k).view(sz_b, len_k, n_head, d_k)
        v = self.w_vs(v).view(sz_b, len_v, n_head, d_v)

        # Transpose for attention dot product: b x nh x lq x dv
        q, k, v = q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2)  # 矩阵乘法只对最后两维操作

        if mask is not None:
            mask = mask.unsqueeze(1)  # For head axis broadcasting

        q, attn = self.attention(q, k, v, mask=mask)

        # Transpose to move the head dimension back: b x lq x nh x dv
        # Combine the last two dimensions to concatenate all the heads together: b x lq x (nh*dv)
        q = q.transpose(1, 2).contiguous().view(sz_b, len_q, -1)  # 多头直接concat
        q = self.dropout(self.fc(q))
        q += residual

        q = self.layer_norm(q)

        return q, attn


class PositionwiseFeedForward(nn.Module):
    """A two-feed-forward-layer module"""
    def __init__(self, d_in, d_hid, dropout=0.1):
        super(PositionwiseFeedForward, self).__init__()
        self.w_1 = nn.Linear(d_in, d_hid)
        self.w_2 = nn.Linear(d_hid, d_in)  # position-wise
        self.layer_norm = nn.LayerNorm(d_in, eps=1e-6)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        """FFN(x) = max(0, xW_1 + b_1)W_1 + b_2, max(0, y)<=>relu(y)"""
        residual = x  # 残差

        x = self.w_2(F.relu(self.w_1(x)))
        x = self.dropout(x)
        x += residual  # 残差block

        x = self.layer_norm(x)

        return x
