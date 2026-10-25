"""
mesh/crypto.py — At-rest encryption, key management, database migration, and encrypted exports.

Addresses Finding S5:
  - Dual-mode encryption at rest:
      1. Optional SQLCipher (`sqlcipher3` / `pysqlcipher3`) full-database page encryption
         with runtime FTS5 capability verification (`has_sqlcipher_with_fts5()`).
      2. Built-in Authenticated Envelope Encryption (`enc:v1:...` for text and `MESHB1` for BLOBs):
         uses `cryptography` AES-256-GCM when installed, with a zero-dependency Python stdlib
         Encrypt-then-MAC (BLAKE2b-256 counter-mode stream + HMAC-SHA256) fallback so encryption
         at rest works on 100% of Python >=3.10 installations.
  - Explicit FTS5 & WAL leakage defense:
      When column encryption is active (or for `local_only=True` rows), on-disk `memories_fts`
      indexing is suppressed (search uses MeshStore's in-memory decrypted lexical index instead),
      and plaintext->encrypted migration runs `PRAGMA wal_checkpoint(TRUNCATE)` + `VACUUM` so zero
      historical plaintext survives in FTS5 shadow tables, freelist pages, or WAL frames.
  - Key management via `MESH_ENCRYPTION_KEY`, `MESH_PASSPHRASE` (PBKDF2-HMAC-SHA256, 600,000
    iterations), or OS keyring (`keyring` package with local `0600` keyfile fallback).
  - Plaintext -> encrypted migration (`encrypt_namespace_db`) with automatic timestamped backup.
  - Authenticated encrypted exports (`encrypt_export_payload` / `decrypt_export_payload`).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .audit import (
    _get_audit_db_path,
    ensure_secure_mesh_dir,
    evict_audit_cache,
    get_mesh_dir,
    reject_symlink,
    secure_file_permissions,
    validate_namespace,
)
from .envutil import env_bool, env_int

_TEXT_PREFIX_GCM = "enc:v1:gcm:"
_TEXT_PREFIX_ETM = "enc:v1:etm:"
_BLOB_MAGIC_GCM = b"MESHB1G\x00"
_BLOB_MAGIC_ETM = b"MESHB1E\x00"

DEFAULT_KDF_ITERATIONS = 600_000
_KEY_LOCK = threading.RLock()
_CACHED_KEY_STATE: Optional[tuple[str, bytes]] = None


def _kdf_iterations() -> int:
    return env_int("MESH_KDF_ITERATIONS", DEFAULT_KDF_ITERATIONS, minimum=10_000, maximum=10_000_000)


def _salt_file_path() -> Path:
    return ensure_secure_mesh_dir().resolve() / ".encryption_salt"


def _master_key_file_path() -> Path:
    return ensure_secure_mesh_dir().resolve() / ".master_key"


def _encryption_marker_path() -> Path:
    return ensure_secure_mesh_dir().resolve() / ".encryption_enabled"


def _write_atomic_private_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically with 0600 permissions (and Windows owner ACLs)."""
    ensure_secure_mesh_dir(path.parent)
    reject_symlink(path)
    tmp_path = path.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
    reject_symlink(tmp_path)
    if os.name == "nt":
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
        secure_file_permissions(path)
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(tmp_path), flags, stat.S_IRUSR | stat.S_IWUSR)
    fchmod = getattr(os, "fchmod", None)
    if fchmod is not None:
        try:
            fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        except (AttributeError, OSError):
            pass
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp_path, path)
    secure_file_permissions(path)


def _get_or_create_kdf_salt() -> bytes:
    salt_file = _salt_file_path()
    reject_symlink(salt_file)
    if salt_file.exists():
        try:
            raw = salt_file.read_bytes()
            if len(raw) >= 16:
                return raw[:16]
        except OSError:
            pass
    salt = secrets.token_bytes(16)
    _write_atomic_private_bytes(salt_file, salt)
    return salt


def derive_key_from_passphrase(
    passphrase: str,
    salt: bytes,
    iterations: Optional[int] = None,
) -> bytes:
    """Derive a 32-byte key from `passphrase` and `salt` using PBKDF2-HMAC-SHA256."""
    if not passphrase:
        raise ValueError("Passphrase cannot be empty.")
    iters = int(iterations) if iterations is not None else _kdf_iterations()
    return hashlib.pbkdf2_hmac(
        "sha256",
        passphrase.encode("utf-8"),
        salt,
        max(10_000, iters),
        dklen=32,
    )


