import importlib
import numbers

import torch
from torch.nn.parameter import Parameter
from torch.nn import init
from torch.nn import functional as F
from typing import List, Tuple

from apex._autocast_utils import _cast_if_autocast_enabled

global fused_layer_norm_cuda
fused_layer_norm_cuda = None


# PyTorch supports `torch.library.custom_op` since 2.4.0.
def supports_custom_op() -> bool:
    return hasattr(torch.library, "custom_op")


# Reference implementation from Huggingface
def manual_rms_norm(input, normalized_shape, weight, eps):
    # layer norm should always be calculated in float32
    dims = tuple(i for i in range(-1, -len(normalized_shape)-1, -1))
    variance = input.to(torch.float32).pow(2).mean(dims, keepdim=True)
    input = input * torch.rsqrt(variance + eps)

    if weight is None:
        return input

    # convert into half-precision if necessary
    if weight.dtype in [torch.float16, torch.bfloat16]:
        input = input.to(weight.dtype)

    return weight * input


class FusedLayerNormAffineFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input, weight, bias, normalized_shape, eps, memory_efficient=False):
        global fused_layer_norm_cuda
        if fused_layer_norm_cuda is None:
            fused_layer_norm_cuda = importlib.import_module("fused_layer_norm_cuda")
        ctx.normalized_shape = normalized_shape
        ctx.eps = eps
        ctx.memory_efficient = memory_efficient
        input_ = input.contiguous()
        weight_ = weight.contiguous()
        bias_ = bias.contiguous()
        output, mean, invvar = fused_layer_norm_cuda.forward_affine(
            input_, ctx.normalized_shape, weight_, bias_, ctx.eps
        )
        if ctx.memory_efficient:
            ctx.save_for_backward(output, weight_, bias_, None, invvar)
        else:
            ctx.save_for_backward(input_, weight_, bias_, mean, invvar)
        return output

    @staticmethod
    def backward(ctx, grad_output):
        input_or_output, weight_, bias_, mean, invvar = ctx.saved_tensors
        grad_input = grad_weight = grad_bias = None
        grad_input, grad_weight, grad_bias = fused_layer_norm_cuda.backward_affine(
            grad_output.contiguous(), mean, invvar, input_or_output,
            ctx.normalized_shape, weight_, bias_, ctx.eps, ctx.memory_efficient
        )
        return grad_input, grad_weight, grad_bias, None, None, None


