﻿# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

from functools import partial
from typing import Optional, List

import numpy as np
import torch
import torch.nn as nn
from torch import Tensor
from omegaconf import OmegaConf
import torch.nn.functional as F

from hyper_nerf_gan.src.nerf.run_nerf import render, run_network
from hyper_nerf_gan.src.nerf.run_nerf_helpers import get_embedder
from hyper_nerf_gan.src.nerf.load_blender import pose_spherical
from hyper_nerf_gan.src.torch_utils import misc
from hyper_nerf_gan.src.torch_utils import persistence
from hyper_nerf_gan.src.torch_utils.misc import uniform_circle
from hyper_nerf_gan.src.torch_utils.ops import conv2d_resample
from hyper_nerf_gan.src.torch_utils.ops import upfirdn2d
from hyper_nerf_gan.src.torch_utils.ops import bias_act
from hyper_nerf_gan.src.torch_utils.ops import fma
from hyper_nerf_gan.src.training.layers import FullyConnectedLayer, GenInput, CoordFuser

#----------------------------------------------------------------------------

@misc.profiled_function
def normalize_2nd_moment(x, dim=1, eps=1e-8):
    return x * (x.square().mean(dim=dim, keepdim=True) + eps).rsqrt()

#----------------------------------------------------------------------------

@misc.profiled_function
def modulated_conv2d(
    x,                          # Input tensor of shape [batch_size, in_channels, in_height, in_width].
    weight,                     # Weight tensor of shape [out_channels, in_channels, kernel_height, kernel_width].
    styles,                     # Modulation coefficients of shape [batch_size, in_channels].
    noise           = None,     # Optional noise tensor to add to the output activations.
    up              = 1,        # Integer upsampling factor.
    down            = 1,        # Integer downsampling factor.
    padding         = 0,        # Padding with respect to the upsampled image.
    resample_filter = None,     # Low-pass filter to apply when resampling activations. Must be prepared beforehand by calling upfirdn2d.setup_filter().
    demodulate      = True,     # Apply weight demodulation?
    flip_weight     = True,     # False = convolution, True = correlation (matches torch.nn.functional.conv2d).
    fused_modconv   = True,     # Perform modulation, convolution, and demodulation as a single fused operation?
):
    batch_size = x.shape[0]
    out_channels, in_channels, kh, kw = weight.shape
    misc.assert_shape(weight, [out_channels, in_channels, kh, kw]) # [OIkk]
    misc.assert_shape(x, [batch_size, in_channels, None, None]) # [NIHW]
    misc.assert_shape(styles, [batch_size, in_channels]) # [NI]

    # Pre-normalize inputs to avoid FP16 overflow.
    if x.dtype == torch.float16 and demodulate:
        weight = weight * (1 / np.sqrt(in_channels * kh * kw) / weight.norm(float('inf'), dim=[1,2,3], keepdim=True)) # max_Ikk
        styles = styles / styles.norm(float('inf'), dim=1, keepdim=True) # max_I

    # Calculate per-sample weights and demodulation coefficients.
    w = None
    dcoefs = None
    if demodulate or fused_modconv:
        w = weight.unsqueeze(0) * styles.reshape(batch_size, 1, -1, 1, 1) # [NOIkk]
    if demodulate:
        dcoefs = (w.square().sum(dim=[2,3,4]) + 1e-8).rsqrt() # [NO]
    if demodulate and fused_modconv:
        w = w * dcoefs.reshape(batch_size, -1, 1, 1, 1) # [NOIkk]

    # Execute by scaling the activations before and after the convolution.
    if not fused_modconv:
        x = x * styles.to(x.dtype).reshape(batch_size, -1, 1, 1)
        x = conv2d_resample.conv2d_resample(x=x, w=weight.to(x.dtype), f=resample_filter, up=up, down=down, padding=padding, flip_weight=flip_weight)
        if demodulate and noise is not None:
            x = fma.fma(x, dcoefs.to(x.dtype).reshape(batch_size, -1, 1, 1), noise.to(x.dtype))
        elif demodulate:
            x = x * dcoefs.to(x.dtype).reshape(batch_size, -1, 1, 1)
        elif noise is not None:
            x = x.add_(noise.to(x.dtype))
        return x

    # Execute as one fused op using grouped convolution.
    with misc.suppress_tracer_warnings(): # this value will be treated as a constant
        batch_size = int(batch_size)
    misc.assert_shape(x, [batch_size, in_channels, None, None])
    x = x.reshape(1, -1, *x.shape[2:])
    w = w.reshape(-1, in_channels, kh, kw)
    x = conv2d_resample.conv2d_resample(x=x, w=w.to(x.dtype), f=resample_filter, up=up, down=down, padding=padding, groups=batch_size, flip_weight=flip_weight)
    x = x.reshape(batch_size, -1, *x.shape[2:])
    if noise is not None:
        x = x.add_(noise)
    return x


@misc.profiled_function
def fmm_modulate_linear(x: Tensor, weight: Tensor, styles: Tensor, noise=None, activation: str="demod") -> Tensor:
    """
    x: [batch_size, c_in, height, width]
    weight: [c_out, c_in, 1, 1]
    style: [batch_size, num_mod_params]
    noise: Optional[batch_size, 1, height, width]
    """
    batch_size, c_in, h, w = x.shape
    c_out, c_in, kh, kw = weight.shape
    rank = styles.shape[1] // (c_in + c_out)

    assert kh == 1 and kw == 1
    assert styles.shape[1] % (c_in + c_out) == 0

    # Now, we need to construct a [c_out, c_in] matrix
    left_matrix = styles[:, :c_out * rank] # [batch_size, left_matrix_size]
    right_matrix = styles[:, c_out * rank:] # [batch_size, right_matrix_size]

    left_matrix = left_matrix.view(batch_size, c_out, rank) # [batch_size, c_out, rank]
    right_matrix = right_matrix.view(batch_size, rank, c_in) # [batch_size, rank, c_in]

    # Imagine, that the output of `self.affine` (in SynthesisLayer) is N(0, 1)
    # Then, std of weights is sqrt(rank). Converting it back to N(0, 1)
    modulation = left_matrix @ right_matrix / np.sqrt(rank) # [batch_size, c_out, c_in]

    if activation == "tanh":
        modulation = modulation.tanh()
    elif activation == "sigmoid":
        modulation = modulation.sigmoid() - 0.5

    W = weight.squeeze(3).squeeze(2).unsqueeze(0) * (modulation + 1.0) # [batch_size, c_out, c_in]
    if activation == "demod":
        W = W / (W.norm(dim=2, keepdim=True) + 1e-8) # [batch_size, c_out, c_in]
    W = W.to(dtype=x.dtype)

    # out = torch.einsum('boi,bihw->bohw', W, x)
    x = x.view(batch_size, c_in, h * w) # [batch_size, c_in, h * w]
    out = torch.bmm(W, x) # [batch_size, c_out, h * w]
    out = out.view(batch_size, c_out, h, w) # [batch_size, c_out, h, w]

    if not noise is None:
        out = out.add_(noise)

    return out