def _decode_raw_key_string(raw_key: str) -> bytes:
    clean = raw_key.strip()
    if len(clean) == 64:
        try:
            return bytes.fromhex(clean)
        except ValueError:
            pass
    try:
        decoded = base64.urlsafe_b64decode(clean + "=" * (-len(clean) % 4))
        if len(decoded) == 32:
            return decoded
    except Exception:
        pass
    return hashlib.sha256(clean.encode("utf-8")).digest()


def _get_or_create_keyring_master_key() -> bytes:
    """
    Load or generate a 256-bit master key in the OS keyring, falling back to
    `~/.mesh/.master_key` (mode 0600) when `MESH_HOME` is isolated or keyring is unavailable.
    """
    use_local = bool(
        os.environ.get("MESH_HOME")
        or os.environ.get("MESH_DIR")
        or os.environ.get("MESH_KEYRING_BACKEND", "").strip().lower() == "local"
    )
    if not use_local:
        try:
            import keyring  # type: ignore[import-not-found]

            existing = keyring.get_password("mesh:crypto", "master_key")
            if existing:
                return _decode_raw_key_string(existing)
            new_key = secrets.token_bytes(32)
            keyring.set_password("mesh:crypto", "master_key", new_key.hex())
            return new_key
        except Exception:
            pass

    key_file = _master_key_file_path()
    reject_symlink(key_file)
    if key_file.exists():
        try:
            raw = key_file.read_text(encoding="utf-8").strip()
            if len(raw) >= 32:
                return _decode_raw_key_string(raw)
        except OSError:
            pass
    new_key = secrets.token_bytes(32)
    _write_atomic_private_bytes(key_file, new_key.hex().encode("utf-8"))
    return new_key


def is_encryption_enabled() -> bool:
    """Return True if at-rest encryption is active for the current Mesh environment (Finding S5)."""
    if env_bool("MESH_ENCRYPT_AT_REST", False):
        return True
    if (os.environ.get("MESH_PASSPHRASE") or "").strip():
        return True
    if (os.environ.get("MESH_ENCRYPTION_KEY") or "").strip():
        return True
    global _ENC_MEMO
    try:
        from .audit import cached_resolved_mesh_dir, get_mesh_dir, reject_symlink

        _mdir = os.fspath(get_mesh_dir())
        _now = time.monotonic()
        _memo = _ENC_MEMO
        if _memo is not None and _memo[0] == _mdir and (_now - _memo[1]) < _enc_memo_seconds():
            return _memo[2]
        reject_symlink(get_mesh_dir())
        os.stat(os.path.join(os.fspath(cached_resolved_mesh_dir()), ".encryption_enabled"))
        _ENC_MEMO = (_mdir, _now, True)
        return True
    except OSError:
        _ENC_MEMO = (_mdir, _now, False)
        return False


_ENC_MEMO: Optional[tuple[str, float, bool]] = None


def _enc_memo_seconds() -> float:
    raw = os.environ.get("MESH_FS_MEMO_MS")
    try:
        return max(0.0, float(raw) / 1000.0) if raw is not None else 0.01
    except ValueError:
        return 0.01


def _invalidate_enc_memo() -> None:
    global _ENC_MEMO
    _ENC_MEMO = None


def enable_encryption_at_rest(passphrase: Optional[str] = None) -> None:
    """Persistently enable at-rest encryption in the active Mesh directory."""
    if passphrase:
        os.environ["MESH_PASSPHRASE"] = passphrase
    _write_atomic_private_bytes(_encryption_marker_path(), b"1")
    _invalidate_enc_memo()
    get_active_master_key()


def disable_encryption_at_rest() -> None:
    """Remove the persistent encryption marker file."""
    global _CACHED_KEY_STATE
    try:
        _encryption_marker_path().unlink(missing_ok=True)
    except OSError:
        pass
    _invalidate_enc_memo()
    with _KEY_LOCK:
        _CACHED_KEY_STATE = None


