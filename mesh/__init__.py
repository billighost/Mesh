"""
mesh/__init__.py — Public API surface for Mesh.
"""

from .audit import (
    get_audit_log,
    get_audit_stats,
    log_forget,
    log_learn,
    log_recall,
    log_update,
)
from .context_builder import build_context
from .digest import generate_digest
from .io import export_namespace, import_namespace
from .memory import VALID_MEMORY_TYPES, Mesh, warmup_model
from .namespaces import (
    delete_namespace,
    list_namespaces,
    namespace_stats,
    rename_namespace,
)
from .passive import extract_memories_from_log
from .version import VERSION

__all__ = [
    "Mesh",
    "VALID_MEMORY_TYPES",
    "warmup_model",
    "export_namespace",
    "import_namespace",
    "list_namespaces",
    "delete_namespace",
    "rename_namespace",
    "namespace_stats",
    "log_recall",
    "log_learn",
    "log_update",
    "log_forget",
    "get_audit_log",
    "get_audit_stats",
    "build_context",
    "generate_digest",
    "extract_memories_from_log",
    "VERSION",
]

__version__ = VERSION