#----------------------------------------------------------------------------


@misc.profiled_function
def fmm_modulate_linear_nerf(x: Tensor, weight: Tensor, styles: Tensor, activation: str="demod") -> Tensor:
    """
    `fmm_modulate_linear` for batch size equal to 1.

    x: [num_rays, c_in]
    weight: [c_out, c_in]
    style: [num_mod_params]
    """
    num_rays, c_in = x.shape
    c_out, c_in = weight.shape
    rank = styles.shape[0] // (c_in + c_out)

    assert styles.shape[0] % (c_in + c_out) == 0
    assert len(styles.shape) == 1

    # Now, we need to construct a [c_out, c_in] matrix
    left_matrix = styles[:c_out * rank]  # [left_matrix_size]
    right_matrix = styles[c_out * rank:]  # [right_matrix_size]

    left_matrix = left_matrix.view(c_out, rank)  # [c_out, rank]
    right_matrix = right_matrix.view(rank, c_in)  # [c_out, rank]

    # Imagine, that the output of `self.affine` (in SynthesisLayer) is N(0, 1)
    # Then, std of weights is sqrt(rank). Converting it back to N(0, 1)
    modulation = left_matrix @ right_matrix / np.sqrt(rank)  # [c_out, c_in]

    if activation == "tanh":
        modulation = modulation.tanh()
    elif activation == "sigmoid":
        modulation = modulation.sigmoid() - 0.5

    W = weight * (modulation + 1.0)  # [c_out, c_in]
    if activation == "demod":
        W = W / (W.norm(dim=1, keepdim=True) + 1e-8)  # [c_out, c_in]
    W = W.to(dtype=x.dtype)

    x = x.view(num_rays, c_in, 1)
    out = torch.matmul(W, x) # [num_rays, c_out, 1]
    out = out.view(num_rays, c_out) # [num_rays, c_out]

    return out
#----------------------------------------------------------------------------

@misc.profiled_function
def maybe_upsample(x, upsampling_mode: str=None, up: int=1) -> Tensor:
    if up == 1 or upsampling_mode is None:
        return x

    if upsampling_mode == 'bilinear':
        x = F.interpolate(x, mode='bilinear', align_corners=True, scale_factor=up)
    else:
        x = F.interpolate(x, mode=upsampling_mode, scale_factor=up)

    return x

#----------------------------------------------------------------------------

@persistence.persistent_class
class Conv2dLayer(torch.nn.Module):
    def __init__(self,
        in_channels,                    # Number of input channels.
        out_channels,                   # Number of output channels.
        kernel_size,                    # Width and height of the convolution kernel.
        bias            = True,         # Apply additive bias before the activation function?
        activation      = 'linear',     # Activation function: 'relu', 'lrelu', etc.
        up              = 1,            # Integer upsampling factor.
        down            = 1,            # Integer downsampling factor.
        resample_filter = [1,3,3,1],    # Low-pass filter to apply when resampling activations.
        conv_clamp      = None,         # Clamp the output to +-X, None = disable clamping.
        channels_last   = False,        # Expect the input to have memory_format=channels_last?
        trainable       = True,         # Update the weights of this layer during training?
    ):
        super().__init__()
        self.activation = activation
        self.up = up
        self.down = down
        self.conv_clamp = conv_clamp
        self.register_buffer('resample_filter', upfirdn2d.setup_filter(resample_filter))
        self.padding = kernel_size // 2
        self.weight_gain = 1 / np.sqrt(in_channels * (kernel_size ** 2))
        self.act_gain = bias_act.activation_funcs[activation].def_gain

        memory_format = torch.channels_last if channels_last else torch.contiguous_format
        weight = torch.randn([out_channels, in_channels, kernel_size, kernel_size]).to(memory_format=memory_format)
        bias = torch.zeros([out_channels]) if bias else None
        if trainable:
            self.weight = torch.nn.Parameter(weight)
            self.bias = torch.nn.Parameter(bias) if bias is not None else None
        else:
            self.register_buffer('weight', weight)
            if bias is not None:
                self.register_buffer('bias', bias)
            else:
                self.bias = None

    def forward(self, x, gain=1):
        w = self.weight * self.weight_gain
        b = self.bias.to(x.dtype) if self.bias is not None else None
        flip_weight = (self.up == 1) # slightly faster
        x = conv2d_resample.conv2d_resample(x=x, w=w.to(x.dtype), f=self.resample_filter, up=self.up, down=self.down, padding=self.padding, flip_weight=flip_weight)

        act_gain = self.act_gain * gain
        act_clamp = self.conv_clamp * gain if self.conv_clamp is not None else None
        x = bias_act.bias_act(x, b, act=self.activation, gain=act_gain, clamp=act_clamp)
        return x

#----------------------------------------------------------------------------

