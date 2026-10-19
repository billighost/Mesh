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
        clear_audit_log,
        get_audit_log,
        get_audit_stats,
        log_forget,
        log_learn,
        log_recall,
        log_update,
        purge_memory_from_audit,
        reject_symlink,
        scrub_memory_update_from_audit,
        validate_namespace,
    )
    from .context_builder import build_context, build_context_bundle
    from .crypto import (
        disable_encryption_at_rest,
        enable_encryption_at_rest,
        encrypt_namespace_db,
        is_encryption_enabled,
    )
    from .daemon import (
        get_or_create_auth_token,
        request_user_permission,
        rotate_auth_token,
    )
    from .digest import generate_digest
    from .guards import GuardResult, guard_ingest, guard_tags
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
    from .secrets import (
        SecretLocator,
        SecretResolver,
        get_secret_from_keyring,
        run_with_secrets,
        store_secret_in_keyring,
    )
    from .store import VALID_TRUST_TIERS, NamespaceNotFoundError, validate_memory_id

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
    "clear_audit_log": (".audit", "clear_audit_log"),
    "reject_symlink": (".audit", "reject_symlink"),
    "validate_namespace": (".audit", "validate_namespace"),
    "get_audit_log": (".audit", "get_audit_log"),
    "get_audit_stats": (".audit", "get_audit_stats"),
    # .store
    "validate_memory_id": (".store", "validate_memory_id"),
    "NamespaceNotFoundError": (".store", "NamespaceNotFoundError"),
    "VALID_TRUST_TIERS": (".store", "VALID_TRUST_TIERS"),
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
    "rotate_auth_token": (".daemon", "rotate_auth_token"),
    # .guards
    "GuardResult": (".guards", "GuardResult"),
    "guard_ingest": (".guards", "guard_ingest"),
    "guard_tags": (".guards", "guard_tags"),
    # .secrets
    "SecretLocator": (".secrets", "SecretLocator"),
    "SecretResolver": (".secrets", "SecretResolver"),
    "store_secret_in_keyring": (".secrets", "store_secret_in_keyring"),
    "get_secret_from_keyring": (".secrets", "get_secret_from_keyring"),
    "run_with_secrets": (".secrets", "run_with_secrets"),
    # .crypto
    "is_encryption_enabled": (".crypto", "is_encryption_enabled"),
    "enable_encryption_at_rest": (".crypto", "enable_encryption_at_rest"),
    "disable_encryption_at_rest": (".crypto", "disable_encryption_at_rest"),
    "encrypt_namespace_db": (".crypto", "encrypt_namespace_db"),
}

# Written as a literal (rather than derived from `_LAZY_EXPORTS`) so static
# analysers and IDEs can see the public surface that `__getattr__` provides.
# `tests/test_remediation.py` asserts `set(__all__) == set(_LAZY_EXPORTS) | {"VERSION", "__version__"}`.
__all__ = [
    # .memory
    "Mesh",
    "VALID_MEMORY_TYPES",
    "ensure_model_downloaded",
    "warmup_model",
    "unload_model",
    "get_engine_status",
    "set_idle_unload_seconds",
    # .io
    "export_namespace",
    "import_namespace",
    # .namespaces
    "list_namespaces",
    "delete_namespace",
    "rename_namespace",
    "namespace_stats",
    # .audit
    "log_recall",
    "log_learn",
    "log_update",
    "log_forget",
    "purge_memory_from_audit",
    "scrub_memory_update_from_audit",
    "clear_audit_log",
    "reject_symlink",
    "validate_namespace",
    "get_audit_log",
    "get_audit_stats",
    # .store
    "validate_memory_id",
    "NamespaceNotFoundError",
    "VALID_TRUST_TIERS",
    # .context_builder
    "build_context",
    "build_context_bundle",
    # .digest
    "generate_digest",
    # .passive
    "extract_memories_from_log",
    # .daemon
    "request_user_permission",
    "get_or_create_auth_token",
    "rotate_auth_token",
    # .guards
    "GuardResult",
    "guard_ingest",
    "guard_tags",
    # .secrets
    "SecretLocator",
    "SecretResolver",
    "store_secret_in_keyring",
    "get_secret_from_keyring",
    "run_with_secrets",
    # .crypto
    "is_encryption_enabled",
    "enable_encryption_at_rest",
    "disable_encryption_at_rest",
    "encrypt_namespace_db",
    # module metadata
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
