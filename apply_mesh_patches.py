#!/usr/bin/env python3
"""
apply_mesh_patches.py - applies the Mesh latency patches in place.

Usage (run from the repo root that contains the `mesh/` folder):
    python apply_mesh_patches.py            # apply
    python apply_mesh_patches.py --check    # dry run, report only
    python apply_mesh_patches.py --revert   # restore from *.pre_patch backups

Every edit is an exact-text replacement. An edit is skipped if it is already
applied, and the script aborts (touching nothing) if an anchor is missing or
ambiguous, so it can never leave a half-patched file behind.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

PATCHES: list[tuple[str, str, str]] = []


def P(path: str, old: str, new: str) -> None:
    PATCHES.append((path, old, new))


# ============================================================================
# mesh/audit.py
# ============================================================================
P("mesh/audit.py",
r'''def reject_symlink(path: Path) -> None:
    """
    Refuse to operate on a symlink (or sidecar `-wal`/`-shm` symlink) to prevent
    symlink-following arbitrary file clobbering or chmod attacks (Finding N6).
    """
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}") if suffix else path
        if _is_symlink_or_reparse(candidate):
            raise ValueError(f"Refusing to operate on symlinked path: '{candidate}'.")
''',
r'''# --- Path-trust cache -------------------------------------------------------
# A full reject_symlink() costs ~9-25 syscalls (is_symlink/is_junction/exists/lstat
# x 3 sidecar suffixes), which on NTFS + Defender is milliseconds per call. Paths
# that passed the full check are remembered for MESH_PATH_RECHECK_SECONDS (default
# 5, 0 disables). While trusted, every call still does ONE lstat of the path itself,
# so swapping the db/dir for a symlink or junction is caught immediately; only the
# -wal/-shm sidecar re-check waits for the TTL.
_PATH_TRUST: dict[str, float] = {}
_RESOLVED_DIR_CACHE: dict[str, tuple[float, Path]] = {}
_FILE_PERM_TS: dict[str, float] = {}
_AUDIT_PATH_CACHE: dict[tuple[str, str], Path] = {}


def _path_recheck_ttl() -> float:
    raw = os.environ.get("MESH_PATH_RECHECK_SECONDS")
    if raw is None:
        return 5.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 5.0


_PATH_MICRO: dict[str, float] = {}


def _fs_memo_seconds() -> float:
    """Window (default 10 ms) in which a path that was lstat-verified is not re-checked (MESH_FS_MEMO_MS, 0 = off)."""
    raw = os.environ.get("MESH_FS_MEMO_MS")
    try:
        return max(0.0, float(raw) / 1000.0) if raw is not None else 0.01
    except ValueError:
        return 0.01


def clear_path_trust_cache() -> None:
    """Forget every verified path (tests / evict hooks)."""
    _PATH_TRUST.clear()
    _PATH_MICRO.clear()
    _RESOLVED_DIR_CACHE.clear()
    _FILE_PERM_TS.clear()
    _AUDIT_PATH_CACHE.clear()


def _lstat_link_state(key: str) -> Optional[bool]:
    """True = symlink/reparse point, False = plain entry, None = missing/unreadable."""
    try:
        st = os.lstat(key)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or (getattr(st, "st_file_attributes", 0) & 0x400):
        return True
    return False


def cached_resolved_mesh_dir() -> Path:
    """`get_mesh_dir().resolve()` memoized for the path-trust TTL."""
    raw = get_mesh_dir()
    key = os.fspath(raw)
    ttl = _path_recheck_ttl()
    if ttl > 0.0:
        ent = _RESOLVED_DIR_CACHE.get("R:" + key)
        if ent is not None and (time.monotonic() - ent[0]) < ttl:
            return ent[1]
    resolved = raw.resolve()
    if ttl > 0.0:
        _RESOLVED_DIR_CACHE["R:" + key] = (time.monotonic(), resolved)
    return resolved


def reject_symlink(path: Path) -> None:
    """
    Refuse to operate on a symlink (or sidecar `-wal`/`-shm` symlink) to prevent
    symlink-following arbitrary file clobbering or chmod attacks (Finding N6).
    """
    key = os.fspath(path)
    ttl = _path_recheck_ttl()
    if ttl > 0.0:
        ts = _PATH_TRUST.get(key)
        _now = time.monotonic()
        if ts is not None and (_now - ts) < ttl:
            _micro = _fs_memo_seconds()
            if _micro > 0.0:
                _seen = _PATH_MICRO.get(key)
                if _seen is not None and (_now - _seen) < _micro:
                    return  # lstat-verified milliseconds ago, inside the same operation: skip a repeat syscall
            link = _lstat_link_state(key)
            if link is False:
                _PATH_MICRO[key] = _now
                return
            _PATH_MICRO.pop(key, None)
            _PATH_TRUST.pop(key, None)
            if link is True:
                raise ValueError(f"Refusing to operate on symlinked path: '{path}'.")
            # Missing now: fall through to the full check.
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}") if suffix else path
        if _is_symlink_or_reparse(candidate):
            raise ValueError(f"Refusing to operate on symlinked path: '{candidate}'.")
    if ttl > 0.0:
        _PATH_TRUST[key] = time.monotonic()
''')

P("mesh/audit.py",
r'''    target = path if path is not None else get_mesh_dir()
    reject_symlink(target)
    try:
        resolved = target.resolve()
        with _SECURED_LOCK:
            if resolved in _SECURED_PATHS and target.exists():
                return target
    except OSError:
        resolved = target
''',
r'''    target = path if path is not None else get_mesh_dir()
    reject_symlink(target)
    _tkey = os.fspath(target)
    _ttl = _path_recheck_ttl()
    if _ttl > 0.0:
        _ent = _RESOLVED_DIR_CACHE.get(_tkey)
        if _ent is not None and (time.monotonic() - _ent[0]) < _ttl and os.path.isdir(_tkey):
            return target
    try:
        resolved = target.resolve()
        with _SECURED_LOCK:
            if resolved in _SECURED_PATHS and target.exists():
                if _ttl > 0.0:
                    _RESOLVED_DIR_CACHE[_tkey] = (time.monotonic(), resolved)
                return target
    except OSError:
        resolved = target
''')

P("mesh/audit.py",
r'''    with _SECURED_LOCK:
        _SECURED_PATHS[resolved] = 1
    return target
''',
r'''    with _SECURED_LOCK:
        _SECURED_PATHS[resolved] = 1
    if _ttl > 0.0:
        _RESOLVED_DIR_CACHE[_tkey] = (time.monotonic(), resolved)
    return target
''')

P("mesh/audit.py",
r'''    try:
        resolved_base = file_path.resolve()
        with _SECURED_LOCK:
            cached_stamp = _SECURED_PATHS.get(resolved_base)
''',
r'''    _fkey = os.fspath(file_path)
    _fttl = _path_recheck_ttl()
    if _fttl > 0.0:
        _fts = _FILE_PERM_TS.get(_fkey)
        if _fts is not None and (time.monotonic() - _fts) < _fttl and _lstat_link_state(_fkey) is False:
            return
    try:
        resolved_base = file_path.resolve()
        with _SECURED_LOCK:
            cached_stamp = _SECURED_PATHS.get(resolved_base)
''')

P("mesh/audit.py",
r'''    try:
        if file_path.exists():
            new_stamp = int(file_path.stat().st_mtime_ns)
            with _SECURED_LOCK:
                _SECURED_PATHS[resolved_base] = new_stamp
    except OSError:
        pass
''',
r'''    try:
        if file_path.exists():
            new_stamp = int(file_path.stat().st_mtime_ns)
            with _SECURED_LOCK:
                _SECURED_PATHS[resolved_base] = new_stamp
            if _fttl > 0.0:
                _FILE_PERM_TS[_fkey] = time.monotonic()
    except OSError:
        pass
''')

P("mesh/audit.py",
r'''def _get_audit_db_path(namespace: str) -> Path:
    clean_ns = validate_namespace(namespace)
    base_dir = ensure_secure_mesh_dir().resolve()
    raw_candidate = base_dir / f"audit_{clean_ns}.db"
    reject_symlink(raw_candidate)
    db_path = raw_candidate.resolve()
    if db_path.parent != base_dir:
        raise ValueError(f"Invalid namespace path resolution for '{namespace}'.")
    return db_path
''',
r'''def _get_audit_db_path(namespace: str) -> Path:
    clean_ns = validate_namespace(namespace)
    _ckey = (clean_ns, os.fspath(get_mesh_dir()))
    _hit = _AUDIT_PATH_CACHE.get(_ckey)
    if _hit is not None:
        _hk = os.fspath(_hit)
        _ts = _PATH_TRUST.get(_hk)
        if _ts is not None and (time.monotonic() - _ts) < _path_recheck_ttl():
            reject_symlink(_hit)  # one lstat while trusted
            return _hit
    ensure_secure_mesh_dir()
    base_dir = cached_resolved_mesh_dir()
    raw_candidate = base_dir / f"audit_{clean_ns}.db"
    reject_symlink(raw_candidate)
    db_path = raw_candidate.resolve()
    if db_path.parent != base_dir:
        raise ValueError(f"Invalid namespace path resolution for '{namespace}'.")
    reject_symlink(db_path)
    _AUDIT_PATH_CACHE[_ckey] = db_path
    return db_path
''')



# ============================================================================
# mesh/store.py  (path-safety syscalls)
# ============================================================================
P("mesh/store.py",
r"""    REDACTED_CONTENT_PLACEHOLDER,
    clear_audit_log,
    ensure_secure_mesh_dir,""",
r"""    REDACTED_CONTENT_PLACEHOLDER,
    cached_resolved_mesh_dir,
    clear_audit_log,
    ensure_secure_mesh_dir,""")

P("mesh/store.py",
r"""    clean_ns = validate_namespace(namespace)
    base_dir = ensure_secure_mesh_dir().resolve()
    lock_path = base_dir / f".mesh_{clean_ns}.lock"
    reject_symlink(lock_path)
    return lock_path""",
r"""    clean_ns = validate_namespace(namespace)
    ensure_secure_mesh_dir()
    base_dir = cached_resolved_mesh_dir()
    lock_path = base_dir / f".mesh_{clean_ns}.lock"
    reject_symlink(lock_path)
    return lock_path""")

P("mesh/store.py",
r"""    clean_ns = validate_namespace(namespace)
    mesh_dir = get_mesh_dir().resolve()
    key = (mesh_dir, clean_ns)
    with _NS_LOCK_REGISTRY_MUTEX:
        state = _NS_LOCK_REGISTRY.get(key)
        if state is None:
            state = _NamespaceLockState(clean_ns)
            _NS_LOCK_REGISTRY[key] = state

    wait_timeout""",
r"""    clean_ns = validate_namespace(namespace)
    mesh_dir = cached_resolved_mesh_dir()
    key = (mesh_dir, clean_ns)
    with _NS_LOCK_REGISTRY_MUTEX:
        state = _NS_LOCK_REGISTRY.get(key)
        if state is None:
            state = _NamespaceLockState(clean_ns)
            _NS_LOCK_REGISTRY[key] = state

    wait_timeout""")

P("mesh/store.py",
r"""        base_dir = ensure_secure_mesh_dir().resolve()
        raw_candidate = base_dir / f"mesh_{self.namespace}.db"
        reject_symlink(raw_candidate)
        resolved_db = raw_candidate.resolve()""",
r"""        ensure_secure_mesh_dir()
        base_dir = cached_resolved_mesh_dir()
        raw_candidate = base_dir / f"mesh_{self.namespace}.db"
        reject_symlink(raw_candidate)
        resolved_db = raw_candidate.resolve()""")


# ============================================================================
# mesh/store.py  (hybrid_query candidate explosion: 1,000 rows -> Python scorer)
# ============================================================================
P("mesh/store.py",
r"""            eval_indices = [idx for idx in masked_indices if ids_snap[idx] in candidate_mid_set]
        else:
            eval_indices = masked_indices

        total_docs = max(1, len(rows_snap))""",
r"""            eval_indices = [idx for idx in masked_indices if ids_snap[idx] in candidate_mid_set]
        else:
            eval_indices = masked_indices

        # df-aware pruning: terms such as "port"/"architecture" occur in nearly every row,
        # so the union above can be the whole namespace. Rank by (lexical hits, semantic sim),
        # keep the best few dozen plus every row that matches a RARE query stem, and only
        # run the expensive per-row lexical scorer on those.
        _prune_cap = max(n * 2, 48)
        if len(eval_indices) > _prune_cap:
            _must: set[str] = set()
            with _CACHE_LOCK:
                for _s in set(q_stems):
                    if 0 < stem_df_snap.get(_s, 0) <= 24:
                        _hits = cache.stem_to_ids.get(_s)
                        if _hits:
                            _must.update(_hits & masked_mid_set)
            _ev = np.asarray(eval_indices, dtype=np.int64)
            _lex_part = np.fromiter(
                (lexical_map.get(ids_snap[_i], 0.0) for _i in eval_indices),
                dtype=np.float64,
                count=len(eval_indices),
            )
            _rank = 0.6 * _lex_part + 0.4 * np.clip(sims[_ev].astype(np.float64), 0.0, 1.0)
            _top = np.argpartition(-_rank, _prune_cap - 1)[:_prune_cap]
            _keep = {eval_indices[int(_i)] for _i in _top}
            eval_indices = [_i for _i in eval_indices if _i in _keep or ids_snap[_i] in _must]

        total_docs = max(1, len(rows_snap))""")

P("mesh/store.py",
r"""        masked_indices: list[int] = []
        masked_mid_set: set[str] = set()
        for idx, mid in enumerate(ids_snap):
            cached_row = rows_snap.get(mid)
            if cached_row is None:
                continue
            if not include_quarantined and cached_row.get("quarantined"):
                continue
            if type_filter and cached_row["memory_type"].lower() != type_filter:
                continue
            if source_agent and cached_row["source_agent"] != source_agent:
                continue
            if not include_private and cached_row.get("local_only"):
                continue
            if filter_tags:
                parsed_tags = cached_row.get("tags_list") or []
                mem_tags_lower = {str(t).lower() for t in parsed_tags}
                if not filter_tags.issubset(mem_tags_lower):
                    continue
            if not include_stale or min_confidence > 0.0:
                eff_conf, is_stale = _compute_row_effective_confidence(cached_row, now=ref_now)
                if not include_stale and is_stale:
                    continue
                if eff_conf < min_confidence:
                    continue
            masked_indices.append(idx)
            masked_mid_set.add(mid)