def get_active_master_key(passphrase: Optional[str] = None) -> bytes:
    """
    Resolve the active 32-byte master encryption key with deterministic precedence:
      1. Explicit `passphrase` argument (if provided)
      2. `MESH_ENCRYPTION_KEY` environment variable
      3. `MESH_PASSPHRASE` environment variable + `~/.mesh/.encryption_salt`
      4. OS keyring / `~/.mesh/.master_key`
    """
    global _CACHED_KEY_STATE
    mesh_dir_str = str(get_mesh_dir().resolve())
    if passphrase is not None and passphrase.strip():
        salt = _get_or_create_kdf_salt()
        return derive_key_from_passphrase(passphrase.strip(), salt)

    env_key = (os.environ.get("MESH_ENCRYPTION_KEY") or "").strip()
    env_pass = (os.environ.get("MESH_PASSPHRASE") or "").strip()
    iters = _kdf_iterations()
    state_fingerprint = f"{mesh_dir_str}|k:{hashlib.sha256(env_key.encode()).hexdigest()[:16]}|p:{hashlib.sha256(env_pass.encode()).hexdigest()[:16]}|i:{iters}"

    with _KEY_LOCK:
        if _CACHED_KEY_STATE is not None and _CACHED_KEY_STATE[0] == state_fingerprint:
            return _CACHED_KEY_STATE[1]

        if env_key:
            key = _decode_raw_key_string(env_key)
        elif env_pass:
            salt = _get_or_create_kdf_salt()
            key = derive_key_from_passphrase(env_pass, salt, iterations=iters)
        else:
            key = _get_or_create_keyring_master_key()

        _CACHED_KEY_STATE = (state_fingerprint, key)
        return key


# ==============================================================================
# SQLCipher Detection & Verification (Finding S5 / 2d)
# ==============================================================================


def has_sqlcipher_with_fts5() -> bool:
    """
    Verify whether `sqlcipher3` (or `pysqlcipher3`) is installed AND compiled with FTS5 support.
    """
    for mod_name in ("sqlcipher3.dbapi2", "pysqlcipher3.dbapi2"):
        try:
            mod = __import__(mod_name, fromlist=["connect"])
            conn = mod.connect(":memory:")
            try:
                conn.execute("PRAGMA key = \"x'0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'\"")
                conn.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(content)")
                conn.execute("INSERT INTO _fts5_probe(content) VALUES ('probe')")
                return True
            finally:
                conn.close()
        except Exception:
            continue
    return False


# ==============================================================================
# AEAD Primitives: AES-256-GCM (optional `cryptography`) + Stdlib BLAKE2b-CTR-HMAC
# ==============================================================================


def _has_aesgcm() -> bool:
    if os.environ.get("MESH_FORCE_STDLIB_CRYPTO", "").strip().lower() in ("1", "true", "yes"):
        return False
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401

        return True
    except Exception:
        return False


def _derive_etm_subkeys(master_key: bytes, nonce: bytes) -> tuple[bytes, bytes]:
    """Derive separated 32-byte encryption and MAC subkeys using BLAKE2b keyed mode."""
    enc_key = hashlib.blake2b(nonce, key=master_key, digest_size=32, person=b"mesh-enc").digest()
    mac_key = hashlib.blake2b(nonce, key=master_key, digest_size=32, person=b"mesh-mac").digest()
    return enc_key, mac_key


def _blake2b_ctr_xor(data: bytes, enc_key: bytes, nonce: bytes) -> bytes:
    """XOR `data` with a BLAKE2b-512 counter-mode keystream (vectorized via NumPy)."""
    if not data:
        return b""
    n_len = len(data)
    n_blocks = (n_len + 63) // 64
    blocks = [
        hashlib.blake2b(
            nonce + blk.to_bytes(8, "little"),
            key=enc_key,
            digest_size=64,
            person=b"mesh-ctr",
        ).digest()
        for blk in range(n_blocks)
    ]
    stream = b"".join(blocks)[:n_len]
    a_arr = np.frombuffer(data, dtype=np.uint8)
    b_arr = np.frombuffer(stream, dtype=np.uint8)
    return np.bitwise_xor(a_arr, b_arr).tobytes()


def _aead_encrypt_bytes(
    plaintext: bytes,
    key: bytes,
    aad: bytes = b"",
) -> tuple[str, bytes, bytes]:
    """
    Encrypt `plaintext` with `key` and `aad`.
    Returns `(mode, nonce, ciphertext_with_tag)` where `mode` is `'gcm'` or `'etm'`.
    """
    if _has_aesgcm():
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = secrets.token_bytes(12)
        ct_and_tag = AESGCM(key).encrypt(nonce, plaintext, aad or None)
        return ("gcm", nonce, ct_and_tag)

    nonce = secrets.token_bytes(16)
    enc_key, mac_key = _derive_etm_subkeys(key, nonce)
    ciphertext = _blake2b_ctr_xor(plaintext, enc_key, nonce)
    mac_input = nonce + len(aad).to_bytes(8, "little") + aad + ciphertext
    tag = hmac.new(mac_key, mac_input, hashlib.sha256).digest()
    return ("etm", nonce, tag + ciphertext)