if supports_custom_op():

    @torch.library.custom_op("apex::fused_layer_norm_affine_fwd", mutates_args=())
    def fused_layer_norm_affine_fwd(
        input: torch.Tensor,
        weight: torch.Tensor,
        bias: torch.Tensor,
        normalized_shape: List[int],
        eps: float,
        memory_efficient: bool = False,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        global fused_layer_norm_cuda
        if fused_layer_norm_cuda is None:
            fused_layer_norm_cuda = importlib.import_module("fused_layer_norm_cuda")

        input_ = input.contiguous()
        weight_ = weight.contiguous()
        bias_ = bias.contiguous()
        output, mean, invvar = fused_layer_norm_cuda.forward_affine(
            input_, normalized_shape, weight_, bias_, eps
        )
        return output, mean, invvar

    @fused_layer_norm_affine_fwd.register_fake
    def fused_layer_norm_affine_fwd_fake(
        input: torch.Tensor,
        weight: torch.Tensor,
        bias: torch.Tensor,
        normalized_shape: List[int],
        eps: float,
        memory_efficient: bool = False,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        input = input.contiguous()
        weight = weight.contiguous()
        bias = bias.contiguous()
        idiff = input.ndim - len(normalized_shape)
        n = 1
        for i in range(idiff):
            n *= input.shape[i]
        if input.dtype in [torch.float16, torch.bfloat16]:
            dtype = torch.float32
        else:
            dtype = input.dtype
        mean = torch.empty([n], dtype=dtype, device=input.device)
        invvar = torch.empty_like(mean)
        return torch.empty_like(input), mean, invvar

    @torch.library.custom_op("apex::fused_layer_norm_affine_bwd", mutates_args=())
    def fused_layer_norm_affine_bwd(
        grad_output: torch.Tensor,
        mean: torch.Tensor,
        invvar: torch.Tensor,
        input_or_output: torch.Tensor,
        normalized_shape: List[int],
        weight: torch.Tensor,
        bias: torch.Tensor,
        eps: float,
        memory_efficient: bool = False,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        grad_input, grad_weight, grad_bias = fused_layer_norm_cuda.backward_affine(
            grad_output.contiguous(),
            mean,
            invvar,
            input_or_output,
            normalized_shape,
            weight,
            bias,
            eps,
            memory_efficient,
        )
        return grad_input, grad_weight, grad_bias

    @fused_layer_norm_affine_bwd.register_fake
    def fused_layer_norm_affine_bwd_fake(
        grad_output: torch.Tensor,
        mean: torch.Tensor,
        invvar: torch.Tensor,
        input_or_output: torch.Tensor,
        normalized_shape: List[int],
        weight: torch.Tensor,
        bias: torch.Tensor,
        eps: float,
        memory_efficient: bool = False,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        grad_input = torch.empty_like(input_or_output)
        grad_weight = torch.empty_like(weight)
        grad_bias = torch.empty_like(bias)
        return grad_input, grad_weight, grad_bias

    def _fused_layer_norm_affine_backward(ctx, grad_output, grad_mean, grad_invvar):
        input_or_output, weight_, bias_, mean, invvar = ctx.saved_tensors
        grad_input = grad_weight = grad_bias = None
        grad_input, grad_weight, grad_bias = fused_layer_norm_affine_bwd(
            grad_output,
            mean,
            invvar,
            input_or_output,
            ctx.normalized_shape,
            weight_,
            bias_,
            ctx.eps,
            ctx.memory_efficient,
        )
        return grad_input, grad_weight, grad_bias, None, None, None

    def _fused_layer_norm_affine_setup_context(ctx, inputs, output):
        input, weight, bias, normalized_shape, eps, memory_efficient = inputs
        output, mean, invvar = output
        input_ = input.contiguous()
        weight_ = weight.contiguous()
        bias_ = bias.contiguous()
        if memory_efficient:
            ctx.save_for_backward(output, weight_, bias_, None, invvar)
        else:
            ctx.save_for_backward(input_, weight_, bias_, mean, invvar)
        ctx.normalized_shape = normalized_shape
        ctx.eps = eps
        ctx.memory_efficient = memory_efficient

    fused_layer_norm_affine_fwd.register_autograd(
        _fused_layer_norm_affine_backward,
        setup_context=_fused_layer_norm_affine_setup_context,
    )


class FusedRMSNormAffineFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input, weight, normalized_shape, eps, memory_efficient=False):
        global fused_layer_norm_cuda
        if fused_layer_norm_cuda is None:
            fused_layer_norm_cuda = importlib.import_module("fused_layer_norm_cuda")
        ctx.normalized_shape = normalized_shape
        ctx.eps = eps
        ctx.memory_efficient = memory_efficient
        input_ = input.contiguous()
        weight_ = weight.contiguous()
        output, invvar = fused_layer_norm_cuda.rms_forward_affine(
            input_, ctx.normalized_shape, weight_, ctx.eps)
        if ctx.memory_efficient:
            ctx.save_for_backward(output, weight_, invvar)
        else:
            ctx.save_for_backward(input_, weight_, invvar)
        return output

    @staticmethod
    def backward(ctx, grad_output):
        input_or_output, weight_, invvar = ctx.saved_tensors
        grad_input = grad_weight = None
        grad_input, grad_weight = fused_layer_norm_cuda.rms_backward_affine(
           grad_output.contiguous(), invvar, input_or_output,
           ctx.normalized_shape, weight_, ctx.eps, ctx.memory_efficient
        )
        return grad_input, grad_weight, None, None, None

if supports_custom_op():
    @torch.library.custom_op("apex::fused_rms_norm_affine_fwd", mutates_args=())
    def fused_rms_norm_affine_fwd(
        input: torch.Tensor,
        weight: torch.Tensor,
        normalized_shape: List[int],
        eps: float,
        memory_efficient: bool = False,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        global fused_layer_norm_cuda
        if fused_layer_norm_cuda is None:
            fused_layer_norm_cuda = importlib.import_module("fused_layer_norm_cuda")

        input_ = input.contiguous()
        weight_ = weight.contiguous()
        output, invvar = fused_layer_norm_cuda.rms_forward_affine(
            input_, normalized_shape, weight_, eps
        )
        return output, invvar


    @fused_rms_norm_affine_fwd.register_fake
    def fused_rms_norm_affine_fwd_fake(
        input: torch.Tensor,
        weight: torch.Tensor,
        normalized_shape: List[int],
        eps: float,
        memory_efficient: bool = False,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        input = input.contiguous()
        weight = weight.contiguous()
        idiff = input.ndim - len(normalized_shape)
        n = 1
        for i in range(idiff):
            n *= input.shape[i]
        if input.dtype in [torch.float16, torch.bfloat16]:
            dtype = torch.float32
        else:
            dtype = input.dtype
        return (
            torch.empty_like(input),
            torch.empty(
                [n],
                dtype=dtype,
                device=input.device,
                requires_grad=input.requires_grad,
                memory_format=torch.contiguous_format,
            ),
        )


    @torch.library.custom_op("apex::fused_rms_norm_affine_bwd", mutates_args=())
    def fused_rms_norm_affine_bwd(
        grad_output: torch.Tensor,
        invvar: torch.Tensor,
        input_or_output: torch.Tensor,
        normalized_shape: List[int],
        weight: torch.Tensor,
        eps: float,
        memory_efficient: bool = False,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        grad_input, grad_weight = fused_layer_norm_cuda.rms_backward_affine(
            grad_output.contiguous(),
            invvar,
            input_or_output,
            normalized_shape,
            weight,
            eps,
            memory_efficient,
        )
        return grad_input, grad_weight


    @fused_rms_norm_affine_bwd.register_fake
    def fused_rms_norm_affine_bwd_fake(
        grad_output: torch.Tensor,
        invvar: torch.Tensor,
        input_or_output: torch.Tensor,
        normalized_shape: List[int],
        weight: torch.Tensor,
        eps: float,
        memory_efficient: bool = False,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        grad_input = torch.empty_like(input_or_output)
        grad_weight = torch.empty_like(weight)
        return grad_input, grad_weight


    def _fused_rms_norm_affine_backward(ctx, grad_output, grad_invvar):
        input_or_output, weight_, invvar = ctx.saved_tensors
        grad_input = grad_weight = None
        grad_input, grad_weight = fused_rms_norm_affine_bwd(
            grad_output,
            invvar,
            input_or_output,
            ctx.normalized_shape,
            weight_,
            ctx.eps,
            ctx.memory_efficient,
        )
        return grad_input, grad_weight, None, None, None


    def _fused_rms_norm_affine_setup_context(ctx, inputs, output):
        input_, weight_, normalized_shape, eps, memory_efficient = inputs
        output_, invvar = output
        input_ = input_.contiguous()
        weight_ = weight_.contiguous()
        if memory_efficient:
            ctx.save_for_backward(output_, weight_, invvar)
        else:
            ctx.save_for_backward(input_, weight_, invvar)
        ctx.normalized_shape = normalized_shape
        ctx.eps = eps
        ctx.memory_efficient = memory_efficient


    fused_rms_norm_affine_fwd.register_autograd(
        _fused_rms_norm_affine_backward,
        setup_context=_fused_rms_norm_affine_setup_context
    )


class FusedLayerNormAffineMixedDtypesFunction(FusedLayerNormAffineFunction):

    @staticmethod
    def forward(ctx, input, weight, bias, normalized_shape, eps, memory_efficient=False):
        global fused_layer_norm_cuda
        if fused_layer_norm_cuda is None:
            fused_layer_norm_cuda = importlib.import_module("fused_layer_norm_cuda")
        ctx.normalized_shape = normalized_shape
        ctx.eps = eps
        ctx.memory_efficient = memory_efficient
        input_ = input.contiguous()
        weight_ = weight.contiguous()
        bias_ = bias.contiguous()
        output, mean, invvar = fused_layer_norm_cuda.forward_affine_mixed_dtypes(
            input_, ctx.normalized_shape, weight_, bias_, ctx.eps
        )
        if ctx.memory_efficient:
            ctx.save_for_backward(output, weight_, bias_, None, invvar)
        else:
            ctx.save_for_backward(input_, weight_, bias_, mean, invvar)
        return output


class FusedRMSNormAffineMixedDtypesFunction(FusedRMSNormAffineFunction):

    @staticmethod
    def forward(ctx, input, weight, normalized_shape, eps, memory_efficient=False):
        global fused_layer_norm_cuda
        if fused_layer_norm_cuda is None:
            fused_layer_norm_cuda = importlib.import_module("fused_layer_norm_cuda")
        ctx.normalized_shape = normalized_shape
        ctx.eps = eps
        ctx.memory_efficient = memory_efficient
        input_ = input.contiguous()
        weight_ = weight.contiguous()
        output, invvar = fused_layer_norm_cuda.rms_forward_affine_mixed_dtypes(
            input_, ctx.normalized_shape, weight_, ctx.eps
        )
        if ctx.memory_efficient:
            ctx.save_for_backward(output, weight_, invvar)
        else:
            ctx.save_for_backward(input_, weight_, invvar)
        return output


class FusedLayerNormFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input, normalized_shape, eps, memory_efficient=False):
        global fused_layer_norm_cuda
        if fused_layer_norm_cuda is None:
            fused_layer_norm_cuda = importlib.import_module("fused_layer_norm_cuda")
        ctx.normalized_shape = normalized_shape
        ctx.eps = eps
        ctx.memory_efficient = memory_efficient
        input_ = input.contiguous()
        output, mean, invvar = fused_layer_norm_cuda.forward(input_, ctx.normalized_shape, ctx.eps)
        if ctx.memory_efficient:
            ctx.save_for_backward(output, None, invvar)
        else:
            ctx.save_for_backward(input_, mean, invvar)
        return output

    @staticmethod
    def backward(ctx, grad_output):
        input_or_output, mean, invvar = ctx.saved_tensors
        grad_input = fused_layer_norm_cuda.backward(
            grad_output.contiguous(), mean, invvar, input_or_output,
            ctx.normalized_shape, ctx.eps, ctx.memory_efficient
        )
        return grad_input, None, None, None


if supports_custom_op():

    @torch.library.custom_op("apex::fused_layer_norm_fwd", mutates_args=())
    def fused_layer_norm_fwd(
        input: torch.Tensor,
        normalized_shape: List[int],
        eps: float,
        memory_efficient: bool = False,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        global fused_layer_norm_cuda
        if fused_layer_norm_cuda is None:
            fused_layer_norm_cuda = importlib.import_module("fused_layer_norm_cuda")

        input_ = input.contiguous()
        output, mean, invvar = fused_layer_norm_cuda.forward(
            input_, normalized_shape, eps
        )
        return output, mean, invvar

    @fused_layer_norm_fwd.register_fake
    def fused_layer_norm_fwd_fake(
        input: torch.Tensor,
        normalized_shape: List[int],
        eps: float,
        memory_efficient: bool = False,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        input = input.contiguous()
        idiff = input.ndim - len(normalized_shape)
        n = 1
        for i in range(idiff):
            n *= input.shape[i]
        if input.dtype in [torch.float16, torch.bfloat16]:
            dtype = torch.float32
        else:
            dtype = input.dtype
        mean = torch.empty([n], dtype=dtype, device=input.device)
        invvar = torch.empty_like(mean)
        return torch.empty_like(input), mean, invvar

    @torch.library.custom_op("apex::fused_layer_norm_bwd", mutates_args=())
    def fused_layer_norm_bwd(
        grad_output: torch.Tensor,
        mean: torch.Tensor,
        invvar: torch.Tensor,
        input_or_output: torch.Tensor,
        normalized_shape: List[int],
        eps: float,
        memory_efficient: bool = False,
    ) -> torch.Tensor:
        grad_input = fused_layer_norm_cuda.backward(
            grad_output.contiguous(),
            mean,
            invvar,
            input_or_output,
            normalized_shape,
            eps,
            memory_efficient,
        )
        return grad_input

    @fused_layer_norm_bwd.register_fake
    def fused_layer_norm_bwd_fake(
        grad_output: torch.Tensor,
        mean: torch.Tensor,
        invvar: torch.Tensor,
        input_or_output: torch.Tensor,
        normalized_shape: List[int],
        eps: float,
        memory_efficient: bool = False,
    ) -> torch.Tensor:
        grad_input = torch.empty_like(input_or_output)
        return grad_input

    def _fused_layer_norm_backward(ctx, grad_output, grad_mean, grad_invvar):
        input_or_output, mean, invvar = ctx.saved_tensors
        grad_input = fused_layer_norm_bwd(
            grad_output,
            mean,
            invvar,
            input_or_output,
            ctx.normalized_shape,
            ctx.eps,
            ctx.memory_efficient,
        )
        return grad_input, None, None, None

    def _fused_layer_norm_setup_context(ctx, inputs, output):
        input, normalized_shape, eps, memory_efficient = inputs
        output, mean, invvar = output
        input_ = input.contiguous()
        if memory_efficient:
            ctx.save_for_backward(output, None, invvar)
        else:
            ctx.save_for_backward(input_, mean, invvar)
        ctx.normalized_shape = normalized_shape
        ctx.eps = eps
        ctx.memory_efficient = memory_efficient

    fused_layer_norm_fwd.register_autograd(
        _fused_layer_norm_backward,
        setup_context=_fused_layer_norm_setup_context,
    )


class FusedRMSNormFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input, normalized_shape, eps, memory_efficient=False):
        global fused_layer_norm_cuda
        if fused_layer_norm_cuda is None:
            fused_layer_norm_cuda = importlib.import_module("fused_layer_norm_cuda")
        ctx.normalized_shape = normalized_shape
        ctx.eps = eps
        ctx.memory_efficient = memory_efficient
        input_ = input.contiguous()
        output, invvar = fused_layer_norm_cuda.rms_forward(input_, ctx.normalized_shape, ctx.eps)
        if ctx.memory_efficient:
            ctx.save_for_backward(output, invvar)
        else:
            ctx.save_for_backward(input_, invvar)
        return output

    @staticmethod
    def backward(ctx, grad_output):
        input_or_output, invvar = ctx.saved_tensors
        grad_input = None
        grad_input = fused_layer_norm_cuda.rms_backward(
            grad_output.contiguous(), invvar, input_or_output,
            ctx.normalized_shape, ctx.eps, ctx.memory_efficient
        )
        return grad_input, None, None, None


if supports_custom_op():
    @torch.library.custom_op("apex::fused_rms_norm_fwd", mutates_args=())
    def fused_rms_norm_fwd(
        input: torch.Tensor,
        normalized_shape: List[int],
        eps: float,
        memory_efficient: bool = False,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        global fused_layer_norm_cuda
        if fused_layer_norm_cuda is None:
            fused_layer_norm_cuda = importlib.import_module("fused_layer_norm_cuda")

        input_ = input.contiguous()
        output, invvar = fused_layer_norm_cuda.rms_forward(
            input_, normalized_shape, eps
        )
        return output, invvar


    @fused_rms_norm_fwd.register_fake
    def fused_rms_norm_fwd_fake(
        input: torch.Tensor,
        normalized_shape: List[int],
        eps: float,
        memory_efficient: bool = False,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        input = input.contiguous()
        idiff = input.ndim - len(normalized_shape)
        n = 1
        for i in range(idiff):
            n *= input.shape[i]
        if input.dtype in [torch.float16, torch.bfloat16]:
            dtype = torch.float32
        else:
            dtype = input.dtype
        return (
            torch.empty_like(input),
            torch.empty(
                [n],
                dtype=dtype,
                device=input.device,
                requires_grad=input.requires_grad,
                memory_format=torch.contiguous_format,
            ),
        )


    @torch.library.custom_op("apex::fused_rms_norm_bwd", mutates_args=())
    def fused_rms_norm_bwd(
        grad_output: torch.Tensor,
        invvar: torch.Tensor,
        input_or_output: torch.Tensor,
        normalized_shape: List[int],
        eps: float,
        memory_efficient: bool = False,
    ) -> torch.Tensor:
        grad_input = fused_layer_norm_cuda.rms_backward(
            grad_output.contiguous(),
            invvar,
            input_or_output,
            normalized_shape,
            eps,
            memory_efficient,
        )
        return grad_input


    @fused_rms_norm_bwd.register_fake
    def fused_rms_norm_bwd_fake(
        grad_output: torch.Tensor,
        invvar: torch.Tensor,
        input_or_output: torch.Tensor,
        normalized_shape: List[int],
        eps: float,
        memory_efficient: bool = False,
    ) -> torch.Tensor:
        grad_input = torch.empty_like(input_or_output)
        return grad_input


    def _fused_rms_norm_backward(ctx, grad_output, grad_invvar):
        input_or_output, invvar = ctx.saved_tensors
        grad_input = None
        grad_input = fused_rms_norm_bwd(
            grad_output,
            invvar,
            input_or_output,
            ctx.normalized_shape,
            ctx.eps,
            ctx.memory_efficient,
        )
        return grad_input, None, None, None


    def _fused_rms_norm_setup_context(ctx, inputs, output):
        input_, normalized_shape, eps, memory_efficient = inputs
        output_, invvar = output
        input_ = input_.contiguous()
        if memory_efficient:
            ctx.save_for_backward(output_, invvar)
        else:
            ctx.save_for_backward(input_, invvar)
        ctx.normalized_shape = normalized_shape
        ctx.eps = eps
        ctx.memory_efficient = memory_efficient


    fused_rms_norm_fwd.register_autograd(
        _fused_rms_norm_backward,
        setup_context=_fused_rms_norm_setup_context
    )


def fused_layer_norm_affine(input, weight, bias, normalized_shape, eps=1e-6, memory_efficient=False):
    args = _cast_if_autocast_enabled(input, weight, bias, normalized_shape, eps, memory_efficient)
    with torch.amp.autocast('cuda', enabled=False):
        if supports_custom_op():
            return fused_layer_norm_affine_fwd(*args)[0]
        else:
            return FusedLayerNormAffineFunction.apply(*args)


def fused_layer_norm(input, normalized_shape, eps=1e-6, memory_efficient=False):
    args = _cast_if_autocast_enabled(input, normalized_shape, eps, memory_efficient)
    with torch.amp.autocast('cuda', enabled=False):
        if supports_custom_op():
            return fused_layer_norm_fwd(*args)[0]
        else:
            return FusedLayerNormFunction.apply(*args)


def mixed_dtype_fused_layer_norm_affine(input, weight, bias, normalized_shape, eps=1e-6, memory_efficient=False):
    args = _cast_if_autocast_enabled(input, weight, bias, normalized_shape, eps, memory_efficient)
    with torch.amp.autocast('cuda', enabled=False):
        return FusedLayerNormAffineMixedDtypesFunction.apply(*args)


def fused_rms_norm_affine(input, weight, normalized_shape, eps=1e-6, memory_efficient=False):
    args = _cast_if_autocast_enabled(input, weight, normalized_shape, eps, memory_efficient)
    with torch.amp.autocast('cuda', enabled=False):
        if supports_custom_op():
            return fused_rms_norm_affine_fwd(*args)[0]
        else:
            return FusedRMSNormAffineFunction.apply(*args)


def fused_rms_norm(input, normalized_shape, eps=1e-6, memory_efficient=False):
    args = _cast_if_autocast_enabled(input, normalized_shape, eps, memory_efficient)
    with torch.amp.autocast('cuda', enabled=False):
        if supports_custom_op():
            return fused_rms_norm_fwd(*args)[0]
        else:
            return FusedRMSNormFunction.apply(*args)


def mixed_dtype_fused_rms_norm_affine(input, weight, normalized_shape, eps=1e-6, memory_efficient=False):
    args = _cast_if_autocast_enabled(input, weight, normalized_shape, eps, memory_efficient)
    with torch.amp.autocast('cuda', enabled=False):
        return FusedRMSNormAffineMixedDtypesFunction.apply(*args)


class FusedLayerNorm(torch.nn.Module):
    r"""Applies Layer Normalization over a mini-batch of inputs as described in
    the paper `Layer Normalization`_ .

    Currently only runs on cuda() tensors.

    .. math::
        y = \frac{x - \mathrm{E}[x]}{ \sqrt{\mathrm{Var}[x] + \epsilon}} * \gamma + \beta

    The mean and standard-deviation are calculated separately over the last
    certain number dimensions which have to be of the shape specified by
    :attr:`normalized_shape`.
    :math:`\gamma` and :math:`\beta` are learnable affine transform parameters of
    :attr:`normalized_shape` if :attr:`elementwise_affine` is ``True``.

    .. note::
        Unlike Batch Normalization and Instance Normalization, which applies
        scalar scale and bias for each entire channel/plane with the
        :attr:`affine` option, Layer Normalization applies per-element scale and
        bias with :attr:`elementwise_affine`.

    This layer uses statistics computed from input data in both training and
    evaluation modes.

    Args:
        normalized_shape (int or list or torch.Size): input shape from an expected input
            of size

            .. math::
                [* \times \text{normalized}\_\text{shape}[0] \times \text{normalized}\_\text{shape}[1]
                    \times \ldots \times \text{normalized}\_\text{shape}[-1]]

            If a single integer is used, it is treated as a singleton list, and this module will
            normalize over the last dimension which is expected to be of that specific size.
        eps: a value added to the denominator for numerical stability. Default: 1e-5
        elementwise_affine: a boolean value that when set to ``True``, this module
            has learnable per-element affine parameters initialized to ones (for weights)
            and zeros (for biases). Default: ``True``.

    Shape:
        - Input: :math:`(N, *)`
        - Output: :math:`(N, *)` (same shape as input)

    Examples::

        >>> input = torch.randn(20, 5, 10, 10)
        >>> # With Learnable Parameters
        >>> m = apex.normalization.FusedLayerNorm(input.size()[1:])
        >>> # Without Learnable Parameters
        >>> m = apex.normalization.FusedLayerNorm(input.size()[1:], elementwise_affine=False)
        >>> # Normalize over last two dimensions
        >>> m = apex.normalization.FusedLayerNorm([10, 10])
        >>> # Normalize over last dimension of size 10
        >>> m = apex.normalization.FusedLayerNorm(10)
        >>> # Activating the module
        >>> output = m(input)

    .. _`Layer Normalization`: https://arxiv.org/abs/1607.06450
    """

    def __init__(self, normalized_shape, eps=1e-5, elementwise_affine=True, memory_efficient=False):
        super().__init__()

        global fused_layer_norm_cuda
        fused_layer_norm_cuda = importlib.import_module("fused_layer_norm_cuda")

        if isinstance(normalized_shape, numbers.Integral):
            normalized_shape = (normalized_shape,)
        self.normalized_shape = torch.Size(normalized_shape)
        self.eps = eps
        self.elementwise_affine = elementwise_affine
        self.memory_efficient = memory_efficient
        if self.elementwise_affine:
            self.weight = Parameter(torch.empty(*normalized_shape))
            self.bias = Parameter(torch.empty(*normalized_shape))
        else:
            self.register_parameter("weight", None)
            self.register_parameter("bias", None)
        self.reset_parameters()

    def reset_parameters(self):
        if self.elementwise_affine:
            init.ones_(self.weight)
            init.zeros_(self.bias)

    def forward(self, input):
        if torch.jit.is_tracing() or torch.jit.is_scripting() or torch.compiler.is_compiling() or not input.is_cuda:
            return F.layer_norm(input, self.normalized_shape, self.weight, self.bias, self.eps)
        if self.elementwise_affine:
            return fused_layer_norm_affine(
                input, self.weight, self.bias, self.normalized_shape, self.eps, self.memory_efficient
            )
        else:
            return fused_layer_norm(input, self.normalized_shape, self.eps, self.memory_efficient)

    def extra_repr(self):
        return "{normalized_shape}, eps={eps}, " "elementwise_affine={elementwise_affine}".format(**self.__dict__)


class FusedRMSNorm(torch.nn.Module):
    r"""Applies RMS Normalization over a mini-batch of inputs

    Currently only runs on cuda() tensors.

    .. math::
        y = \frac{x}{\mathrm{RMS}[x]} * \gamma

    The root-mean-square is calculated separately over the last
    certain number dimensions which have to be of the shape specified by
    :attr:`normalized_shape`.
    :math:`\gamma` is a learnable affine transform parameter of
    :attr:`normalized_shape` if :attr:`elementwise_affine` is ``True``.
    `epsilon` is added to the mean-square, then the root of the sum is taken.

    .. note::
        Unlike Batch Normalization and Instance Normalization, which applies
        scalar scale and bias for each entire channel/plane with the
        :attr:`affine` option, RMS Normalization applies per-element scale
        with :attr:`elementwise_affine`.

    This layer uses statistics computed from input data in both training and
    evaluation modes.

    Args:
        normalized_shape (int or list or torch.Size): input shape from an expected input
            of size

            .. math::
                [* \times \text{normalized}\_\text{shape}[0] \times \text{normalized}\_\text{shape}[1]
                    \times \ldots \times \text{normalized}\_\text{shape}[-1]]

            If a single integer is used, it is treated as a singleton list, and this module will
            normalize over the last dimension which is expected to be of that specific size.
        eps: a value added to the denominator for numerical stability. Default: 1e-5
        elementwise_affine: a boolean value that when set to ``True``, this module
            has learnable per-element affine parameters initialized to ones (for weights)
            and zeros (for biases). Default: ``True``.

    Shape:
        - Input: :math:`(N, *)`
        - Output: :math:`(N, *)` (same shape as input)

    Examples::

        >>> input = torch.randn(20, 5, 10, 10)
        >>> # With Learnable Parameters
        >>> m = apex.normalization.FusedRMSNorm(input.size()[1:])
        >>> # Without Learnable Parameters
        >>> m = apex.normalization.FusedRMSNorm(input.size()[1:], elementwise_affine=False)
        >>> # Normalize over last two dimensions
        >>> m = apex.normalization.FusedRMSNorm([10, 10])
        >>> # Normalize over last dimension of size 10
        >>> m = apex.normalization.FusedRMSNorm(10)
        >>> # Activating the module
        >>> output = m(input)

    .. _`Root Mean Square Layer Normalization`: https://arxiv.org/pdf/1910.07467.pdf
    """

    def __init__(self, normalized_shape, eps=1e-5, elementwise_affine=True, memory_efficient=False):
        super().__init__()

        global fused_layer_norm_cuda
        fused_layer_norm_cuda = importlib.import_module("fused_layer_norm_cuda")

        if isinstance(normalized_shape, numbers.Integral):
            normalized_shape = (normalized_shape,)
        self.normalized_shape = torch.Size(normalized_shape)
        self.eps = eps
        self.elementwise_affine = elementwise_affine
        self.memory_efficient = memory_efficient
        if self.elementwise_affine:
            self.weight = Parameter(torch.empty(*normalized_shape))
        else:
            self.register_parameter("weight", None)
        self.reset_parameters()

    def reset_parameters(self):
        if self.elementwise_affine:
            init.ones_(self.weight)

    def forward(self, input):
        if torch.jit.is_tracing() or torch.jit.is_scripting() or torch.compiler.is_compiling() or not input.is_cuda:
            return manual_rms_norm(input, self.normalized_shape, self.weight, self.eps)

        if self.elementwise_affine:
            return fused_rms_norm_affine(
                input, self.weight, self.normalized_shape, self.eps, self.memory_efficient
            )
        else:
            return fused_rms_norm(input, self.normalized_shape, self.eps, self.memory_efficient)

    def extra_repr(self):
        return "{normalized_shape}, eps={eps}, " "elementwise_affine={elementwise_affine}".format(**self.__dict__)


# NOTE (mkozuki): Why "mixed"?
# MixedFusedLayerNorm differs from FusedLayerNorm in that this layer norm uses parameter's dtype
# as output tensor's dtype while FusedLayerNorm uses input tensor's dtype for output tensor's dtype.
# See: `layer_norm_affine` and `layer_norm_affine_mixed_dtypes` in "csrc/layer_norm_cuda.cpp"
class MixedFusedLayerNorm(FusedLayerNorm):

    def __init__(self, normalized_shape, eps=1e-5, *, memory_efficient=False, **kwargs):
        if "elementwise_affine" in kwargs:
            import warnings
            warnings.warn("MixedFusedLayerNorm does not support `elementwise_affine` argument")
            elementwise_affine = kwargs.pop("elementwise_affine")
            if not elementwise_affine:
                raise RuntimeError("MixedFusedLayerNorm does not support `elementwise_affine = False`")

        super().__init__(
            normalized_shape=normalized_shape, eps=eps, elementwise_affine=True, memory_efficient=memory_efficient
        )
    def forward(self, input: torch.Tensor):
        # NOTE (mkozuki): CPU path is here mainly for unittest sake.
        if torch.jit.is_tracing() or torch.jit.is_scripting() or not input.is_cuda:
            return F.layer_norm(input, self.normalized_shape, self.weight, self.bias, self.eps)
        return mixed_dtype_fused_layer_norm_affine(
            input, self.weight, self.bias, self.normalized_shape, self.eps, self.memory_efficient
        )


# MixedFusedLayerNorm differs from FusedLayerNorm in that this layer norm uses parameter's dtype
# as output tensor's dtype while FusedLayerNorm uses input tensor's dtype for output tensor's dtype.
# See: `layer_norm_affine` and `layer_norm_affine_mixed_dtypes` in "csrc/layer_norm_cuda.cpp"
class MixedFusedRMSNorm(FusedRMSNorm):

    def __init__(self, normalized_shape, eps=1e-5, *, memory_efficient=False, **kwargs):
        if "elementwise_affine" in kwargs:
            import warnings
            warnings.warn("MixedFusedRMSNorm does not support `elementwise_affine` argument")
            elementwise_affine = kwargs.pop("elementwise_affine")
            if not elementwise_affine:
                raise RuntimeError("MixedFusedRMSNorm does not support `elementwise_affine = False`")

        super().__init__(
            normalized_shape=normalized_shape, eps=eps, elementwise_affine=True, memory_efficient=memory_efficient
        )
    def forward(self, input: torch.Tensor):
        # NOTE (mkozuki): CPU path is here mainly for unittest sake.
        # TODO Manual RMS Norm Implementation Here
        if torch.jit.is_tracing() or torch.jit.is_scripting() or not input.is_cuda:
            return manual_rms_norm(input, self.normalized_shape, self.weight, self.eps)
        return mixed_dtype_fused_rms_norm_affine(
            input, self.weight, self.normalized_shape, self.eps, self.memory_efficient
        )