""",
r"""        masked_indices: list[int] = []
        masked_mid_set: set[str] = set()
        _no_row_filters = (
            not type_filter
            and not source_agent
            and not filter_tags
            and include_private
            and include_stale
            and min_confidence <= 0.0
        )
        if _no_row_filters:
            # Fast path: only the quarantine flag can exclude a row (comprehensions run ~2x faster than the
            # per-row append loop; same rows, same order).
            if include_quarantined:
                masked_indices = [idx for idx, mid in enumerate(ids_snap) if mid in rows_snap]
            else:
                masked_indices = [
                    idx
                    for idx, mid in enumerate(ids_snap)
                    if (_r := rows_snap.get(mid)) is not None and not _r.get("quarantined")
                ]
            masked_mid_set = {ids_snap[idx] for idx in masked_indices}
        else:
            for idx, mid in enumerate(ids_snap):
                cached_row = rows_snap.get(mid)
                if cached_row is None:
                    continue
                if not include_quarantined and cached_row.get("quarantined"):
                    continue
                if type_filter and cached_row["memory_type"].lower() != type_filter:
                    continue
                if source_agent and cached_row["source_agent"] != source_agent:
                    continue
                if not include_private and cached_row.get("local_only"):
                    continue
                if filter_tags:
                    parsed_tags = cached_row.get("tags_list") or []
                    mem_tags_lower = {str(t).lower() for t in parsed_tags}
                    if not filter_tags.issubset(mem_tags_lower):
                        continue
                if not include_stale or min_confidence > 0.0:
                    eff_conf, is_stale = _compute_row_effective_confidence(cached_row, now=ref_now)
                    if not include_stale and is_stale:
                        continue
                    if eff_conf < min_confidence:
                        continue
                masked_indices.append(idx)
                masked_mid_set.add(mid)
