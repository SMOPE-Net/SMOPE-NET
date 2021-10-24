import io
from collections import defaultdict
from typing import List, Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor
from PIL import Image
from torch.nn.modules import padding

from .Model3DNet import Model3DNet

import util.box_ops as box_ops
from util.misc import NestedTensor, interpolate, inverse_sigmoid, nested_tensor_from_tensor_list

class MHAttentionMap(nn.Module):
    """This is a 2D attention module, which only returns the attention softmax (no multiplication by value)"""

    def __init__(self, query_dim, hidden_dim, num_heads, dropout=0.0, bias=True):
        super().__init__()
        self.num_heads = num_heads
        self.hidden_dim = hidden_dim
        self.dropout = nn.Dropout(dropout)

        self.q_linear = nn.Linear(query_dim, hidden_dim, bias=bias)
        self.k_linear = nn.Linear(query_dim, hidden_dim, bias=bias)

        nn.init.zeros_(self.k_linear.bias)
        nn.init.zeros_(self.q_linear.bias)
        nn.init.xavier_uniform_(self.k_linear.weight)
        nn.init.xavier_uniform_(self.q_linear.weight)

        self.normalize_fact = float(hidden_dim / self.num_heads) ** -0.5

    def forward(self, q, k):
        q = self.q_linear(q)
        k = self.k_linear(k)

        qh = q.view(q.shape[0], q.shape[1], self.num_heads, self.hidden_dim // self.num_heads)
        kh = k.view(k.shape[0], k.shape[1], self.num_heads, self.hidden_dim // self.num_heads)

        weights = torch.einsum("bqnc,bmnc->bqnm", qh * self.normalize_fact, kh)

        weights = F.softmax(weights.flatten(2), dim=-1).view(weights.size())
        weights = self.dropout(weights)
        return weights


class PoseHeadSmallLinear(nn.Module):
    """
    Simple linear head
    """

    def __init__(self, num_dims, num_heads):
        super().__init__()
        self.num_dims = num_dims
        self.num_heads = num_heads
        
        self.l1 = nn.Linear(num_dims, num_dims)
        self.g1 = nn.GroupNorm(8, num_dims)

        num_feat = num_dims+num_heads

        #### for model class ####
        l2 = []
        num_l2 = 4
        for i in range(num_l2):
            l2.append(nn.Linear(num_feat // (2**i), num_feat // (2**(i+1))))
            l2.append(nn.ReLU())
        
        self.l2 = nn.Sequential(*l2)
        self.l3 = nn.Linear(num_feat // (2**num_l2), 1)

        ####  for 6 dof ###
        l4 = []
        num_l4 = 4
        for i in range(num_l4):
            l4.append(nn.Linear(num_feat // (2**i), num_feat // (2**(i+1))))
            l4.append(nn.ReLU())
        self.l4 = nn.Sequential(*l4)
        self.l5 = nn.Linear(num_feat // (2**num_l4), 6)
        
        

        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_normal_(m.weight.data, gain=nn.init.calculate_gain('relu'))
                nn.init.constant_(m.bias.data, 0)
        

    def forward(self, x: Tensor, attention: Tensor):
        x = self.l1(x)
        x = self.g1(x.permute(0, 2, 1)).permute(0, 2, 1)
        x = F.relu(x)
        x = torch.cat([x.unsqueeze(1).repeat(1, attention.shape[1], 1, 1), attention.permute(0, 1, 3, 2)], dim=3)

        ## for model class
        x_c = self.l2(x)
        x_c = F.relu(self.l3(x_c))
        x_c = F.softmax(x_c.squeeze(-1), dim=-1)

        ## for pose 6dof
        x_p = self.l4(x)
        x_p = self.l5(x_p)

        return x_c, x_p


class DETRpose(nn.Module):
    def __init__(self, detr, freeze_detr=False):
        super().__init__()
        self.detr = detr

        if freeze_detr:
            for p in self.parameters():
                p.requires_grad_(False)

        hidden_dim, nheads = detr.transformer.d_model, detr.transformer.nhead

        self.model_3d_net = Model3DNet()

        self.bbox_attention = MHAttentionMap(hidden_dim, hidden_dim, nheads, dropout=0.0)

        self.pose_head = PoseHeadSmallLinear(hidden_dim, nheads)

    def forward(self, samples: NestedTensor):
        if isinstance(samples, (list, torch.Tensor)):
            samples = nested_tensor_from_tensor_list(samples)
        
        features, pos = self.detr.backbone(samples)

        bs = features[-1].tensors.shape[0]

        src, mask = features[-1].decompose()
        assert mask is not None
        src_proj = self.detr.input_proj(src)
        hs, memory = self.detr.transformer(src_proj, mask, self.detr.query_embed.weight, pos[-1])

        outputs_class = self.detr.class_embed(hs)
        outputs_coord = self.detr.bbox_embed(hs).sigmoid()

        out = {
            "pred_logits": outputs_class[-1],
            "pred_boxes": outputs_coord[-1]
        }

        if self.detr.aux_loss:
            out["aux_outputs"] = self.detr._set_aux_loss(outputs_class, outputs_coord)

        # 3d model and query attention
        # 1. get the feature of 3d model
        model_3d_feat = self.model_3d_net.forward_encoder()

        pred_model_point = self.model_3d_net.forward_decoder(model_3d_feat)

        model_3d_feat = model_3d_feat[None].repeat(bs, 1, 1)

        # 2. cacluate the attention between the query bbox and the 3d model
        # input: hs[-1] (bs x n_q x n_f) model_3d_feat: (bs x n_m x n_f)
        # output: (bs x n_q x n_h x n_m)
        bbox_3dmodel_attention = self.bbox_attention(hs[-1], model_3d_feat)

        # 3. output the 3d model class and pose 6dof for each bbox
        # input: model_3d_feat (bs x n_m x n_f)
        # input: bbox_3dmodel_attention (bs x n_q x n_h x n_m)
        # output: pose_class (bs x n_q x n_m)
        # output: pose_6dof (bs x n_q x n_m x 6)
        pose_class, pose_6dof = self.pose_head(model_3d_feat, bbox_3dmodel_attention)

        out["pose_class"] = pose_class
        out["pose_6dof"] = pose_6dof
        out["pred_model_points"] = pred_model_point[0]
        out["pred_model_scales"] = pred_model_point[1]
        out["pred_model_centers"] = pred_model_point[2]
        
        return out