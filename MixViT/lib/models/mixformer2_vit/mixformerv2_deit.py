import math
from functools import partial

import torch
import torch.nn as nn
import torch.nn.functional as F

import timm.models.vision_transformer
from timm.models.layers import DropPath, trunc_normal_, Mlp

from lib.utils.misc import is_main_process
from lib.models.mixformer2_vit.head import build_box_head, build_score_decoder
from lib.utils.box_ops import box_xyxy_to_cxcywh
from lib.models.mixformer_vit.pos_util import get_2d_sincos_pos_embed

from itertools import repeat
import collections.abc

# From PyTorch internals
def _ntuple(n):
    def parse(x):
        if isinstance(x, collections.abc.Iterable):
            return x
        return tuple(repeat(x, n))
    return parse

to_2tuple = _ntuple(2)

class PatchEmbed(nn.Module):
    """ 2D Image to Patch Embedding
    """
    def __init__(self, patch_size=16, in_chans=3, embed_dim=768, norm_layer=None, flatten=True):
        super().__init__()
        patch_size = to_2tuple(patch_size)
        self.flatten = flatten

        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size)
        self.norm = norm_layer(embed_dim) if norm_layer else nn.Identity()

    def forward(self, x):
        # B, C, H, W = x.shape
        # _assert(H == self.img_size[0], f"Input image height ({H}) doesn't match model ({self.img_size[0]}).")
        # _assert(W == self.img_size[1], f"Input image width ({W}) doesn't match model ({self.img_size[1]}).")
        x = self.proj(x)
        if self.flatten:
            x = x.flatten(2).transpose(1, 2).contiguous()  # BCHW -> BNC
        x = self.norm(x)
        return x

class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, attn_drop=0., proj_drop=0.):
        super().__init__()
        assert dim % num_heads == 0, 'dim should be divisible by num_heads'
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        #self.qkv_mem = None

    def forward(self, x, num_t, num_s):
        """
        x is a concatenated vector of template and search region features.
        """
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv.unbind(0)   # make torchscript happy (cannot use tensor as tuple)

        q_t, q_s = torch.split(q, [num_t, num_s+4], dim=2)
        k_t, k_s = torch.split(k, [num_t, num_s+4], dim=2)
        v_t, v_s = torch.split(v, [num_t, num_s+4], dim=2)

        # asymmetric mixed attention
        attn = (q_t @ k_t.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)
        x_t = (attn @ v_t).transpose(1, 2).reshape(B, num_t, C)

        attn = (q_s @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)
        x_s = (attn @ v).transpose(1, 2).reshape(B, num_s+4, C)

        x = torch.cat([x_t, x_s], dim=1)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x

class LayerScale(nn.Module):
    def __init__(self, dim, init_values=1e-5, inplace=False):
        super().__init__()
        self.inplace = inplace
        self.gamma = nn.Parameter(init_values * torch.ones(dim))

    def forward(self, x):
        return x.mul_(self.gamma) if self.inplace else x * self.gamma

