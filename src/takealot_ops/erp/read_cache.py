"""Small in-process cache for expensive, read-only API projections."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from threading import Event, Lock
from typing import Generic, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class _Entry(Generic[T]):
    value: T
    expires_at: float
    generation: int


class ReadProjectionCache:
    """Bounded TTL cache with per-key request coalescing and safe invalidation."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 20.0,
        max_entries: int = 48,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._lock = Lock()
        self._entries: OrderedDict[Hashable, _Entry[object]] = OrderedDict()
        self._inflight: dict[tuple[int, Hashable], Event] = {}
        self._generation = 0

    def get_or_load(self, key: Hashable, loader: Callable[[], T]) -> T:
        """Return a fresh value, allowing only one loader per generation and key."""
        while True:
            leader = False
            with self._lock:
                now = self._clock()
                generation = self._generation
                entry = self._entries.get(key)
                if entry is not None and entry.generation == generation and entry.expires_at > now:
                    self._entries.move_to_end(key)
                    return entry.value  # type: ignore[return-value]
                if entry is not None:
                    self._entries.pop(key, None)
                inflight_key = (generation, key)
                event = self._inflight.get(inflight_key)
                if event is None:
                    event = Event()
                    self._inflight[inflight_key] = event
                    leader = True
            if not leader:
                event.wait()
                continue
            try:
                value = loader()
            except BaseException:
                with self._lock:
                    self._inflight.pop(inflight_key, None)
                    event.set()
                raise
            with self._lock:
                self._inflight.pop(inflight_key, None)
                if generation == self._generation:
                    self._entries[key] = _Entry(
                        value=value,
                        expires_at=self._clock() + self._ttl_seconds,
                        generation=generation,
                    )
                    self._entries.move_to_end(key)
                    while len(self._entries) > self._max_entries:
                        self._entries.popitem(last=False)
                event.set()
            return value

    def clear(self) -> None:
        """Invalidate all cached values and prevent older loaders from repopulating."""
        with self._lock:
            self._generation += 1
            self._entries.clear()

    @property
    def generation(self) -> int:
        """Expose the current invalidation generation for diagnostics and tests."""
        with self._lock:
            return self._generation
