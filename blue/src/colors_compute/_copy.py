"""Copy structured inputs without preserving SDK immutable container classes."""
from collections.abc import Mapping, Sequence
from copy import deepcopy as _deepcopy


def deepcopy(value, memo=None):
    memo = {} if memo is None else memo
    if id(value) in memo:
        return memo[id(value)]
    if isinstance(value, Mapping):
        result = {}
        memo[id(value)] = result
        for key, item in value.items():
            result[_deepcopy(key)] = deepcopy(item, memo)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        result = []
        memo[id(value)] = result
        result.extend(deepcopy(item, memo) for item in value)
        return result
    return _deepcopy(value, memo)