def _aead_decrypt_bytes(
    mode: str,
    nonce: bytes,
    ct_with_tag: bytes,
    key: bytes,
    aad: bytes = b"",
) -> bytes:
    """Verify authenticity and decrypt `ct_with_tag`."""
    if mode == "gcm":
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        return AESGCM(key).decrypt(nonce, ct_with_tag, aad or None)

    if mode == "etm":
        if len(ct_with_tag) < 32:
            raise ValueError("Truncated ciphertext tag in encrypted payload.")
        tag = ct_with_tag[:32]
        ciphertext = ct_with_tag[32:]
        enc_key, mac_key = _derive_etm_subkeys(key, nonce)
        mac_input = nonce + len(aad).to_bytes(8, "little") + aad + ciphertext
        expected_tag = hmac.new(mac_key, mac_input, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected_tag):
            raise ValueError("Ciphertext authentication failed (invalid key or tampered data).")
        return _blake2b_ctr_xor(ciphertext, enc_key, nonce)

    raise ValueError(f"Unsupported encryption mode '{mode}'.")


def is_encrypted_text(value: Any) -> bool:
    """Return True if `value` is an `enc:v1:...` encrypted string."""
    return isinstance(value, str) and (
        value.startswith(_TEXT_PREFIX_GCM) or value.startswith(_TEXT_PREFIX_ETM)
    )


def encrypt_text_for_storage(
    plaintext: str,
    aad: str = "",
    key: Optional[bytes] = None,
) -> str:
    """
    Encrypt a string into an authenticated `enc:v1:<mode>:<nonce_b64>:<ct_b64>` envelope.
    Idempotent: if `plaintext` is already encrypted with `enc:v1:`, returns it unchanged.
    """
    if not isinstance(plaintext, str):
        plaintext = str(plaintext or "")
    if is_encrypted_text(plaintext):
        return plaintext
    active_key = key if key is not None else get_active_master_key()
    mode, nonce, ct_with_tag = _aead_encrypt_bytes(
        plaintext.encode("utf-8"),
        active_key,
        aad.encode("utf-8"),
    )
    nonce_b64 = base64.urlsafe_b64encode(nonce).decode("ascii").rstrip("=")
    ct_b64 = base64.urlsafe_b64encode(ct_with_tag).decode("ascii").rstrip("=")
    prefix = _TEXT_PREFIX_GCM if mode == "gcm" else _TEXT_PREFIX_ETM
    return f"{prefix}{nonce_b64}:{ct_b64}"


def decrypt_text_if_encrypted(
    value: Optional[str],
    aad: str = "",
    key: Optional[bytes] = None,
) -> str:
    """
    If `value` carries an `enc:v1:...` prefix, verify and decrypt it; otherwise return `value` unchanged.
    """
    if value is None:
        return ""
    if not is_encrypted_text(value):
        return str(value)

    if value.startswith(_TEXT_PREFIX_GCM):
        mode = "gcm"
        rest = value[len(_TEXT_PREFIX_GCM) :]
    else:
        mode = "etm"
        rest = value[len(_TEXT_PREFIX_ETM) :]

    nonce_b64, sep, ct_b64 = rest.partition(":")
    if not sep:
        raise ValueError("Malformed encrypted text envelope.")
    nonce = base64.urlsafe_b64decode(nonce_b64 + "=" * (-len(nonce_b64) % 4))
    ct_with_tag = base64.urlsafe_b64decode(ct_b64 + "=" * (-len(ct_b64) % 4))
    active_key = key if key is not None else get_active_master_key()
    plain_bytes = _aead_decrypt_bytes(mode, nonce, ct_with_tag, active_key, aad.encode("utf-8"))
    return plain_bytes.decode("utf-8")


def is_encrypted_blob(blob: Any) -> bool:
    """Return True if `blob` carries the `MESHB1` encrypted binary header."""
    return isinstance(blob, (bytes, bytearray, memoryview)) and bytes(blob[:8]) in (
        _BLOB_MAGIC_GCM,
        _BLOB_MAGIC_ETM,
    )


