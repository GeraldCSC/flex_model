from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import logging
from typing import Union, Tuple, List, Any, Dict, Callable, Optional

import torch
from torch import Tensor
from transformers.modeling_outputs import BaseModelOutputWithPast


# Types which do not have classes
# TODO: Add high-level type for root node tree definitions
InternalObject = Any
LeafObject = Any
ScalarObject = Any
ScalarNode = Any


_INTERNAL_NODE_TYPE_REGISTRY: Dict[type, InternalNode] = {}


def register_internal_node_type(internal_node_type: type) -> Callable:
    def _inner(_internal_node_cls: InternalNode) -> None:
        _INTERNAL_NODE_TYPE_REGISTRY[internal_node_type] = _internal_node_cls

    return _inner


_LEAF_NODE_TYPE_REGISTRY: Dict[type, LeafNode] = {}


def register_leaf_node_type(leaf_node_type: type) -> Callable:
    def _inner(_leaf_node_cls: LeafNode) -> None:
        _LEAF_NODE_TYPE_REGISTRY[leaf_node_type] = _leaf_node_cls

    return _inner


def get_internal_node(internal_obj: InternalObject) -> InternalNode:
    return _INTERNAL_NODE_TYPE_REGISTRY[type(internal_obj)]


def get_leaf_node(leaf_obj: LeafObject) -> LeafNode:
    return _LEAF_NODE_TYPE_REGISTRY[type(leaf_obj)]


def _recursively_find_first_tensor(
    obj: Union[InternalObject, LeafObject, ScalarObject]
) -> Optional[Tensor]:
    # TODO: Deprecate
    if is_leaf_obj(obj):
        return obj

    if not is_internal_obj(obj):
        return

    for ele in obj:
        res = _recursively_find_first_tensor(ele)
        if res is not None:
            return res


class InternalNode:
    """Node correponding to unpackable container."""

    def __init__(
        self, children: Optional[List[Union[InternalNode, LeafNode, ScalarNode]]] = None
    ) -> None:
        self.children = children if children is not None else []

    def __eq__(self, other: Any) -> bool:
        """Traverse subtree checking for node equality recursively."""

        def _dfs(
            node1: Union[InternalNode, LeafNode, ScalarNode],
            node2: Union[InternalNode, LeafNode, ScalarNode],
        ) -> bool:
            # Mismatched types
            if type(node1) != type(node2):
                return False

            # Leaf node case
            if is_leaf_node(node1) and is_leaf_node(node2):
                return True

            # Internal node case
            elif is_internal_node(node1) and is_internal_node(node2):
                if len(node1.children) != len(node2.children):
                    return False

                subtrees_equal = []
                for n1, n2 in zip(node1.children, node2.children):
                    subtree = _dfs(n1, n2)
                    subtrees_equal.append(subtree)
                return all(subtrees_equal)

            # Scalar node case
            else:
                return node1 == node2

        return _dfs(self, other)

    def __repr__(self) -> str:
        return f"Node({self.children})"

    def __str__(self) -> str:
        return self.__repr__()

    def flatten(self, instance):
        """Flatten the associated instance by returning its contents."""
        raise NotImplementedError

    def unflatten(self, children):
        """Pack the contents (children) back into the associated container."""
        raise NotImplementedError


@register_internal_node_type(tuple)
class TupleNode(InternalNode):
    """Node corresponding to tuple."""

    def __repr__(self) -> str:
        return f"TupleNode({self.children})"

    def __str__(self) -> str:
        return self.__repr__()

    def flatten(self, instance: Tuple[Any]) -> Tuple[Any]:
        return instance

    def unflatten(self, children: List[Any]) -> Tuple[Any]:
        return tuple(child for child in children)


@register_internal_node_type(list)
class ListNode(InternalNode):
    """Node corresponding to list."""

    def __repr__(self) -> str:
        return f"ListNode({self.children})"

    def __str__(self) -> str:
        return self.__repr__()

    def flatten(self, instance: List[Any]) -> Tuple[Any]:
        return tuple(instance)

    def unflatten(self, children: List[Any]) -> List[Any]:
        return list(child for child in children)


