"""Shared core for the egernia microservices."""

import logging
from collections.abc import Callable
from importlib.metadata import entry_points

__version__ = "0.1.0"

_log = logging.getLogger("egernia_core")


def load_entry_points(group: str, is_valid: Callable[[object], bool], kind: str):
    """Load the entry points in *group* as ``(entry_name, object)`` pairs.

    A broken or wrongly-typed third-party plugin is logged and skipped, so it
    never hides the plugins that do load.
    """
    loaded = []
    for entry in entry_points(group=group):
        try:
            obj = entry.load()
        except Exception:
            _log.exception("failed to load %s plugin %r", kind, entry.name)
            continue
        if not is_valid(obj):
            _log.error("entry point %r is not a valid %s plugin; ignoring", entry.name, kind)
            continue
        loaded.append((entry.name, obj))
    return loaded
