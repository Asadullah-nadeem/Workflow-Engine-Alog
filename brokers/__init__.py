from __future__ import annotations

from typing import Optional
from .base_broker import BaseBroker
from .dhan import DhanBroker
from .zerodha import ZerodhaBroker

BROKER_REGISTRY = {
    "dhan": DhanBroker,
    "zerodha": ZerodhaBroker,
}


def get_broker(name: str, base_url: Optional[str] = None) -> BaseBroker:
    """
    Factory function to instantiate the requested broker adapter.
    """
    normalized = name.strip().lower()
    if normalized not in BROKER_REGISTRY:
        supported = ", ".join(BROKER_REGISTRY.keys())
        raise ValueError(
            f"Unsupported broker: '{name}'. Supported brokers are: {supported}"
        )

    broker_class = BROKER_REGISTRY[normalized]
    if base_url:
        return broker_class(base_url=base_url)
    return broker_class()


__all__ = ["BaseBroker", "DhanBroker", "ZerodhaBroker", "get_broker", "BROKER_REGISTRY"]