@persistence.persistent_class
class MappingNetwork(torch.nn.Module):
    def __init__(self,
        z_dim,                      # Input latent (Z) dimensionality, 0 = no latent.
        c_dim,                      # Conditioning label (C) dimensionality, 0 = no label.
        w_dim,                      # Intermediate latent (W) dimensionality.
        num_ws,                     # Number of intermediate latents to output, None = do not broadcast.
        num_layers      = 8,        # Number of mapping layers.
        embed_features  = None,     # Label embedding dimensionality, None = same as w_dim.
        layer_features  = None,     # Number of intermediate features in the mapping layers, None = same as w_dim.
        activation      = 'lrelu',  # Activation function: 'relu', 'lrelu', etc.
        lr_multiplier   = 0.01,     # Learning rate multiplier for the mapping layers.
        w_avg_beta      = 0.995,    # Decay for tracking the moving average of W during training, None = do not track.
    ):
        super().__init__()
        self.z_dim = z_dim
        self.c_dim = c_dim
        self.w_dim = w_dim
        self.num_ws = num_ws
        self.num_layers = num_layers
        self.w_avg_beta = w_avg_beta

        if embed_features is None:
            embed_features = w_dim
        if c_dim == 0:
            embed_features = 0
        if layer_features is None:
            layer_features = w_dim
        features_list = [z_dim + embed_features] + [layer_features] * (num_layers - 1) + [w_dim]

        if c_dim > 0:
            self.embed = FullyConnectedLayer(c_dim, embed_features)
        for idx in range(num_layers):
            in_features = features_list[idx]
            out_features = features_list[idx + 1]
            layer = FullyConnectedLayer(in_features, out_features, activation=activation, lr_multiplier=lr_multiplier)
            setattr(self, f'fc{idx}', layer)

        if num_ws is not None and w_avg_beta is not None:
            self.register_buffer('w_avg', torch.zeros([w_dim]))

    def forward(self, z, c, truncation_psi=1, truncation_cutoff=None, skip_w_avg_update=False):
        # Embed, normalize, and concat inputs.
        x = None
        with torch.autograd.profiler.record_function('input'):
            if self.z_dim > 0:
                misc.assert_shape(z, [None, self.z_dim])
                x = normalize_2nd_moment(z.to(torch.float32))
            if self.c_dim > 0:
                misc.assert_shape(c, [None, self.c_dim])
                y = normalize_2nd_moment(self.embed(c.to(torch.float32)))
                x = torch.cat([x, y], dim=1) if x is not None else y

        # Main layers.
        for idx in range(self.num_layers):
            layer = getattr(self, f'fc{idx}')
            x = layer(x)

        # Update moving average of W.
        if self.w_avg_beta is not None and self.training and not skip_w_avg_update:
            with torch.autograd.profiler.record_function('update_w_avg'):
                self.w_avg.copy_(x.detach().mean(dim=0).lerp(self.w_avg, self.w_avg_beta))

        # Broadcast.
        if self.num_ws is not None:
            with torch.autograd.profiler.record_function('broadcast'):
                x = x.unsqueeze(1).repeat([1, self.num_ws, 1])

        # Apply truncation.
        if truncation_psi != 1:
            with torch.autograd.profiler.record_function('truncate'):
                assert self.w_avg_beta is not None
                if self.num_ws is None or truncation_cutoff is None:
                    x = self.w_avg.lerp(x, truncation_psi)
                else:
                    x[:, :truncation_cutoff] = self.w_avg.lerp(x[:, :truncation_cutoff], truncation_psi)
        return x

#----------------------------------------------------------------------------

@persistence.persistent_class
class SynthesisLayer(torch.nn.Module):
    def __init__(self,
        in_channels,                    # Number of input channels.
        out_channels,                   # Number of output channels.
        w_dim,                          # Intermediate latent (W) dimensionality.
        resolution,                     # Resolution of this layer.
        kernel_size     = 3,            # Convolution kernel size.
        up              = 1,            # Integer upsampling factor.
        activation      = 'lrelu',      # Activation function: 'relu', 'lrelu', etc.
        resample_filter = [1,3,3,1],    # Low-pass filter to apply when resampling activations.
        conv_clamp      = None,         # Clamp the output of convolution layers to +-X, None = disable clamping.
        channels_last   = False,        # Use channels_last format for the weights?
        instance_norm   = False,        # Use instance norm?
        cfg             = {},           # Additional config
    ):
        super().__init__()

        self.cfg = OmegaConf.create(cfg)
        self.resolution = resolution
        self.up = up
        self.activation = activation
        self.conv_clamp = conv_clamp
        self.register_buffer('resample_filter', upfirdn2d.setup_filter(resample_filter))
        self.padding = kernel_size // 2
        self.act_gain = bias_act.activation_funcs[activation].def_gain

        if self.cfg.fmm.enabled:
            self.affine = FullyConnectedLayer(w_dim, (in_channels + out_channels) * self.cfg.fmm.rank, bias_init=0)
        else:
            self.affine = FullyConnectedLayer(w_dim, in_channels, bias_init=1)
        memory_format = torch.channels_last if channels_last else torch.contiguous_format
        self.weight = torch.nn.Parameter(torch.randn([out_channels, in_channels, kernel_size, kernel_size]).to(memory_format=memory_format))
        if self.cfg.use_noise:
            self.register_buffer('noise_const', torch.randn([resolution, resolution]))
            self.noise_strength = torch.nn.Parameter(torch.zeros([]))
        self.bias = torch.nn.Parameter(torch.zeros([out_channels]))
        self.instance_norm = instance_norm

    def forward(self, x, w, noise_mode='random', fused_modconv=True, gain=1):
        assert noise_mode in ['random', 'const', 'none']
        in_resolution = self.resolution // self.up
        misc.assert_shape(x, [None, self.weight.shape[1], in_resolution, in_resolution])
        styles = self.affine(w)

        noise = None
        if self.cfg.use_noise and noise_mode == 'random':
            noise = torch.randn([x.shape[0], 1, self.resolution, self.resolution], device=x.device) * self.noise_strength
        if self.cfg.use_noise and noise_mode == 'const':
            noise = self.noise_const * self.noise_strength

        flip_weight = (self.up == 1) # slightly faster

        if self.instance_norm:
            x = x / (x.std(dim=[2,3], keepdim=True) + 1e-8) # [batch_size, c, h, w]

        if self.cfg.fmm.enabled:
            x = fmm_modulate_linear(x=x, weight=self.weight, styles=styles, noise=noise, activation=self.cfg.fmm.activation)
        else:
            x = modulated_conv2d(x=x, weight=self.weight, styles=styles, noise=noise, up=self.up,
                padding=self.padding, resample_filter=self.resample_filter, flip_weight=flip_weight,
                fused_modconv=fused_modconv)

        act_gain = self.act_gain * gain
        act_clamp = self.conv_clamp * gain if self.conv_clamp is not None else None
        x = bias_act.bias_act(x, self.bias.to(x.dtype), act=self.activation, gain=act_gain, clamp=act_clamp)
        return x

#----------------------------------------------------------------------------

@persistence.persistent_class
class NerfSynthesisLayer(torch.nn.Module):
    def __init__(self,
        in_channels,                    # Number of input channels.
        out_channels,                   # Number of output channels.
        w_dim,                          # Intermediate latent (W) dimensionality.
        activation      = 'lrelu',      # Activation function: 'relu', 'lrelu', etc.
        channels_last   = False,        # Use channels_last format for the weights?
        # instance_norm   = False,        # Use instance norm?
        cfg             = {},           # Additional config
    ):
        super().__init__()

        self.cfg = OmegaConf.create(cfg)
        self.activation = activation

        self.w_dim = w_dim
        self.affine = FullyConnectedLayer(self.w_dim, (in_channels + out_channels) * self.cfg.fmm.rank, bias_init=0)

        memory_format = torch.channels_last if channels_last else torch.contiguous_format
        self.weight = torch.nn.Parameter(torch.randn([out_channels, in_channels]).to(memory_format=memory_format))
        self.bias = torch.nn.Parameter(torch.zeros([out_channels]))
        # self.instance_norm = instance_norm

    def forward(self, x, w):
        misc.assert_shape(x, [None, self.weight.shape[1]])
        misc.assert_shape(w, [1, self.w_dim])
        styles = self.affine(w).squeeze(0)

        # if self.instance_norm:
        #     x = x / (x.std(dim=[2,3], keepdim=True) + 1e-8) # [batch_size, c, h, w]

        x = fmm_modulate_linear_nerf(x=x, weight=self.weight, styles=styles, activation=self.cfg.fmm.activation)

        x = bias_act.bias_act(x, self.bias.to(x.dtype), act=self.activation)
        return x

