"""Service registry: synchronous capabilities one module offers another.

Keys are ``"<module>.<service>"``. Lookups for absent modules return ``None`` so
optional dependencies degrade gracefully.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class ServiceRegistry:
    def __init__(self) -> None:
        self._services: dict[str, Callable[..., Any]] = {}

    def register(self, key: str, fn: Callable[..., Any]) -> None:
        if key in self._services:
            raise ValueError(f"service {key!r} is already registered")
        self._services[key] = fn

    def get(self, key: str) -> Callable[..., Any] | None:
        return self._services.get(key)

    def keys(self) -> list[str]:
        return sorted(self._services)
