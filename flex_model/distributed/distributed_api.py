"""Backend-agnostic distributed launch and teardown.

User has some model on 1+ GPUs, using some sort of distributed backed
(typically accelerate or torch distributed). First find out which
backend is being used in the __init__method of the core `FlexModel` class.

:note: We always assume distributed backend is initialized already, ie. torch
    has already called `init_process_groups`.
:note: Additionally, we leave primitives like `torch.dsitributed.get_rank()` to
    torch instead of wrapping them.

:note: Viable states
    * Single-gpu no torch dist
    * Single-gpu w/ torch dist
    * Multi-gpu w/ torch dist

:note: Guards
    * In the presence of torch distributed, every rank is in a TP group. However,
    not every rank is in a DP or PP group. Therefore we must guard collectives
    depending on presence within DP or PP groups.
"""

from __future__ import annotations

import logging
from typing import Optional, Type

import accelerate
import torch
import torch.distributed as pt_dist
from accelerate import PartialState

from flex_model.distributed.backends import (
    AccelerateDistributedBackend,
    DistributedBackend,
    GPUDeviceMesh,
    TorchDistributedBackend,
)

_SUPPORTED_BACKENDS = {
    "torch": TorchDistributedBackend,
    "accelerate": AccelerateDistributedBackend,
}
_ACTIVE_BACKEND: Optional[DistributedBackend] = None

logger = logging.getLogger(__name__)


def initialize_distributed_backend(
    world_size: int,
    tensor_parallel_size: int,
    pipeline_parallel_size: int,
    data_parallel_size: int,
) -> None:
    """Main entry point from :class:`FlexModel` to initialize distributed backend.

    Given tensor, pipeline and data parallel sharding scheme of the wrapped
    :code:`nn.Module`, detect which backend to use and assemble a GPU device mesh
    which facilitates activation communication.

    :param int world_size: Total number of devices used to host the wrapped module.
    :param int tensor_parallel_size: Number of devices in each tensor parallel
        group.
    :param int pipeline_parallel_size: Number of devices in the pipeline parallel
        group.
    :param int data_parallel_size: Number of devices in each data parallel group.

    :raises AssertionError: If the world size is inconsistent with the tp, pp
        and dp sizes.
    """
    assert (
        world_size
        == tensor_parallel_size * pipeline_parallel_size * data_parallel_size
    )
    backend_cls = _parse_backend()
    logger.debug(f"Using DistributedBackend: {backend_cls.__name__}")

    device_mesh = GPUDeviceMesh.build(
        world_size,
        tensor_parallel_size,
        pipeline_parallel_size,
        data_parallel_size,
    )

    backend = backend_cls(device_mesh)
    _expose_distributed_backend(backend)


def distributed_backend_is_initialized() -> bool:
    """Check if the distributed backend has been selected and enabled.

    :returns: True if the backend is enabled.
    """
    global _ACTIVE_BACKEND
    return _ACTIVE_BACKEND is not None


def destroy_distributed_backend() -> None:
    """Disable and delete the active distributed backend."""
    global _ACTIVE_BACKEND
    _ACTIVE_BACKEND = None


def _parse_backend() -> Type[DistributedBackend]:
    """Parse the runtime distributed state and determine the backend to use.

    Both Pytorch and huggingface use similar distributed backends, where
    huggingface accelerate is a light wrapper over Pytorch distributed. To
    figure out which backend is used, we can probe out various environment
    and/or state variables.

    :returns: The corresponding `DistributedBackend` class to be instantiated.
    :rtype: Type[DistributedBackend]

    :raises NotImplementedError: Unknown and unsupported distributed backend
        found.
    """
    global _SUPPORTED_BACKENDS

    ps = PartialState()

    if (
        ps.distributed_type == accelerate.DistributedType.DEEPSPEED
        or ps.distributed_type == accelerate.DistributedType.FSDP
        or ps.distributed_type == accelerate.DistributedType.MEGATRON_LM
    ):
        hf_distributed = True
    else:
        hf_distributed = False

    # Using huggingface accelerate with torch
    if torch.distributed.is_initialized() and hf_distributed:
        return _SUPPORTED_BACKENDS["accelerate"]

    # Using torch distributed only. Single-gpu case is covered by torch
    # backend.
    elif (
        torch.distributed.is_initialized()
        and not hf_distributed
        or not torch.distributed.is_initialized()
    ):
        return _SUPPORTED_BACKENDS["torch"]

    # Unsupported
    else:
        raise NotImplementedError(
            "Distributed backend currently not supported."
        )