""")

P("mesh/store.py",
r"""        lexical_map: dict[str, float] = {}
        for tok in q_content_tokens:
            t_hits = cache.token_to_ids.get(tok)
            if t_hits:
                for mid in t_hits:
                    if mid in masked_mid_set:
                        lexical_map[mid] = lexical_map.get(mid, 0.0) + 1.0
        for syn_set in q_synonyms.values():
            for syn in syn_set:
                s_hits = cache.token_to_ids.get(syn)
                if s_hits:
                    for mid in s_hits:
                        if mid in masked_mid_set:
                            lexical_map[mid] = lexical_map.get(mid, 0.0) + 0.5
        if lexical_map:""",
r"""        # Counter.update() counts in C; identical totals to the old per-id Python loop
        # (+1.0 per exact token hit, +0.5 per synonym hit, masked rows only).
        from collections import Counter as _HitCounter

        _exact_hits = _HitCounter()
        for tok in q_content_tokens:
            t_hits = cache.token_to_ids.get(tok)
            if t_hits:
                _exact_hits.update(t_hits)
        _syn_hits = _HitCounter()
        for syn_set in q_synonyms.values():
            for syn in syn_set:
                s_hits = cache.token_to_ids.get(syn)
                if s_hits:
                    _syn_hits.update(s_hits)
        lexical_map: dict[str, float] = {
            mid: float(cnt) for mid, cnt in _exact_hits.items() if mid in masked_mid_set
        }
        for mid, cnt in _syn_hits.items():
            if mid in masked_mid_set:
                lexical_map[mid] = lexical_map.get(mid, 0.0) + 0.5 * cnt
        if lexical_map:""")


# ============================================================================
# mesh/memory.py  (learn/recall: recursive-glob model discovery on every call;
#                  native hash embedding)
# ============================================================================
P("mesh/memory.py",
r'''def _discover_local_onnx_pair() -> Optional[tuple[Path, Path]]:
    fast_pair = _has_local_onnx_pair()
    if fast_pair is not None:
        return fast_pair

    model_dir = _get_onnx_model_dir()''',
r'''_DISCOVER_NEG_UNTIL: float = 0.0


def _discover_negative_ttl() -> float:
    raw = os.environ.get("MESH_MODEL_DISCOVERY_TTL", "").strip()
    if not raw:
        return 120.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 120.0


def _discover_local_onnx_pair() -> Optional[tuple[Path, Path]]:
    global _DISCOVER_NEG_UNTIL
    fast_pair = _has_local_onnx_pair()
    if fast_pair is not None:
        return fast_pair
    # A failed search walks several cache trees with rglob (thousands of is_file() calls,
    # ~12 ms on Windows) and _get_model() used to repeat it on EVERY learn()/recall().
    # Remember the miss; a pair that appears later is still picked up by the
    # _has_local_onnx_pair() fast check above.
    if time.monotonic() < _DISCOVER_NEG_UNTIL:
        return None

    model_dir = _get_onnx_model_dir()''')

P("mesh/memory.py",
r'''    return _has_local_onnx_pair()


def _download_file_from_mirrors(''',
r'''    _pair = _has_local_onnx_pair()
    if _pair is None:
        _ttl = _discover_negative_ttl()
        if _ttl > 0.0:
            _DISCOVER_NEG_UNTIL = time.monotonic() + _ttl
    return _pair


def _download_file_from_mirrors(''')

P("mesh/memory.py",
r'''import numpy as np
''',
r'''import numpy as np

from .c_bridge import c_hash_embed_384 as _c_hash_embed_384
''')

P("mesh/memory.py",
r'''    `embedding_model = 'hash-384'` column so warm-up vectors are always upgraded once ONNX loads.
    """