@register_internal_node_type(BaseModelOutputWithPast)
class BaseModelOutputWithPastNode(InternalNode):
    """Node corresponding to Huggingface BaseModelOutputWithPast object."""

    def __repr__(self) -> str:
        return f"BaseModelOutputWithPastNode({self.children})"

    def flatten(self, instance: BaseModelOutputWithPast) -> Tuple[Any, Any, Any, Any]:
        contents = (
            instance.last_hidden_state,
            instance.past_key_values,
            instance.hidden_states,
            instance.attentions,
        )
        return contents

    def unflatten(self, children: List[Any]) -> BaseModelOutputWithPast:
        return BaseModelOutputWithPast(*children)


class LeafNode:
    """Leaf node, typically corresponding to a tensor.

    NOTE: Leaf nodes should not hold a ref to the underlying data, only some
    metadata.
    """

    def __init__(self, val: Any = None) -> None:
        self.val = val

    def __eq__(self, other: Any) -> bool:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"LeafNode"

    def __str__(self) -> str:
        return self.__repr__()


@register_leaf_node_type(Tensor)
class TensorNode(LeafNode):
    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, TensorNode):
            return False

        return self.val == other.val

    def __repr__(self) -> str:
        return f"TensorNode<{self.val}>"


def is_leaf_obj(obj: Any) -> bool:
    """Return true if the object corresponds to a leaf node."""
    return type(obj) in _LEAF_NODE_TYPE_REGISTRY


def is_internal_obj(obj: Any) -> bool:
    """Return true if the object corrsponds to an internal node."""
    return type(obj) in _INTERNAL_NODE_TYPE_REGISTRY


def is_leaf_node(node: Any) -> bool:
    """Return true if the object is a leaf node."""
    return isinstance(node, LeafNode)


def is_internal_node(node: Any) -> bool:
    """Return true if the object is an internal node."""
    return isinstance(node, InternalNode)


def _flatten(
    root_obj: Any,
) -> Tuple[Union[InternalNode, LeafNode, ScalarNode], List[Optional[Tensor]]]:
    order = []
    leaves = []

    def _dfs(obj):
        # Leaf obj case
        if is_leaf_obj(obj):
            leaf_node = get_leaf_node(obj)(val=obj.shape)
            order.append(leaf_node)
            leaves.append(obj)
            return leaf_node

        # Internal obj recursive case
        elif is_internal_obj(obj):
            # NOTE: Each node needs to know how to flatten its associated type
            #       instance. Ie. BaseModelOutputWithPast needs to be able to
            #       return its attributes in a tuple. They should also be able
            #       to perfectly recreate instances of themselves using a list of
            #       children.
            internal_node = get_internal_node(obj)()
            order.append(internal_node)

            # Internal node knows how to unpack its equivalent internal object
            unvisited_children = internal_node.flatten(obj)

            # Recurse into internal object's children
            for child in unvisited_children:
                internal_node.children.append(_dfs(child))
            return internal_node

        # Scalar obj case
        else:
            # Scalar nodes are just objects
            scalar_node = obj
            order.append(scalar_node)
            return scalar_node

    _dfs(root_obj)
    return order[0], leaves


def _unflatten(
    root_node: Union[InternalNode, LeafNode, ScalarNode], leaves: List[Optional[Tensor]]
) -> Any:
    leaves = list(reversed(leaves))

    def _dfs(node):
        # Leaf node case
        if is_leaf_node(node):
            return leaves.pop()

        # Internal node case
        elif is_internal_node(node):
            # Node knows how to pack itself up again into its corresponding obj
            obj = node.unflatten(_dfs(child) for child in node.children)
            return obj

        # Scalar node case
        else:
            return node

    return _dfs(root_node)