def encrypt_blob_for_storage(
    blob: Optional[bytes],
    aad: str = "",
    key: Optional[bytes] = None,
) -> Optional[bytes]:
    """Encrypt a binary BLOB (e.g. a 384-dim float32 vector) with `MESHB1` header."""
    if blob is None:
        return None
    raw = bytes(blob)
    if is_encrypted_blob(raw):
        return raw
    active_key = key if key is not None else get_active_master_key()
    mode, nonce, ct_with_tag = _aead_encrypt_bytes(raw, active_key, aad.encode("utf-8"))
    magic = _BLOB_MAGIC_GCM if mode == "gcm" else _BLOB_MAGIC_ETM
    return magic + len(nonce).to_bytes(1, "big") + nonce + ct_with_tag


def decrypt_blob_if_encrypted(
    blob: Optional[bytes],
    aad: str = "",
    key: Optional[bytes] = None,
) -> Optional[bytes]:
    """Decrypt a `MESHB1` binary BLOB or return raw unencrypted BLOB bytes unchanged."""
    if blob is None:
        return None
    raw = bytes(blob)
    if not is_encrypted_blob(raw):
        return raw
    magic = raw[:8]
    mode = "gcm" if magic == _BLOB_MAGIC_GCM else "etm"
    nonce_len = int(raw[8])
    nonce = raw[9 : 9 + nonce_len]
    ct_with_tag = raw[9 + nonce_len :]
    active_key = key if key is not None else get_active_master_key()
    return _aead_decrypt_bytes(mode, nonce, ct_with_tag, active_key, aad.encode("utf-8"))


# ==============================================================================
# Plaintext -> Encrypted Database Migration with Backup & VACUUM (Finding S5)
# ==============================================================================