def _expose_distributed_backend(backend: DistributedBackend):
    """Set the global distributed backend."""
    global _ACTIVE_BACKEND
    _ACTIVE_BACKEND = backend


def initialize_activation_parallel() -> None:
    """Initialize activation parallel distributed groups."""
    global _ACTIVE_BACKEND
    assert _ACTIVE_BACKEND is not None
    _ACTIVE_BACKEND.initialize_activation_parallel()


def activation_parallel_is_initialized() -> bool:
    """Check if activation parallel distributed groups have been initialized."""
    global _ACTIVE_BACKEND
    assert _ACTIVE_BACKEND is not None
    return _ACTIVE_BACKEND.activation_parallel_is_initialized()


def in_tensor_parallel_group() -> bool:
    """Check if current worker belongs to a tensor parallel group."""
    global _ACTIVE_BACKEND
    assert _ACTIVE_BACKEND is not None
    return _ACTIVE_BACKEND.in_tensor_parallel_group()


def in_pipeline_parallel_group() -> bool:
    """Check if current worker belongs to a pipeline parallel group."""
    global _ACTIVE_BACKEND
    assert _ACTIVE_BACKEND is not None
    return _ACTIVE_BACKEND.in_pipeline_parallel_group()


def in_data_parallel_group() -> bool:
    """Check if current worker belongs to a data parallel group."""
    global _ACTIVE_BACKEND
    assert _ACTIVE_BACKEND is not None
    return _ACTIVE_BACKEND.in_data_parallel_group()


def get_activation_tensor_parallel_group() -> Optional[pt_dist.ProcessGroup]:
    """Get the activation parallel tp group."""
    global _ACTIVE_BACKEND
    assert _ACTIVE_BACKEND is not None
    return _ACTIVE_BACKEND.get_activation_tensor_parallel_group()


def get_activation_data_parallel_group() -> Optional[pt_dist.ProcessGroup]:
    """Get the activation parallel dp group."""
    global _ACTIVE_BACKEND
    assert _ACTIVE_BACKEND is not None
    return _ACTIVE_BACKEND.get_activation_data_parallel_group()


def get_activation_pipeline_parallel_group() -> Optional[pt_dist.ProcessGroup]:
    """Get the activation parallel dp group."""
    global _ACTIVE_BACKEND
    assert _ACTIVE_BACKEND is not None
    return _ACTIVE_BACKEND.get_activation_pipeline_parallel_group()


def get_activation_tensor_parallel_world_size() -> int:
    """Get the activation parallel tp group world size."""
    global _ACTIVE_BACKEND
    assert _ACTIVE_BACKEND is not None
    return _ACTIVE_BACKEND.get_activation_tensor_parallel_world_size()


def get_activation_data_parallel_world_size() -> int:
    """Get the activation parallel dp group world size."""
    global _ACTIVE_BACKEND
    assert _ACTIVE_BACKEND is not None
    return _ACTIVE_BACKEND.get_activation_data_parallel_world_size()


def get_activation_pipeline_parallel_world_size() -> int:
    """Get the activation parallel dp group world size."""
    global _ACTIVE_BACKEND
    assert _ACTIVE_BACKEND is not None
    return _ACTIVE_BACKEND.get_activation_pipeline_parallel_world_size()


def get_activation_tensor_parallel_rank() -> int:
    """Get the activation parallel tp group world size."""
    global _ACTIVE_BACKEND
    assert _ACTIVE_BACKEND is not None
    return _ACTIVE_BACKEND.get_activation_tensor_parallel_rank()


def get_activation_data_parallel_rank() -> int:
    """Get the data parallel dp group world size."""
    global _ACTIVE_BACKEND
    assert _ACTIVE_BACKEND is not None
    return _ACTIVE_BACKEND.get_activation_data_parallel_rank()


def get_activation_pipeline_parallel_rank() -> int:
    """Get the data parallel dp group world size."""
    global _ACTIVE_BACKEND
    assert _ACTIVE_BACKEND is not None
    return _ACTIVE_BACKEND.get_activation_pipeline_parallel_rank()


def destroy_activation_parallel() -> None:
    """Destroy the activation parallel groups."""
    global _ACTIVE_BACKEND
    assert _ACTIVE_BACKEND is not None
    _ACTIVE_BACKEND.destroy_activation_parallel()