#----------------------------------------------------------------------------

@persistence.persistent_class
class ToRGBLayer(torch.nn.Module):
    def __init__(self, in_channels, out_channels, w_dim, kernel_size=1, conv_clamp=None, channels_last=False):
        super().__init__()
        self.conv_clamp = conv_clamp
        self.affine = FullyConnectedLayer(w_dim, in_channels, bias_init=1)
        memory_format = torch.channels_last if channels_last else torch.contiguous_format
        self.weight = torch.nn.Parameter(torch.randn([out_channels, in_channels, kernel_size, kernel_size]).to(memory_format=memory_format))
        self.bias = torch.nn.Parameter(torch.zeros([out_channels]))
        self.weight_gain = 1 / np.sqrt(in_channels * (kernel_size ** 2))

    def forward(self, x, w, fused_modconv=True):
        styles = self.affine(w) * self.weight_gain
        x = modulated_conv2d(x=x, weight=self.weight, styles=styles, demodulate=False, fused_modconv=fused_modconv)
        x = bias_act.bias_act(x, self.bias.to(x.dtype), clamp=self.conv_clamp)
        return x

#----------------------------------------------------------------------------

@persistence.persistent_class
class SynthesisBlock(torch.nn.Module):
    def __init__(self,
        in_channels,                        # Number of input channels, 0 = first block.
        out_channels,                       # Number of output channels.
        w_dim,                              # Intermediate latent (W) dimensionality.
        resolution,                         # Resolution of this block.
        img_channels,                       # Number of output color channels.
        is_last,                            # Is this the last block?
        architecture        = 'skip',       # Architecture: 'orig', 'skip', 'resnet'.
        resample_filter     = [1,3,3,1],    # Low-pass filter to apply when resampling activations.
        conv_clamp          = None,         # Clamp the output of convolution layers to +-X, None = disable clamping.
        use_fp16            = False,        # Use FP16 for this block?
        fp16_channels_last  = False,        # Use channels-last memory format with FP16?
        cfg                 = {},           # Additional config
        **layer_kwargs,                     # Arguments for SynthesisLayer.
    ):
        assert architecture in ['orig', 'skip', 'resnet']
        super().__init__()

        self.cfg = OmegaConf.create(cfg)
        self.in_channels = in_channels
        self.w_dim = w_dim

        if resolution <= self.cfg.input.resolution:
            self.resolution = self.cfg.input.resolution
            self.up = 1
            self.input_resolution = self.cfg.input.resolution
        else:
            self.resolution = resolution
            self.up = 2
            self.input_resolution = resolution // 2

        self.img_channels = img_channels
        self.is_last = is_last
        self.architecture = architecture
        self.use_fp16 = use_fp16
        self.channels_last = (use_fp16 and fp16_channels_last)
        self.register_buffer('resample_filter', upfirdn2d.setup_filter(resample_filter))
        self.num_conv = 0
        self.num_torgb = 0

        kernel_size = self.cfg.coords.kernel_size if self.cfg.coords.enabled else 3

        if in_channels == 0:
            self.input = GenInput(self.cfg.input, out_channels, w_dim)
            conv1_in_channels = self.input.total_dim
        else:
            if self.cfg.coords.enabled and (not self.cfg.coords.per_resolution or self.resolution > self.input_resolution):
                assert self.architecture != 'resnet'
                self.coord_fuser = CoordFuser(self.cfg.coords, self.w_dim, self.resolution)
                conv0_in_channels = in_channels + self.coord_fuser.total_dim
            else:
                self.coord_fuser = None
                conv0_in_channels = in_channels

            up_for_conv0 = self.up if self.cfg.upsampling_mode is None else 1
            self.conv0 = SynthesisLayer(conv0_in_channels, out_channels, w_dim=w_dim, resolution=self.resolution, up=up_for_conv0,
                resample_filter=resample_filter, conv_clamp=conv_clamp, channels_last=self.channels_last,
                kernel_size=kernel_size, cfg=cfg, **layer_kwargs)
            self.num_conv += 1
            conv1_in_channels = out_channels

        self.conv1 = SynthesisLayer(conv1_in_channels, out_channels, w_dim=w_dim, resolution=self.resolution,
            conv_clamp=conv_clamp, channels_last=self.channels_last, kernel_size=kernel_size, cfg=cfg,
            instance_norm=(in_channels > 0 and cfg.get('fmm', {}).get('instance_norm', False)), **layer_kwargs)
        self.num_conv += 1

        if self.cfg.get('num_extra_convs', {}).get(str(self.resolution), 0) > 0:
            assert self.architecture != 'resnet', "Not implemented for resnet"
            self.extra_convs = nn.ModuleList([
                SynthesisLayer(out_channels, out_channels, w_dim=w_dim, resolution=self.resolution,
                    conv_clamp=conv_clamp, channels_last=self.channels_last, kernel_size=kernel_size,
                    instance_norm=cfg.get('fmm', {}).get('instance_norm', False), cfg=cfg, **layer_kwargs)
                    for _ in range(self.cfg.num_extra_convs[str(self.resolution)])])
            self.num_conv += len(self.extra_convs)
        else:
            self.extra_convs = None

        if is_last or architecture == 'skip':
            self.torgb = ToRGBLayer(out_channels, img_channels, w_dim=w_dim,
                conv_clamp=conv_clamp, channels_last=self.channels_last)
            self.num_torgb += 1

        if in_channels != 0 and architecture == 'resnet':
            self.skip = Conv2dLayer(in_channels, out_channels, kernel_size=1, bias=False, up=self.up,
                resample_filter=resample_filter, channels_last=self.channels_last)

    def forward(self, x, img, ws, force_fp32=False, fused_modconv=None, **layer_kwargs):
        misc.assert_shape(ws, [None, self.num_conv + self.num_torgb, self.w_dim])
        w_iter = iter(ws.unbind(dim=1))
        dtype = torch.float16 if self.use_fp16 and not force_fp32 else torch.float32
        memory_format = torch.channels_last if self.channels_last and not force_fp32 else torch.contiguous_format

        if fused_modconv is None:
            with misc.suppress_tracer_warnings(): # this value will be treated as a constant
                fused_modconv = (not self.training) and (dtype == torch.float32 or (isinstance(x, Tensor) and int(x.shape[0]) == 1))

        # Input.
        if self.in_channels == 0:
            conv1_w = next(w_iter)
            x = self.input(ws.shape[0], conv1_w, device=ws.device, dtype=dtype, memory_format=memory_format)
        else:
            misc.assert_shape(x, [None, self.in_channels, self.input_resolution, self.input_resolution])
            x = x.to(dtype=dtype, memory_format=memory_format)

        x = maybe_upsample(x, self.cfg.upsampling_mode, self.up)

        # Main layers.
        if self.in_channels == 0:
            x = self.conv1(x, conv1_w, fused_modconv=fused_modconv, **layer_kwargs)
        elif self.architecture == 'resnet':
            y = self.skip(x, gain=np.sqrt(0.5))
            x = self.conv0(x, next(w_iter), fused_modconv=fused_modconv, **layer_kwargs)
            x = self.conv1(x, next(w_iter), fused_modconv=fused_modconv, gain=np.sqrt(0.5), **layer_kwargs)
            x = y.add_(x)
        else:
            conv0_w = next(w_iter)

            if self.coord_fuser is not None:
                x = self.coord_fuser(x, conv0_w, dtype=dtype, memory_format=memory_format)

            x = self.conv0(x, conv0_w, fused_modconv=fused_modconv, **layer_kwargs)
            x = self.conv1(x, next(w_iter), fused_modconv=fused_modconv, **layer_kwargs)

        if not self.extra_convs is None:
            for conv, w in zip(self.extra_convs, w_iter):
                x = conv(x, w, fused_modconv=fused_modconv, **layer_kwargs)

        # ToRGB.
        if img is not None:
            misc.assert_shape(img, [None, self.img_channels, self.input_resolution, self.input_resolution])

            if self.up == 2:
                if self.cfg.upsampling_mode is None:
                    img = upfirdn2d.upsample2d(img, self.resample_filter)
                else:
                    img = maybe_upsample(img, self.cfg.upsampling_mode, 2)

        if self.is_last or self.architecture == 'skip':
            y = self.torgb(x, next(w_iter), fused_modconv=fused_modconv)
            y = y.to(dtype=torch.float32, memory_format=torch.contiguous_format)
            img = img.add_(y) if img is not None else y

        assert x.dtype == dtype
        assert img is None or img.dtype == torch.float32
        return x, img