def encrypt_namespace_db(
    namespace: str = "shared",
    passphrase: Optional[str] = None,
    keep_backup: bool = True,
) -> dict[str, Any]:
    """
    Migrate an existing namespace database (`mesh_<ns>.db` and `audit_<ns>.db`) to
    encrypted-at-rest storage (Finding S5):
      1. Enable encryption at rest and derive the master key.
      2. Flush pending telemetry and evict cached connections.
      3. Checkpoint and truncate the WAL (`PRAGMA wal_checkpoint(TRUNCATE)`).
      4. Create atomic 0600 backups under `~/.mesh/backups/`.
      5. Encrypt all plaintext fields in `memories`, `queries`, `contradictions`, and `audit_log`.
      6. Purge `memories_fts` so zero plaintext tokens remain in FTS5 shadow tables on disk.
      7. Run `VACUUM` on both databases so SQLite rewrites every page from scratch.
    """
    from .namespaces import _evict_namespace_caches
    from .store import NamespaceNotFoundError, flush_all_telemetry, namespace_write_lock

    clean_ns = validate_namespace(namespace)
    flush_all_telemetry()
    enable_encryption_at_rest(passphrase=passphrase)
    key = get_active_master_key(passphrase=passphrase)

    base_dir = ensure_secure_mesh_dir().resolve()
    db_path = (base_dir / f"mesh_{clean_ns}.db").resolve()
    audit_path = _get_audit_db_path(clean_ns)

    if not db_path.exists() and not audit_path.exists():
        raise NamespaceNotFoundError(f"Namespace '{clean_ns}' does not exist.")

    _evict_namespace_caches(clean_ns, db_path)
    evict_audit_cache(clean_ns)

    backup_dir = ensure_secure_mesh_dir(base_dir / "backups")
    ts_label = int(time.time() * 1000)
    backup_files: list[str] = []
    encrypted_memories = 0
    encrypted_queries = 0
    encrypted_contradictions = 0
    encrypted_audits = 0

    with namespace_write_lock(clean_ns, timeout=30.0):
        if db_path.exists():
            reject_symlink(db_path)
            conn = sqlite3.connect(db_path, timeout=10.0)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()

            bak_db = backup_dir / f"mesh_{clean_ns}.db.bak.{ts_label}"
            shutil.copy2(db_path, bak_db)
            secure_file_permissions(bak_db)
            backup_files.append(str(bak_db))

            conn = sqlite3.connect(db_path, timeout=10.0)
            try:
                conn.execute("PRAGMA secure_delete=ON")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    # 1. Encrypt memories
                    rows = conn.execute(
                        "SELECT id, content, tags, embedding FROM memories WHERE namespace = ?",
                        (clean_ns,),
                    ).fetchall()
                    for mid, content, tags, emb_blob in rows:
                        new_content = (
                            encrypt_text_for_storage(str(content), key=key)
                            if content is not None and not is_encrypted_text(content)
                            else content
                        )
                        new_tags = (
                            encrypt_text_for_storage(str(tags), key=key)
                            if tags is not None and not is_encrypted_text(tags)
                            else tags
                        )
                        new_blob = (
                            encrypt_blob_for_storage(emb_blob, key=key)
                            if emb_blob is not None and not is_encrypted_blob(emb_blob)
                            else emb_blob
                        )
                        if new_content != content or new_tags != tags or new_blob != emb_blob:
                            conn.execute(
                                "UPDATE memories SET content = ?, tags = ?, embedding = ? WHERE id = ? AND namespace = ?",
                                (new_content, new_tags, new_blob, mid, clean_ns),
                            )
                            encrypted_memories += 1

                    # 2. Encrypt queries
                    q_rows = conn.execute(
                        "SELECT id, query_text FROM queries WHERE namespace = ?",
                        (clean_ns,),
                    ).fetchall()
                    for qid, qtext in q_rows:
                        if qtext and not is_encrypted_text(qtext):
                            conn.execute(
                                "UPDATE queries SET query_text = ? WHERE id = ? AND namespace = ?",
                                (encrypt_text_for_storage(str(qtext), key=key), qid, clean_ns),
                            )
                            encrypted_queries += 1

                    # 3. Encrypt contradictions
                    c_rows = conn.execute(
                        "SELECT id, new_content, existing_content FROM contradictions WHERE namespace = ?",
                        (clean_ns,),
                    ).fetchall()
                    for cid, new_c, exist_c in c_rows:
                        enc_new = (
                            encrypt_text_for_storage(str(new_c), key=key)
                            if new_c and not is_encrypted_text(new_c)
                            else new_c
                        )
                        enc_exist = (
                            encrypt_text_for_storage(str(exist_c), key=key)
                            if exist_c and not is_encrypted_text(exist_c)
                            else exist_c
                        )
                        if enc_new != new_c or enc_exist != exist_c:
                            conn.execute(
                                "UPDATE contradictions SET new_content = ?, existing_content = ? WHERE id = ? AND namespace = ?",
                                (enc_new, enc_exist, cid, clean_ns),
                            )
                            encrypted_contradictions += 1

                    # 4. Purge on-disk FTS5 table so no plaintext tokens remain in shadow tables
                    try:
                        conn.execute("DELETE FROM memories_fts")
                    except sqlite3.OperationalError:
                        pass

                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS mesh_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO mesh_meta (key, value) VALUES ('encrypted_at_rest', '1')"
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO mesh_meta (key, value) VALUES ('content_version', "
                        "COALESCE((SELECT CAST(value AS INTEGER) + 1 FROM mesh_meta WHERE key = 'content_version'), '2'))"
                    )
                    conn.commit()
                except Exception:
                    if conn.in_transaction:
                        conn.rollback()
                    raise

                # 5. Rewrite all pages via VACUUM and truncate WAL
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
            secure_file_permissions(db_path)

        if audit_path.exists():
            reject_symlink(audit_path)
            conn_a = sqlite3.connect(audit_path, timeout=10.0)
            try:
                conn_a.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn_a.close()

            bak_audit = backup_dir / f"audit_{clean_ns}.db.bak.{ts_label}"
            shutil.copy2(audit_path, bak_audit)
            secure_file_permissions(bak_audit)
            backup_files.append(str(bak_audit))

            conn_a = sqlite3.connect(audit_path, timeout=10.0)
            try:
                conn_a.execute("PRAGMA secure_delete=ON")
                conn_a.execute("BEGIN IMMEDIATE")
                try:
                    a_rows = conn_a.execute(
                        "SELECT id, query, content, top_result FROM audit_log WHERE namespace = ?",
                        (clean_ns,),
                    ).fetchall()
                    for aid, q_val, c_val, top_val in a_rows:
                        new_q = (
                            encrypt_text_for_storage(str(q_val), key=key)
                            if q_val is not None and not is_encrypted_text(q_val)
                            else q_val
                        )
                        new_c = (
                            encrypt_text_for_storage(str(c_val), key=key)
                            if c_val is not None and not is_encrypted_text(c_val)
                            else c_val
                        )
                        new_top = (
                            encrypt_text_for_storage(str(top_val), key=key)
                            if top_val is not None and not is_encrypted_text(top_val)
                            else top_val
                        )
                        if new_q != q_val or new_c != c_val or new_top != top_val:
                            conn_a.execute(
                                "UPDATE audit_log SET query = ?, content = ?, top_result = ? WHERE id = ?",
                                (new_q, new_c, new_top, aid),
                            )
                            encrypted_audits += 1
                    conn_a.commit()
                except Exception:
                    if conn_a.in_transaction:
                        conn_a.rollback()
                    raise
                conn_a.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn_a.execute("VACUUM")
                conn_a.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn_a.close()
            secure_file_permissions(audit_path)

    if not keep_backup:
        for bf in backup_files:
            try:
                Path(bf).unlink(missing_ok=True)
            except OSError:
                pass
        backup_files = []

    _evict_namespace_caches(clean_ns, db_path)
    evict_audit_cache(clean_ns)

    return {
        "namespace": clean_ns,
        "encrypted": True,
        "encrypted_memories": encrypted_memories,
        "encrypted_queries": encrypted_queries,
        "encrypted_contradictions": encrypted_contradictions,
        "encrypted_audits": encrypted_audits,
        "backups": backup_files,
    }


