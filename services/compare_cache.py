"""Bounded five-minute reuse of completed comparisons, with no paid work."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from time import monotonic

TTL_SECONDS = 300
MAX_ENTRIES = 64
_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()


def _key(topic: str, sources: list[str]) -> tuple:
    return topic.strip(), tuple(sorted({s.strip().casefold() for s in sources}))


def get_comparison(topic: str, sources: list[str]) -> dict | None:
    key = _key(topic, sources)
    item = _cache.get(key)
    if not item:
        return None
    expires, payload = item
    if monotonic() >= expires:
        del _cache[key]
        return None
    _cache.move_to_end(key)
    return deepcopy(payload)


def store_comparison(topic: str, sources: list[str], payload: dict) -> None:
    # Never cache errors, placeholder prose, or an uncompleted extraction.
    if not payload.get("claims") or not payload.get("sources_analyzed"):
        return
    key = _key(topic, sources)
    _cache[key] = (monotonic() + TTL_SECONDS, deepcopy(payload))
    _cache.move_to_end(key)
    while len(_cache) > MAX_ENTRIES:
        _cache.popitem(last=False)


def _reset_for_tests() -> None:
    _cache.clear()