#----------------------------------------------------------------------------

@persistence.persistent_class
class SynthesisNetwork(torch.nn.Module):
    def __init__(self,
        w_dim,                      # Intermediate latent (W) dimensionality.
        img_resolution,             # Output image resolution.
        img_channels,               # Number of color channels.
        channel_base    = 32768,    # Overall multiplier for the number of channels.
        channel_max     = 512,      # Maximum number of channels in any layer.
        num_fp16_res    = 0,        # Use FP16 for the N highest resolutions.
        cfg             = {},       # Additional config
        **block_kwargs,             # Arguments for SynthesisBlock.
    ):
        assert img_resolution >= 4 and img_resolution & (img_resolution - 1) == 0
        super().__init__()

        self.w_dim = w_dim
        self.cfg = OmegaConf.create(cfg)
        self.img_resolution = img_resolution
        self.img_resolution_log2 = int(np.log2(img_resolution))
        self.img_channels = img_channels
        self.block_resolutions = [2 ** i for i in range(2, self.img_resolution_log2 + 1)]
        channels_dict = {res: min(channel_base // res, channel_max) for res in self.block_resolutions}
        fp16_resolution = max(2 ** (self.img_resolution_log2 + 1 - num_fp16_res), 8)

        self.num_ws = 0
        for res in self.block_resolutions:
            in_channels = channels_dict[res // 2] if res > 4 else 0
            out_channels = channels_dict[res]
            use_fp16 = (res >= fp16_resolution)
            is_last = (res == self.img_resolution)
            block = SynthesisBlock(in_channels, out_channels, w_dim=w_dim, resolution=res,
                img_channels=img_channels, is_last=is_last, use_fp16=use_fp16, cfg=cfg, **block_kwargs)
            self.num_ws += block.num_conv

            if is_last:
                self.num_ws += block.num_torgb
            setattr(self, f'b{res}', block)

    def forward(self, ws, **block_kwargs):
        block_ws = []
        with torch.autograd.profiler.record_function('split_ws'):
            misc.assert_shape(ws, [None, self.num_ws, self.w_dim])
            ws = ws.to(torch.float32)
            w_idx = 0
            for res in self.block_resolutions:
                block = getattr(self, f'b{res}')
                block_ws.append(ws.narrow(1, w_idx, block.num_conv + block.num_torgb))
                w_idx += block.num_conv

        x = img = None
        for res, cur_ws in zip(self.block_resolutions, block_ws):
            block = getattr(self, f'b{res}')
            x, img = block(x, img, cur_ws, **block_kwargs)
        return img

#----------------------------------------------------------------------------

@persistence.persistent_class
class NerfSynthesisNetwork(torch.nn.Module):
    def __init__(self,
        w_dim,                      # Intermediate latent (W) dimensionality.
        img_resolution,             # Output image resolution.
        cfg             = {},       # Additional config
        **block_kwargs,             # Arguments for SynthesisBlock.
    ):
        assert img_resolution >= 4 and img_resolution & (img_resolution - 1) == 0
        super().__init__()

        self.cfg = OmegaConf.create(cfg)
        self.img_resolution = img_resolution

        self.w_dim = w_dim
        self.D = self.cfg.num_layers
        self.W = self.cfg.width
        self.height = img_resolution
        self.width = img_resolution

        embed_fn, input_ch = get_embedder(multires=10, i=0)
        self.embed_fn = embed_fn
        self.input_ch = input_ch
        self.input_ch_views = 0
        self.skips = [4]
        self.use_viewdirs = False

        self.pts_linears = nn.ModuleList(
            [NerfSynthesisLayer(in_channels=self.input_ch,
                                out_channels=self.W,
                                w_dim=self.w_dim,
                                activation='relu',
                                cfg=cfg)] + [
            NerfSynthesisLayer(in_channels=self.W,
                               out_channels=self.W,
                               w_dim=self.w_dim,
                               activation='relu',
                               cfg=cfg)
            if i not in self.skips else
            NerfSynthesisLayer(in_channels=self.W + self.input_ch,
                               out_channels=self.W,
                               w_dim=self.w_dim,
                               activation='relu',
                               cfg=cfg)
            for i in range(self.D - 1)])

        if self.use_viewdirs:
            raise NotImplementedError("Viewing directions are not supported")
            ### Implementation according to the official code release (https://github.com/bmild/nerf/blob/master/run_nerf_helpers.py#L104-L105)
            self.views_linears = nn.ModuleList([nn.Linear(self.input_ch_views + W, W // 2)])

            ### Implementation according to the paper
            # self.views_linears = nn.ModuleList(
            #     [nn.Linear(input_ch_views + W, W//2)] + [nn.Linear(W//2, W//2) for i in range(D//2)])

            # self.feature_linear = nn.Linear(self.width, self.width)
            # self.alpha_linear = nn.Linear(self.width, 1)
            # self.rgb_linear = nn.Linear(self.width // 2, 3)
        else:
            self.output_linear = NerfSynthesisLayer(in_channels=self.W,
                                                    out_channels=4,
                                                    w_dim=self.w_dim,
                                                    activation='linear',
                                                    cfg=cfg)
        self.network_query_fn = partial(run_network, embed_fn=self.embed_fn, embeddirs_fn=None)

        self.fov = 0.6911112070083618
        focal = .5 * self.width / np.tan(.5 * self.fov)
        self.K = np.array([
            [focal, 0, 0.5 * self.width],
            [0, focal, 0.5 * self.height],
            [0, 0, 1]
        ])  # camera matrix

        self.render_kwargs = {
            'network_query_fn': self.network_query_fn,
            'perturb': self.cfg.perturb,
            'ndc': False,
            'N_samples': self.cfg.n_samples,
            'N_importance': self.cfg.n_importance,
            'use_viewdirs': False,
            'white_bkgd': self.cfg.white_bkgd,
            'raw_noise_std': self.cfg.raw_noise_std,
        }
        self.index_channel = False
        self.first_pass = True  # Hack for fixing high memory usage during first pass

    def forward(self,
                ws: torch.Tensor,
                poses: Optional[List[torch.Tensor]]=None,
                scale: bool=True,
                crop: bool=False,
                perturb: bool=False,
                **kwargs):

        if poses is None:
            if self.cfg.use_normal:
                poses = [pose_spherical(theta=np.random.normal(0, self.cfg.theta_std),
                                        phi=90 - uniform_circle(low=90 - self.cfg.phi_high, high=90 - self.cfg.phi_low),
                                        radius=4.0)
                         for _ in range(ws.shape[0])]
            else:
                poses = [pose_spherical(theta=np.random.uniform(low=self.cfg.theta_low, high=self.cfg.theta_high),
                                        phi=90 - uniform_circle(low=90 - self.cfg.phi_high, high=90 - self.cfg.phi_low),
                                        radius=4.0)
                         for _ in range(ws.shape[0])]

        if self.first_pass:
            self.render_kwargs['N_samples'] = 2
            self.render_kwargs['N_importance'] = 0

        if crop and self.cfg.patch_size is not None:
            self.render_kwargs['patch_size'] = self.cfg.patch_size
        else:
            self.render_kwargs['patch_size'] = None
        if perturb:
            self.render_kwargs['perturb'] = self.cfg.perturb
            self.render_kwargs['raw_noise_std'] = self.cfg.raw_noise_std
        else:
            self.render_kwargs['perturb'] = 0.0
            self.render_kwargs['raw_noise_std'] = 0.0

        rgbs = []
        for i, c2w in enumerate(poses):
            self.current_ws = ws.narrow(dim=0, start=i, length=1)  # value used in `forward_rays`
            rgb, _, _, _ = render(self.height,
                                  self.width,
                                  self.K,
                                  near=4.0 - self.cfg.render_size / 2,
                                  far=4.0 +  self.cfg.render_size / 2,
                                  c2w=c2w[:3, :4],
                                  network_fn=self.forward_rays,
                                  index_channel=self.index_channel,
                                  **self.render_kwargs)

            rgbs.append(rgb)

        rgbs = torch.stack(rgbs).permute((0, 3, 1, 2))
        if scale:
            rgbs = (rgbs - 0.5) * 2  # Rescale to [-1, 1]

        if self.first_pass:
            self.render_kwargs['N_samples'] = self.cfg.n_samples
            self.render_kwargs['N_importance'] = self.cfg.n_importance
            self.first_pass = False

        return rgbs

    def forward_rays(self, x):
        input_pts, input_views = torch.split(x, [self.input_ch, self.input_ch_views], dim=-1)
        input_pts = input_pts.to(device=self.current_ws.device)
        h = input_pts
        for i, l in enumerate(self.pts_linears):
            h = self.pts_linears[i](h, self.current_ws)

            if i in self.skips:
                h = torch.cat([input_pts, h], -1)

        if self.use_viewdirs:
            alpha = self.alpha_linear(h)
            feature = self.feature_linear(h)
            h = torch.cat([feature, input_views], -1)

            for i, l in enumerate(self.views_linears):
                h = self.views_linears[i](h)

            rgb = self.rgb_linear(h)
            outputs = torch.cat([rgb, alpha], -1)
        else:
            outputs = self.output_linear(h, self.current_ws)

        return outputs

#----------------------------------------------------------------------------

@persistence.persistent_class
class Generator(torch.nn.Module):
    def __init__(self,
        z_dim,                      # Input latent (Z) dimensionality.
        c_dim,                      # Conditioning label (C) dimensionality.
        w_dim,                      # Intermediate latent (W) dimensionality.
        img_resolution,             # Output resolution.
        img_channels,               # Number of output color channels.
        mapping_kwargs      = {},   # Arguments for MappingNetwork.
        synthesis_kwargs    = {},   # Arguments for SynthesisNetwork.
        cfg                 = {},   # Config
    ):
        super().__init__()
        self.z_dim = z_dim
        self.c_dim = c_dim
        self.w_dim = w_dim
        self.img_resolution = img_resolution
        self.img_channels = img_channels
        self.synthesis = SynthesisNetwork(w_dim=w_dim, img_resolution=img_resolution, img_channels=img_channels, cfg=cfg, **synthesis_kwargs)
        self.num_ws = self.synthesis.num_ws
        self.mapping = MappingNetwork(z_dim=z_dim, c_dim=c_dim, w_dim=w_dim, num_ws=self.num_ws, **mapping_kwargs)

    def forward(self, z, c, truncation_psi=1, truncation_cutoff=None, **synthesis_kwargs):
        ws = self.mapping(z, c, truncation_psi=truncation_psi, truncation_cutoff=truncation_cutoff)
        img = self.synthesis(ws, **synthesis_kwargs)
        return img


#----------------------------------------------------------------------------

@persistence.persistent_class
class NeRFGenerator(torch.nn.Module):
    def __init__(self,
        z_dim,                      # Input latent (Z) dimensionality.
        c_dim,                      # Conditioning label (C) dimensionality.
        w_dim,                      # Intermediate latent (W) dimensionality.
        img_resolution,             # Output resolution.
        img_channels,               # Number of output color channels.
        mapping_kwargs      = {},   # Arguments for MappingNetwork.
        synthesis_kwargs    = {},   # Arguments for SynthesisNetwork.
        cfg                 = {},   # Config
    ):
        super().__init__()
        self.z_dim = z_dim
        self.c_dim = c_dim
        self.w_dim = w_dim
        self.img_resolution = img_resolution
        self.synthesis = NerfSynthesisNetwork(w_dim=w_dim, img_resolution=img_resolution, cfg=cfg, **synthesis_kwargs)
        self.mapping = MappingNetwork(z_dim=z_dim, c_dim=c_dim, w_dim=w_dim, num_ws=None, w_avg_beta=None, **mapping_kwargs) # todo: w_avg_beta shouldn't be here but it's absence breaks code something

    def forward(self, z, c, truncation_psi=1, truncation_cutoff=None, **synthesis_kwargs):
        ws = self.mapping(z, c, truncation_psi=truncation_psi, truncation_cutoff=truncation_cutoff)
        img = self.synthesis(ws, **synthesis_kwargs)
        return img


#----------------------------------------------------------------------------

@persistence.persistent_class
class DiscriminatorBlock(torch.nn.Module):
    def __init__(self,
        in_channels,                        # Number of input channels, 0 = first block.
        tmp_channels,                       # Number of intermediate channels.
        out_channels,                       # Number of output channels.
        resolution,                         # Resolution of this block.
        img_channels,                       # Number of input color channels.
        first_layer_idx,                    # Index of the first layer.
        architecture        = 'resnet',     # Architecture: 'orig', 'skip', 'resnet'.
        activation          = 'lrelu',      # Activation function: 'relu', 'lrelu', etc.
        resample_filter     = [1,3,3,1],    # Low-pass filter to apply when resampling activations.
        conv_clamp          = None,         # Clamp the output of convolution layers to +-X, None = disable clamping.
        use_fp16            = False,        # Use FP16 for this block?
        fp16_channels_last  = False,        # Use channels-last memory format with FP16?
        freeze_layers       = 0,            # Freeze-D: Number of layers to freeze.
    ):
        assert in_channels in [0, tmp_channels]
        assert architecture in ['orig', 'skip', 'resnet']
        super().__init__()
        self.in_channels = in_channels
        self.resolution = resolution
        self.img_channels = img_channels
        self.first_layer_idx = first_layer_idx
        self.architecture = architecture
        self.use_fp16 = use_fp16
        self.channels_last = (use_fp16 and fp16_channels_last)
        self.register_buffer('resample_filter', upfirdn2d.setup_filter(resample_filter))

        self.num_layers = 0
        def trainable_gen():
            while True:
                layer_idx = self.first_layer_idx + self.num_layers
                trainable = (layer_idx >= freeze_layers)
                self.num_layers += 1
                yield trainable
        trainable_iter = trainable_gen()

        if in_channels == 0 or architecture == 'skip':
            self.fromrgb = Conv2dLayer(img_channels, tmp_channels, kernel_size=1, activation=activation,
                trainable=next(trainable_iter), conv_clamp=conv_clamp, channels_last=self.channels_last)

        self.conv0 = Conv2dLayer(tmp_channels, tmp_channels, kernel_size=3, activation=activation,
            trainable=next(trainable_iter), conv_clamp=conv_clamp, channels_last=self.channels_last)

        self.conv1 = Conv2dLayer(tmp_channels, out_channels, kernel_size=3, activation=activation, down=2,
            trainable=next(trainable_iter), resample_filter=resample_filter, conv_clamp=conv_clamp, channels_last=self.channels_last)

        if architecture == 'resnet':
            self.skip = Conv2dLayer(tmp_channels, out_channels, kernel_size=1, bias=False, down=2,
                trainable=next(trainable_iter), resample_filter=resample_filter, channels_last=self.channels_last)

    def forward(self, x, img, force_fp32=False):
        dtype = torch.float16 if self.use_fp16 and not force_fp32 else torch.float32
        memory_format = torch.channels_last if self.channels_last and not force_fp32 else torch.contiguous_format

        # Input.
        if x is not None:
            misc.assert_shape(x, [None, self.in_channels, self.resolution, self.resolution])
            x = x.to(dtype=dtype, memory_format=memory_format)

        # FromRGB.
        if self.in_channels == 0 or self.architecture == 'skip':
            misc.assert_shape(img, [None, self.img_channels, self.resolution, self.resolution])
            img = img.to(dtype=dtype, memory_format=memory_format)
            y = self.fromrgb(img)
            x = x + y if x is not None else y
            img = upfirdn2d.downsample2d(img, self.resample_filter) if self.architecture == 'skip' else None

        # Main layers.
        if self.architecture == 'resnet':
            y = self.skip(x, gain=np.sqrt(0.5))
            x = self.conv0(x)
            x = self.conv1(x, gain=np.sqrt(0.5))
            x = y.add_(x)
        else:
            x = self.conv0(x)
            x = self.conv1(x)

        assert x.dtype == dtype
        return x, img

#----------------------------------------------------------------------------

@persistence.persistent_class
class MinibatchStdLayer(torch.nn.Module):
    def __init__(self, group_size, num_channels=1):
        super().__init__()
        self.group_size = group_size
        self.num_channels = num_channels

    def forward(self, x):
        N, C, H, W = x.shape
        with misc.suppress_tracer_warnings(): # as_tensor results are registered as constants
            G = torch.min(torch.as_tensor(self.group_size), torch.as_tensor(N)) if self.group_size is not None else N
        F = self.num_channels
        c = C // F

        y = x.reshape(G, -1, F, c, H, W)    # [GnFcHW] Split minibatch N into n groups of size G, and channels C into F groups of size c.
        y = y - y.mean(dim=0)               # [GnFcHW] Subtract mean over group.
        y = y.square().mean(dim=0)          # [nFcHW]  Calc variance over group.
        y = (y + 1e-8).sqrt()               # [nFcHW]  Calc stddev over group.
        y = y.mean(dim=[2,3,4])             # [nF]     Take average over channels and pixels.
        y = y.reshape(-1, F, 1, 1)          # [nF11]   Add missing dimensions.
        y = y.repeat(G, 1, H, W)            # [NFHW]   Replicate over group and pixels.
        x = torch.cat([x, y], dim=1)        # [NCHW]   Append to input as new channels.
        return x

#----------------------------------------------------------------------------

@persistence.persistent_class
class DiscriminatorEpilogue(torch.nn.Module):
    def __init__(self,
        in_channels,                    # Number of input channels.
        cmap_dim,                       # Dimensionality of mapped conditioning label, 0 = no label.
        resolution,                     # Resolution of this block.
        img_channels,                   # Number of input color channels.
        architecture        = 'resnet', # Architecture: 'orig', 'skip', 'resnet'.
        mbstd_group_size    = 4,        # Group size for the minibatch standard deviation layer, None = entire minibatch.
        mbstd_num_channels  = 1,        # Number of features for the minibatch standard deviation layer, 0 = disable.
        activation          = 'lrelu',  # Activation function: 'relu', 'lrelu', etc.
        conv_clamp          = None,     # Clamp the output of convolution layers to +-X, None = disable clamping.
    ):
        assert architecture in ['orig', 'skip', 'resnet']
        super().__init__()
        self.in_channels = in_channels
        self.cmap_dim = cmap_dim
        self.resolution = resolution
        self.img_channels = img_channels
        self.architecture = architecture

        if architecture == 'skip':
            self.fromrgb = Conv2dLayer(img_channels, in_channels, kernel_size=1, activation=activation)
        self.mbstd = MinibatchStdLayer(group_size=mbstd_group_size, num_channels=mbstd_num_channels) if mbstd_num_channels > 0 else None
        self.conv = Conv2dLayer(in_channels + mbstd_num_channels, in_channels, kernel_size=3, activation=activation, conv_clamp=conv_clamp)
        self.fc = FullyConnectedLayer(in_channels * (resolution ** 2), in_channels, activation=activation)
        self.out = FullyConnectedLayer(in_channels, 1 if cmap_dim == 0 else cmap_dim)

    def forward(self, x, img, cmap, force_fp32=False):
        misc.assert_shape(x, [None, self.in_channels, self.resolution, self.resolution]) # [NCHW]
        _ = force_fp32 # unused
        dtype = torch.float32
        memory_format = torch.contiguous_format

        # FromRGB.
        x = x.to(dtype=dtype, memory_format=memory_format)
        if self.architecture == 'skip':
            misc.assert_shape(img, [None, self.img_channels, self.resolution, self.resolution])
            img = img.to(dtype=dtype, memory_format=memory_format)
            x = x + self.fromrgb(img)

        # Main layers.
        if self.mbstd is not None:
            x = self.mbstd(x)
        x = self.conv(x)
        x = self.fc(x.flatten(1))
        x = self.out(x)

        # Conditioning.
        if self.cmap_dim > 0:
            misc.assert_shape(cmap, [None, self.cmap_dim])
            x = (x * cmap).sum(dim=1, keepdim=True) * (1 / np.sqrt(self.cmap_dim))

        assert x.dtype == dtype
        return x

#----------------------------------------------------------------------------

@persistence.persistent_class
class Discriminator(torch.nn.Module):
    def __init__(self,
        c_dim,                          # Conditioning label (C) dimensionality.
        img_resolution,                 # Input resolution. Ignored if 'patch_size' is not None.
        img_channels,                   # Number of input color channels.
        patch_size,                     # Overrides img_resolution.
        architecture        = 'resnet', # Architecture: 'orig', 'skip', 'resnet'.
        channel_base        = 32768,    # Overall multiplier for the number of channels.
        channel_max         = 512,      # Maximum number of channels in any layer.
        num_fp16_res        = 0,        # Use FP16 for the N highest resolutions.
        conv_clamp          = None,     # Clamp the output of convolution layers to +-X, None = disable clamping.
        cmap_dim            = None,     # Dimensionality of mapped conditioning label, None = default.
        block_kwargs        = {},       # Arguments for DiscriminatorBlock.
        mapping_kwargs      = {},       # Arguments for MappingNetwork.
        epilogue_kwargs     = {},       # Arguments for DiscriminatorEpilogue.
    ):
        super().__init__()
        self.c_dim = c_dim
        if patch_size is not None:
            self.img_resolution = patch_size
        else:
            self.img_resolution = img_resolution

        self.img_resolution_log2 = int(np.log2(self.img_resolution))

        self.img_channels = img_channels
        self.block_resolutions = [2 ** i for i in range(self.img_resolution_log2, 2, -1)]
        channels_dict = {res: min(channel_base // res, channel_max)
                         for res in self.block_resolutions + [4]}
        fp16_resolution = max(2 ** (self.img_resolution_log2 + 1 - num_fp16_res), 8)

        if cmap_dim is None:
            cmap_dim = channels_dict[4]
        if c_dim == 0:
            cmap_dim = 0

        common_kwargs = dict(img_channels=img_channels,
                             architecture=architecture,
                             conv_clamp=conv_clamp)
        cur_layer_idx = 0
        for res in self.block_resolutions:
            in_channels = channels_dict[res] if res < self.img_resolution else 0
            tmp_channels = channels_dict[res]
            out_channels = channels_dict[res // 2]
            use_fp16 = (res >= fp16_resolution)
            block = DiscriminatorBlock(in_channels, tmp_channels, out_channels, resolution=res,
                first_layer_idx=cur_layer_idx, use_fp16=use_fp16, **block_kwargs, **common_kwargs)
            setattr(self, f'b{res}', block)
            cur_layer_idx += block.num_layers
        if c_dim > 0:
            self.mapping = MappingNetwork(z_dim=0, c_dim=c_dim, w_dim=cmap_dim, num_ws=None,
                                          w_avg_beta=None, **mapping_kwargs)
        self.b4 = DiscriminatorEpilogue(channels_dict[4], cmap_dim=cmap_dim, resolution=4,
                                        **epilogue_kwargs, **common_kwargs)

    def forward(self, img, c, **block_kwargs):
        x = None
        for res in self.block_resolutions:
            block = getattr(self, f'b{res}')
            x, img = block(x, img, **block_kwargs)

        cmap = None
        if self.c_dim > 0:
            cmap = self.mapping(None, c)
        x = self.b4(x, img, cmap)
        return x

#----------------------------------------------------------------------------