''',
r'''    `embedding_model = 'hash-384'` column so warm-up vectors are always upgraded once ONNX loads.
    """
    if isinstance(text, str):
        _native_vec = _c_hash_embed_384(text)  # ~30 us vs ~330 us; None => Python path below
        if _native_vec is not None:
            return _native_vec
''')

# ============================================================================
# mesh/guards.py  (fast path must still honour MESH_MAX_CONTENT_LENGTH)
# ============================================================================
P("mesh/guards.py",
r'''    if _stripped and 0 < len(_stripped) <= 16384 and _c_guard_prescan(content):''',
r'''    if _stripped and 0 < len(_stripped) <= min(16384, _max_content_len()) and _c_guard_prescan(content):''')

P("mesh/guards.py",
r'''def guard_ingest(content: str, local_only: bool = False) -> GuardResult:''',
r'''def _max_content_len() -> int:
    from .memory import MAX_CONTENT_LENGTH

    return int(MAX_CONTENT_LENGTH)


def guard_ingest(content: str, local_only: bool = False) -> GuardResult:''')

# ============================================================================
# mesh/audit.py  (forget()/update() scrubbed the audit log twice)
# ============================================================================
P("mesh/audit.py",
r'''def purge_memory_from_audit(
    namespace: str,''',
r'''_SCRUB_DEDUP: dict[tuple, tuple[float, int]] = {}
_AUDIT_ENQUEUE_SEQ = [0]


def _scrub_recently_done(kind: str, namespace: str, memory_id: str, text: Optional[str]) -> bool:
    """True when the identical scrub finished <2 s ago and nothing was logged since."""
    ent = _SCRUB_DEDUP.get((kind, namespace, memory_id, hash(text or "")))
    return ent is not None and (time.monotonic() - ent[0]) < 2.0 and ent[1] == _AUDIT_ENQUEUE_SEQ[0]


def _scrub_mark_done(kind: str, namespace: str, memory_id: str, text: Optional[str]) -> None:
    if len(_SCRUB_DEDUP) > 512:
        _SCRUB_DEDUP.clear()
    _SCRUB_DEDUP[(kind, namespace, memory_id, hash(text or ""))] = (time.monotonic(), _AUDIT_ENQUEUE_SEQ[0])


def purge_memory_from_audit(
    namespace: str,''')

P("mesh/audit.py",
r'''    try:
        namespace = validate_namespace(namespace)
        flush_audit_log(namespace)
        db_path = _get_audit_db_path(namespace)
        if not db_path.exists() or _get_audit_db_stamp(db_path) is None:
            return
        with _AUDIT_LOCK:
            with _connect_audit(db_path) as conn:
                _scrub_audit_columns_locked(
                    conn=conn,
                    namespace=namespace,
                    memory_id=memory_id,
                    needle_text=content,
                    placeholder=PURGED_CONTENT_PLACEHOLDER,
                )
                conn.commit()
            new_stamp = _get_audit_db_stamp(db_path)
            if new_stamp is not None:
                _INITIALIZED_AUDIT_DBS[db_path] = new_stamp
    except Exception:
        pass
''',
r'''    try:
        namespace = validate_namespace(namespace)
        if _scrub_recently_done("purge", namespace, memory_id, content):
            return
        flush_audit_log(namespace)
        db_path = _get_audit_db_path(namespace)
        if not db_path.exists() or _get_audit_db_stamp(db_path) is None:
            return
        with _AUDIT_LOCK:
            with _connect_audit(db_path) as conn:
                _scrub_audit_columns_locked(
                    conn=conn,
                    namespace=namespace,
                    memory_id=memory_id,
                    needle_text=content,
                    placeholder=PURGED_CONTENT_PLACEHOLDER,
                )
                conn.commit()
            new_stamp = _get_audit_db_stamp(db_path)
            if new_stamp is not None:
                _INITIALIZED_AUDIT_DBS[db_path] = new_stamp
        _scrub_mark_done("purge", namespace, memory_id, content)
    except Exception:
        pass
''')

P("mesh/audit.py",
r'''    try:
        namespace = validate_namespace(namespace)
        flush_audit_log(namespace)
        db_path = _get_audit_db_path(namespace)
        if not db_path.exists() or _get_audit_db_stamp(db_path) is None:
            return
        placeholder = REDACTED_CONTENT_PLACEHOLDER if local_only else UPDATED_CONTENT_PLACEHOLDER''',
r'''    try:
        namespace = validate_namespace(namespace)
        _dedup_text = f"{int(bool(local_only))}:{old_content or ''}"
        if _scrub_recently_done("update", namespace, memory_id, _dedup_text):
            return
        flush_audit_log(namespace)
        db_path = _get_audit_db_path(namespace)
        if not db_path.exists() or _get_audit_db_stamp(db_path) is None:
            return
        placeholder = REDACTED_CONTENT_PLACEHOLDER if local_only else UPDATED_CONTENT_PLACEHOLDER''')

P("mesh/audit.py",
r'''                        needle_text=old_content,
                        placeholder=placeholder,
                    )
                conn.commit()
            new_stamp = _get_audit_db_stamp(db_path)
            if new_stamp is not None:
                _INITIALIZED_AUDIT_DBS[db_path] = new_stamp
    except Exception:
        pass
''',
r'''                        needle_text=old_content,
                        placeholder=placeholder,
                    )
                conn.commit()
            new_stamp = _get_audit_db_stamp(db_path)
            if new_stamp is not None:
                _INITIALIZED_AUDIT_DBS[db_path] = new_stamp
        _scrub_mark_done("update", namespace, memory_id, _dedup_text)
    except Exception:
        pass
''')

P("mesh/audit.py",
r'''    try:
        clean_ns = validate_namespace(namespace)
        db_path = _get_audit_db_path(clean_ns)
    except Exception:
        return

    if db_path not in _AUDIT_KNOWN_DB_PATHS:''',
r'''    _AUDIT_ENQUEUE_SEQ[0] += 1
    try:
        clean_ns = validate_namespace(namespace)
        db_path = _get_audit_db_path(clean_ns)
    except Exception:
        return

    if db_path not in _AUDIT_KNOWN_DB_PATHS:''')

# ============================================================================
# mesh/store.py  (delete(): forced telemetry flush before every forget)
# ============================================================================
P("mesh/store.py",
r'''        if not clean_id or not self.db_path.exists():
            return False

        self.flush_telemetry()
        purged_content: Optional[str] = None
        deleted = False
''',
r'''        if not clean_id or not self.db_path.exists():
            return False

        # Pending telemetry only needs flushing when it could still reference this row;
        # queries mentioning the content are scrubbed from the buffer in-memory below.
        with _TELEMETRY_LOCK:
            _tb = _TELEMETRY_BUFFERS.get(self.db_path)
            _has_pending = bool(_tb is not None and (_tb.access_increments or _tb.query_rows))
        if _has_pending:
            self.flush_telemetry()
        purged_content: Optional[str] = None
        deleted = False
''')


# ============================================================================
# driver
# ============================================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="repo root containing mesh/")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()
    root = Path(args.root).resolve()

    if args.revert:
        n = 0
        for bak in root.glob("**/*.pre_patch"):
            shutil.copy2(bak, bak.with_suffix(""))
            n += 1
        print(f"reverted {n} file(s)")
        return 0

    bridge = root / "mesh" / "c_bridge.py"
    if not bridge.exists() or "c_hash_embed_384" not in bridge.read_text(encoding="utf-8", errors="replace"):
        print("STOP: copy the NEW mesh/c_bridge.py (from this package) over mesh/c_bridge.py first.")
        return 3

    by_file: dict[str, list[tuple[str, str]]] = {}
    for f, old, new in PATCHES:
        by_file.setdefault(f, []).append((old, new))

    results: dict[str, str] = {}
    failed = False
    applied = skipped = 0
    for f, edits in by_file.items():
        p = root / f
        if not p.exists():
            print(f"MISSING FILE: {f}")
            failed = True
            continue
        raw = p.read_bytes().decode("utf-8")
        crlf = "\r\n" in raw
        text = raw.replace("\r\n", "\n")
        for i, (old, new) in enumerate(edits, 1):
            if new in text:  # check FIRST: for some edits `new` contains `old`
                skipped += 1
            elif text.count(old) == 1:
                text = text.replace(old, new)
                applied += 1
            else:
                cnt = text.count(old)
                print(f"ANCHOR PROBLEM in {f} (edit #{i}, matches={cnt}): {old[:90]!r}")
                failed = True
        results[f] = text.replace("\n", "\r\n") if crlf else text

    if failed:
        print("ABORTED: nothing was written.")
        return 2
    print(f"{applied} edit(s) to apply, {skipped} already applied.")
    if args.check:
        return 0
    for f, text in results.items():
        p = root / f
        bak = p.with_suffix(p.suffix + ".pre_patch")
        if not bak.exists():
            shutil.copy2(p, bak)
        p.write_bytes(text.encode("utf-8"))
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
