"""The shared, local project namespace convention used by agents and clients."""

from __future__ import annotations

import configparser
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from .audit import reject_symlink, validate_namespace


def resolve_namespace(explicit: str | None = None, workspace: Path | None = None) -> str:
    if explicit is not None:
        return validate_namespace(explicit)
    configured = os.environ.get("MESH_NAMESPACE", "").strip()
    if configured:
        return validate_namespace(configured)
    root = (workspace or Path.cwd()).absolute()
    project = root
    for candidate in (root, *root.parents):
        pointer = candidate / ".mesh" / "namespace"
        if pointer.exists():
            reject_symlink(pointer.parent)
            reject_symlink(pointer)
            if pointer.stat().st_size > 256:
                raise ValueError("Project namespace pointer is too large.")
            return validate_namespace(pointer.read_text(encoding="utf-8").strip())
        git_dir = candidate / ".git"
        if git_dir.exists() or git_dir.is_symlink():
            project = candidate
            config = _git_config(git_dir)
            if config is not None:
                parser = configparser.ConfigParser(interpolation=None)
                try:
                    parser.read_string(_read_metadata(config, 1048576))
                    remote = parser.get('remote "origin"', "url", fallback="")
                    name = urlparse(remote).path.rsplit("/", 1)[-1].removesuffix(".git")
                    if name:
                        return validate_namespace(_normalize(name))
                except (OSError, configparser.Error):
                    pass
            break
    return validate_namespace(_normalize(project.name) or "shared")


def _read_metadata(path: Path, limit: int = 4096) -> str:
    for candidate in (path, *path.parents):
        reject_symlink(candidate)
    with path.open("rb") as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise ValueError("Git namespace metadata is too large.")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Git namespace metadata must be UTF-8.") from None


def _metadata_pointer(value: str, base: Path) -> Path:
    value = value.strip()
    if not value or "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("Malformed Git namespace pointer.")
    pointer = Path(value)
    return Path(os.path.abspath(pointer if pointer.is_absolute() else base / pointer))


def _git_config(git_path: Path) -> Path | None:
    reject_symlink(git_path)
    if git_path.is_file():
        value = _read_metadata(git_path)
        if not value.startswith("gitdir: "):
            raise ValueError("Malformed Git namespace pointer.")
        directory = _metadata_pointer(value[len("gitdir: "):], git_path.parent)
    else:
        directory = git_path
    reject_symlink(directory)
    common = directory / "commondir"
    if common.exists() or common.is_symlink():
        directory = _metadata_pointer(_read_metadata(common), directory)
    config = directory / "config"
    for candidate in (config, *config.parents):
        reject_symlink(candidate)
    return config if config.is_file() else None


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-_")[:64]
