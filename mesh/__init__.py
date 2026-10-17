"""
mesh/__init__.py — Zero-overhead lazy public API surface for Mesh (PEP 562).

Importing `mesh` or invoking CLI subcommands does not eagerly import `numpy`,
`onnxruntime`, `fastapi`, or `sqlite3` until the specific attribute is accessed.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from .version import VERSION

__version__ = VERSION

if TYPE_CHECKING:
    from .audit import (
        get_audit_log,
        get_audit_stats,
        log_forget,
        log_learn,
        log_recall,
        log_update,
        purge_memory_from_audit,
        scrub_memory_update_from_audit,
        validate_namespace,
    )
    from .context_builder import build_context, build_context_bundle
    from .daemon import get_or_create_auth_token, request_user_permission
    from .digest import generate_digest
    from .io import export_namespace, import_namespace
    from .memory import (
        VALID_MEMORY_TYPES,
        Mesh,
        ensure_model_downloaded,
        get_engine_status,
        set_idle_unload_seconds,
        unload_model,
        warmup_model,
    )
    from .namespaces import (
        delete_namespace,
        list_namespaces,
        namespace_stats,
        rename_namespace,
    )
    from .passive import extract_memories_from_log
    from .store import validate_memory_id

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    # .memory
    "Mesh": (".memory", "Mesh"),
    "VALID_MEMORY_TYPES": (".memory", "VALID_MEMORY_TYPES"),
    "ensure_model_downloaded": (".memory", "ensure_model_downloaded"),
    "warmup_model": (".memory", "warmup_model"),
    "unload_model": (".memory", "unload_model"),
    "get_engine_status": (".memory", "get_engine_status"),
    "set_idle_unload_seconds": (".memory", "set_idle_unload_seconds"),
    # .io
    "export_namespace": (".io", "export_namespace"),
    "import_namespace": (".io", "import_namespace"),
    # .namespaces
    "list_namespaces": (".namespaces", "list_namespaces"),
    "delete_namespace": (".namespaces", "delete_namespace"),
    "rename_namespace": (".namespaces", "rename_namespace"),
    "namespace_stats": (".namespaces", "namespace_stats"),
    # .audit
    "log_recall": (".audit", "log_recall"),
    "log_learn": (".audit", "log_learn"),
    "log_update": (".audit", "log_update"),
    "log_forget": (".audit", "log_forget"),
    "purge_memory_from_audit": (".audit", "purge_memory_from_audit"),
    "scrub_memory_update_from_audit": (".audit", "scrub_memory_update_from_audit"),
    "validate_namespace": (".audit", "validate_namespace"),
    "get_audit_log": (".audit", "get_audit_log"),
    "get_audit_stats": (".audit", "get_audit_stats"),
    # .store
    "validate_memory_id": (".store", "validate_memory_id"),
    # .context_builder
    "build_context": (".context_builder", "build_context"),
    "build_context_bundle": (".context_builder", "build_context_bundle"),
    # .digest
    "generate_digest": (".digest", "generate_digest"),
    # .passive
    "extract_memories_from_log": (".passive", "extract_memories_from_log"),
    # .daemon
    "request_user_permission": (".daemon", "request_user_permission"),
    "get_or_create_auth_token": (".daemon", "get_or_create_auth_token"),
}

__all__ = [
    *list(_LAZY_EXPORTS.keys()),
    "VERSION",
    "__version__",
]


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is not None:
        mod_name, attr_name = target
        mod = importlib.import_module(mod_name, __name__)
        val = getattr(mod, attr_name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals().keys()) | set(__all__))