# ==============================================================================
# Encrypted Exports (Finding S5 / 2d)
# ==============================================================================


def encrypt_export_payload(
    export_data: dict[str, Any],
    passphrase: Optional[str] = None,
) -> dict[str, Any]:
    """
    Encrypt an export dictionary into an authenticated JSON envelope (`mesh_encrypted_export=True`).
    If `passphrase` is provided, derives a per-export key with a fresh 16-byte random salt;
    otherwise uses the active master key.
    """
    raw_json = json.dumps(export_data, ensure_ascii=False)
    salt = secrets.token_bytes(16)
    iters = _kdf_iterations()
    if passphrase and passphrase.strip():
        key = derive_key_from_passphrase(passphrase.strip(), salt, iterations=iters)
        kdf_name = "pbkdf2-hmac-sha256"
    else:
        key = get_active_master_key()
        kdf_name = "master-key"
    ciphertext = encrypt_text_for_storage(raw_json, aad="mesh-export-v1", key=key)
    return {
        "mesh_encrypted_export": True,
        "mesh_version": export_data.get("mesh_version"),
        "namespace": export_data.get("namespace"),
        "exported_at": export_data.get("exported_at"),
        "kdf": kdf_name,
        "kdf_iterations": iters,
        "salt": base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        "ciphertext": ciphertext,
    }


def decrypt_export_payload(
    payload: dict[str, Any],
    passphrase: Optional[str] = None,
) -> dict[str, Any]:
    """Decrypt a `mesh_encrypted_export` JSON dictionary back into its original export dict."""
    if not isinstance(payload, dict) or not payload.get("mesh_encrypted_export"):
        return payload
    ciphertext = str(payload.get("ciphertext") or "")
    if not ciphertext:
        raise ValueError("Encrypted export file is missing 'ciphertext'.")
    kdf_name = str(payload.get("kdf") or "pbkdf2-hmac-sha256")
    salt_b64 = str(payload.get("salt") or "")
    iters = int(payload.get("kdf_iterations") or _kdf_iterations())

    if kdf_name == "pbkdf2-hmac-sha256":
        resolved_pass = passphrase or (os.environ.get("MESH_PASSPHRASE") or "").strip()
        if not resolved_pass:
            raise ValueError(
                "Export file is passphrase-encrypted; provide a passphrase or set MESH_PASSPHRASE."
            )
        salt = base64.urlsafe_b64decode(salt_b64 + "=" * (-len(salt_b64) % 4))
        key = derive_key_from_passphrase(resolved_pass, salt, iterations=iters)
    else:
        key = get_active_master_key(passphrase=passphrase)

    decrypted_json = decrypt_text_if_encrypted(ciphertext, aad="mesh-export-v1", key=key)
    parsed = json.loads(decrypted_json)
    if not isinstance(parsed, dict):
        raise ValueError("Decrypted export payload is not a JSON object.")
    return parsed