class Block(nn.Module):
    def __init__(
            self, dim, num_heads, mlp_ratio=4., qkv_bias=False, drop=0., attn_drop=0., init_values=None,
            drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(dim, num_heads=num_heads, qkv_bias=qkv_bias, attn_drop=attn_drop, proj_drop=drop)
        self.ls1 = LayerScale(dim, init_values=init_values) if init_values else nn.Identity()
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path1 = DropPath(drop_path) if drop_path > 0. else nn.Identity()

        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
        self.ls2 = LayerScale(dim, init_values=init_values) if init_values else nn.Identity()
        self.drop_path2 = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.dim = dim

    def forward(self, x, num_t, num_s):
        x = x + self.drop_path1(self.attn(self.norm1(x), num_t, num_s))
        x = x + self.drop_path2(self.mlp(self.norm2(x)))
        return x


class VisionTransformer(timm.models.vision_transformer.VisionTransformer):
    """ Vision Transformer with support for global average pooling
    """
    def __init__(self, img_size_s=256, img_size_t=128, patch_size=16, in_chans=3, num_classes=1000, embed_dim=768,
                 depth=12, num_heads=12, mlp_ratio=4., qkv_bias=True, drop_rate=0., attn_drop_rate=0.,
                 drop_path_rate=0., weight_init='', embed_layer=PatchEmbed, norm_layer=None, act_layer=None):
        super(VisionTransformer, self).__init__(img_size=224, patch_size=patch_size, in_chans=in_chans,
                                                num_classes=num_classes, embed_dim=embed_dim, depth=depth,
                                                num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias,
                                                drop_rate=drop_rate, attn_drop_rate=attn_drop_rate,
                                                drop_path_rate=drop_path_rate, weight_init=weight_init,
                                                norm_layer=norm_layer, act_layer=act_layer)

        self.patch_embed = embed_layer(
            patch_size=patch_size, in_chans=in_chans, embed_dim=embed_dim)
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.blocks = nn.Sequential(*[
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i],
                norm_layer=norm_layer) for i in range(depth)])


        self.num_patches_s = (img_size_s // patch_size) ** 2
        self.num_patches_t = (img_size_t // patch_size) ** 2
        self.pos_embed_s = nn.Parameter(torch.zeros(1, self.num_patches_s, embed_dim), requires_grad=False)
        self.pos_embed_t = nn.Parameter(torch.zeros(1, self.num_patches_t, embed_dim), requires_grad=False)

        self.reg_tokens = nn.Parameter(torch.randn(1, 4, embed_dim))  #
        self.pos_embed_reg = nn.Parameter(torch.zeros(1, 4, embed_dim))  #
        self.pos_drop = nn.Dropout(p=drop_rate)  #

        self.initialize_pos_weights()

        trunc_normal_(self.reg_tokens, std=.02)
        if weight_init != 'skip':
            self.init_weights(weight_init)

    def initialize_pos_weights(self):
        # initialization
        # initialize (and freeze) pos_embed by sin-cos embedding
        pos_embed_t = get_2d_sincos_pos_embed(self.pos_embed_t.shape[-1], int(self.num_patches_t ** .5),
                                            cls_token=False)
        self.pos_embed_t.data.copy_(torch.from_numpy(pos_embed_t).float().unsqueeze(0))

        pos_embed_s = get_2d_sincos_pos_embed(self.pos_embed_s.shape[-1], int(self.num_patches_s ** .5),
                                              cls_token=False)
        self.pos_embed_s.data.copy_(torch.from_numpy(pos_embed_s).float().unsqueeze(0))

    def forward(self, x_t, x_s):  #
        """
        :param x_t: (batch, c, 128, 128)
        :param x_s: (batch, c, 288, 288)
        :return:
        """
        x_t = self.patch_embed(x_t)  # BCHW-->BNC
        x_s = self.patch_embed(x_s)
        B, C = x_t.size(0), x_t.size(-1)
        H_s = W_s = int(math.sqrt(x_s.size(1) + 0.1))
        H_t = W_t = int(math.sqrt(x_t.size(1) + 0.1))

        x_s = x_s + self.pos_embed_s
        x_t = x_t + self.pos_embed_t

        reg_tokens = self.reg_tokens.expand(B, -1, -1)  # (b, 4, embed_dim)
        reg_tokens = reg_tokens + self.pos_embed_reg

        x = torch.cat([x_t,  x_s, reg_tokens], dim=1)  # (b, hw+hw+HW+4, embed_dim)
        x = self.pos_drop(x)

        distill_feat_list = []

        for i, blk in enumerate(self.blocks):
            x = blk(x, H_t*W_t, H_s*W_s)
            distill_feat_list.append(x)

        x_t, x_s, reg_tokens = torch.split(x, [H_t * W_t, H_s * W_s, 4], dim=1)

        x_t_2d = x_t.transpose(1, 2).reshape(B, C, H_t, W_t)
        x_s_2d = x_s.transpose(1, 2).reshape(B, C, H_s, W_s)

        return x_t_2d, x_s_2d, reg_tokens, distill_feat_list


def get_mixformer_vit(config):
    img_size_s = config.DATA.SEARCH.SIZE
    img_size_t = config.DATA.TEMPLATE.SIZE
    if config.MODEL.VIT_TYPE == 'large_patch16':
        vit = VisionTransformer(
            img_size_s=img_size_s, img_size_t=img_size_t,
            patch_size=16, embed_dim=1024, depth=24, num_heads=16, mlp_ratio=4, qkv_bias=True,
            norm_layer=partial(nn.LayerNorm, eps=1e-6), drop_path_rate=0.1)
    elif config.MODEL.VIT_TYPE == 'base_patch16':
        vit = VisionTransformer(
            img_size_s=img_size_s, img_size_t=img_size_t,
            patch_size=16, embed_dim=768, depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
            norm_layer=partial(nn.LayerNorm, eps=1e-6), drop_path_rate=0.1)
    elif config.MODEL.VIT_TYPE == 'deit3_4layers':
        vit = VisionTransformer(
            img_size_s=img_size_s, img_size_t=img_size_t,
            patch_size=16, embed_dim=768, depth=4, num_heads=12, mlp_ratio=4, qkv_bias=True,
            norm_layer=partial(nn.LayerNorm, eps=1e-6), drop_path_rate=0.1)
    else:
        raise KeyError(f"VIT_TYPE shoule set to 'large_patch16' or 'base_patch16'")

    return vit


class MixFormer(nn.Module):
    """ This is the base class for Transformer Tracking, whcih jointly perform feature extraction and interaction. """
    def __init__(self, backbone, box_head, score_head, head_type="CORNER"):
        """ Initializes the model.
        """
        super().__init__()
        self.backbone = backbone
        self.box_head = box_head
        self.head_type = head_type
        self.score_head = score_head

    def forward(self, template, search):
        # search: (b, c, h, w)
        if template.dim() == 5:
            template = template.squeeze(0)
        # if online_template.dim() == 5:
        #     template = online_template.squeeze(0)
        if search.dim() == 5:
            search = search.squeeze(0)
        template, search, reg_tokens, distill_feat_list = self.backbone(template, search)
        # Forward the corner head
        out = self.forward_head(search, reg_tokens=reg_tokens, softmax=True)
        out['reg_tokens'] = reg_tokens
        out['distill_feat_list'] = distill_feat_list

        return out

    def forward_test(self, search, softmax, run_score_head=True, gt_bboxes=None):
        # search: (b, c, h, w) h=20
        if search.dim() == 5:
            search = search.squeeze(0)
        template, search, reg_tokens = self.backbone.forward_test(search)
        # search (b, 384, 20, 20)
        # Forward the corner head
        out, outputs_coord_new = self.forward_head(search, reg_tokens=reg_tokens, softmax=softmax)
        return out, outputs_coord_new

    # def set_online(self, template, online_template):
    #     if template.dim() == 5:
    #         template = template.squeeze(0)
    #     if online_template.dim() == 5:
    #         template = online_template.squeeze(0)
    #     self.backbone.set_online(template, online_template)

    def forward_head(self, search, reg_tokens, softmax):##
        """
        :param search: (b, c, h, w), reg_mask: (b, h, w)
        :return:
        """
        out_dict = {}
        out_dict_box = self.forward_box_head(search, reg_tokens=reg_tokens, softmax=softmax)
        out_dict.update(out_dict_box)
        out_dict.update({
            'pred_scores': self.score_head(reg_tokens).view(-1),
        })
        return out_dict

    def forward_box_head(self, search, reg_tokens, softmax):  ##
        """
        :param search: (b, c, h, w)
        :return:
        """
        if "MLP" in self.head_type:
            b = reg_tokens.size(0)
            pred_boxes, prob_l, prob_t, prob_r, prob_b = self.box_head(reg_tokens, softmax=softmax)
            #outputs_coord = box_xyxy_to_cxcywh(pred_boxes)
            outputs_coord_new = pred_boxes.view(b, 1, 4)
            out = {
                'pred_boxes': outputs_coord_new,
                'prob_l': prob_l,
                'prob_t': prob_t,
                'prob_b': prob_b,
                'prob_r': prob_r,
            }
            return out
        else:
            raise KeyError


def build_mixformer_deit(cfg):
    backbone = get_mixformer_vit(cfg)  # backbone without positional encoding and attention mask
    box_head = build_box_head(cfg)  # a simple corner head
    score_head = build_score_decoder(cfg)
    model = MixFormer(
        backbone,
        box_head,
        score_head,
        head_type=cfg.MODEL.HEAD_TYPE
    )

    if cfg.MODEL.BACKBONE.PRETRAINED:
        ckpt_path = cfg.MODEL.BACKBONE.PRETRAINED_PATH
        ckpt = torch.load(ckpt_path, map_location='cpu')['net']
        new_dict = {}
        for k, v in ckpt.items():
            if 'pos_embed' not in k and 'mask_token' not in k:
                new_dict[k] = v
        missing_keys, unexpected_keys = model.load_state_dict(new_dict, strict=False)
        if is_main_process():
            print("missing keys:", missing_keys)
            print("unexpected keys:", unexpected_keys)
            print("Loading pretrained ViT done.")

    return model