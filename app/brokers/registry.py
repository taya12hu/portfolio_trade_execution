"""Broker registry. Adapters self-register with `@register_broker("name")`; `load_brokers()` imports
every `app/brokers/<name>/adapter.py`, so adding a broker needs no edits outside its own package."""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from app.brokers.base import AdapterContext, BrokerAdapter
from app.domain.models import BrokerSession

_REGISTRY: dict[str, type[BrokerAdapter]] = {}
_loaded = False


def register_broker(name: str):
    def decorator(cls: type[BrokerAdapter]) -> type[BrokerAdapter]:
        if name in _REGISTRY and _REGISTRY[name] is not cls:
            raise RuntimeError(f"broker {name!r} registered twice")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorator


def load_brokers() -> None:
    global _loaded
    if _loaded:
        return
    import app.brokers as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.ispkg:
            try:
                importlib.import_module(f"app.brokers.{mod.name}.adapter")
            except ModuleNotFoundError as exc:
                if exc.name != f"app.brokers.{mod.name}.adapter":
                    raise
    _loaded = True


def supported_brokers() -> list[str]:
    load_brokers()
    return sorted(_REGISTRY)


def get_adapter_class(name: str) -> type[BrokerAdapter] | None:
    load_brokers()
    return _REGISTRY.get(name)


def create_adapter(
    name: str, ctx: AdapterContext, session: BrokerSession | None = None, config: dict[str, Any] | None = None
) -> BrokerAdapter:
    cls = get_adapter_class(name)
    if cls is None:
        raise KeyError(name)
    return cls(ctx, session=session, config=config)
