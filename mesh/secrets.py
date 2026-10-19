"""
mesh/secrets.py — First-class secret detection, redaction, locator references, and runtime injection.

Addresses Findings:
  - S1: Secret locator model (`SecretLocator`, `secret_ref`) & OS keyring / CLI resolvers + `mesh run`
  - S2: Unified detection and redaction rules (no false assurance on `was`, quoted Bearer, unclosed PEM,
        or intervening words like "the prod database password for checkout is hunter2")
  - S3: Full coverage for multi-word passphrases, short PINs/passwords, JSON (`$2b$...`, commas),
        YAML block scalars, `.env` assignments, and CLI flags (`--password`, `--token`, `--api-key`)
  - S4: Context + entropy + benign-word filtering so ordinary prose ("token limit is 4096",
        "secret santa", "The refresh token is automatically rotated", "pwd: /path") is never mangled
  - S10: Decouples local secret redaction (`redact_secrets_local`, which preserves emails) from
         cloud log scrubbing (`redact_secrets_for_cloud`, which also redacts emails)
  - N2: Bounded URL userinfo redaction that never crosses quotes/commas or port paths
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import threading
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional

from .audit import ensure_secure_mesh_dir, secure_file_permissions, validate_namespace

# Finding S8 / S10: Shared invisible character class (soft hyphen, zero-width joiners, bidi controls, tags)
INVISIBLE_CHAR_CLASS = (
    r"\u00ad"                  # SOFT HYPHEN
    r"\u034f"                  # COMBINING GRAPHEME JOINER
    r"\u061c"                  # ARABIC LETTER MARK
    r"\u115f\u1160"            # Hangul fillers
    r"\u17b4\u17b5"            # Khmer inherent vowels
    r"\u180b-\u180f"           # Mongolian FVS + vowel separator
    r"\u200b-\u200f"           # zero-width space .. right-to-left mark
    r"\u202a-\u202e"           # bidi embeddings / overrides
    r"\u2060-\u2064"           # word joiner .. invisible plus
    r"\u2066-\u2069"           # bidi isolates
    r"\ufeff"                  # BOM / zero-width no-break space
    r"\ufe00-\ufe0f"           # variation selectors
    r"\uffa0"                  # halfwidth Hangul filler
    r"\U000e0000-\U000e007f"   # tag characters
    r"\U000e0100-\U000e01ef"   # variation selectors supplement
)
_INVISIBLE_CHAR_CLASS = INVISIBLE_CHAR_CLASS
ZERO_WIDTH_RE = re.compile(f"[{INVISIBLE_CHAR_CLASS}]")
_ZERO_WIDTH_RE = ZERO_WIDTH_RE

# Finding S1: Supported secret locator providers and URI schemes
VALID_SECRET_PROVIDERS: frozenset[str] = frozenset(
    {
        "env",
        "keyring",
        "1password",
        "bitwarden",
        "sops",
        "aws_sm",
        "gcp_sm",
        "vault",
    }
)

_PROVIDER_SCHEME_MAP: dict[str, str] = {
    "env": "env://",
    "keyring": "keyring://",
    "1password": "op://",
    "bitwarden": "bw://",
    "sops": "sops://",
    "aws_sm": "awssm://",
    "gcp_sm": "gcpsm://",
    "vault": "vault://",
}

_SCHEME_TO_PROVIDER: dict[str, str] = {v: k for k, v in _PROVIDER_SCHEME_MAP.items()}
_VALID_KEY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_ENV_REF_ONLY_RE = re.compile(r"^\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)$")
_PLACEHOLDER_ONLY_RE = re.compile(
    r"^\[(?:REDACTED_[A-Z_]+|SECRET_REF:[^\]]+)\]$",
    re.IGNORECASE,
)

# Finding S4: Nouns that follow "token", "password", "secret", or "key" to form a benign subject phrase
_BENIGN_SUBJECT_NOUNS: frozenset[str] = frozenset(
    {
        "limit", "limits", "count", "counts", "budget", "size", "length",
        "policy", "policies", "rotation", "rotations", "reset", "resets",
        "manager", "managers", "vault", "vaults", "expiry", "expiration",
        "ttl", "timeout", "lifetime", "header", "headers", "endpoint",
        "endpoints", "schema", "table", "column", "columns", "field",
        "fields", "format", "type", "types", "algorithm", "check",
        "validation", "validator", "window", "bucket", "rate", "quota",
        " santa", "santa", "ring", "rings", "store", "storage", "file",
        "path", "location", "variable", "env", "name", "id", "identifier",
        "hash", "hashing", "salting", "complexity", "requirement", "requirements",
        "age", "history", "reuse", "sharing", "leak", "detection", "scanner",
    }
)

# Finding S4: Benign English status/prose words that are never secret values when unquoted
_BENIGN_VALUE_WORDS: frozenset[str] = frozenset(
    {
        "true", "false", "null", "none", "nil", "undefined", "empty",
        "required", "optional", "enabled", "disabled", "active", "inactive",
        "valid", "invalid", "expired", "revoked", "rotated", "stored",
        "saved", "kept", "located", "configured", "missing", "set", "unset",
        "redacted", "encrypted", "hashed", "masked", "hidden", "public",
        "private", "local", "automatically", "manually", "always", "never",
        "authenticated", "verified", "deprecated", "supported", "needed",
        "mandatory", "recommended", "forgotten", "changed", "updated",
        "deleted", "removed", "generated", "created", "injected", "loaded",
        "passed", "provided", "supplied", "checked", "validated", "enforced",
        "allowed", "denied", "blocked", "rejected", "accepted", "ignored",
        "used", "shared", "scoped", "locked", "unlocked", "cached", "flushed",
    }
)

# Common dictionary passwords that are all-lowercase but must still be caught in `password is <val>`
_COMMON_LOWERCASE_PASSWORDS: frozenset[str] = frozenset(
    {
        "password", "hunter", "secret", "admin", "letmein", "qwerty",
        "welcome", "monkey", "dragon", "master", "shadow", "supersecret",
        "passphrase", "changeme", "foobar", "baseball", "football",
        "trustno1", "iloveyou", "sunshine", "princess", "whatever",
    }
)

_VAULT_LOCK = threading.RLock()


# ==============================================================================
# Secret Locator Model & Keyring / CLI Resolver (Finding S1)
# ==============================================================================


def validate_secret_key_name(key_name: str) -> str:
    """Validate a logical secret key name (e.g. 'STRIPE_SECRET_KEY' or 'db_password')."""
    if not isinstance(key_name, str):
        raise ValueError("Secret key_name must be a string.")
    clean = key_name.strip()
    if not clean or not _VALID_KEY_NAME_RE.match(clean):
        raise ValueError(
            f"Invalid secret key_name '{key_name}'. Must match ^[A-Za-z_][A-Za-z0-9_.-]{{0,127}}$."
        )
    return clean


@dataclasses.dataclass(frozen=True)
class SecretLocator:
    """
    Structured pointer to where a secret lives without ever storing its plaintext value (Finding S1).
    """

    provider: str
    uri: str
    key_name: str
    description: str = ""
    namespace: str = "shared"

    def __post_init__(self) -> None:
        prov = (self.provider or "").strip().lower()
        if prov not in VALID_SECRET_PROVIDERS:
            raise ValueError(
                f"Invalid secret provider '{self.provider}'. Must be one of {sorted(VALID_SECRET_PROVIDERS)}."
            )
        clean_key = validate_secret_key_name(self.key_name)
        clean_uri = (self.uri or "").strip()
        expected_prefix = _PROVIDER_SCHEME_MAP[prov]
        if not clean_uri.startswith(expected_prefix) or len(clean_uri) <= len(expected_prefix):
            raise ValueError(
                f"Invalid URI '{self.uri}' for provider '{prov}'; must start with '{expected_prefix}'."
            )
        if any(ch in clean_uri for ch in ("\r", "\n", "\x00", " ", "\t")):
            raise ValueError("Secret locator URI cannot contain whitespace or control characters.")
        clean_ns = validate_namespace(self.namespace or "shared")
        object.__setattr__(self, "provider", prov)
        object.__setattr__(self, "key_name", clean_key)
        object.__setattr__(self, "uri", clean_uri)
        object.__setattr__(self, "namespace", clean_ns)
        object.__setattr__(self, "description", str(self.description or "").strip())

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "uri": self.uri,
            "key_name": self.key_name,
            "namespace": self.namespace,
            "description": self.description,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    def format_placeholder(self) -> str:
        return f"[SECRET_REF:{self.uri}]"

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_namespace: str = "shared") -> "SecretLocator":
        if not isinstance(data, dict):
            raise ValueError("secret_ref must be a dict or URI string.")
        uri = str(data.get("uri") or "").strip()
        provider = str(data.get("provider") or "").strip().lower()
        if not provider and uri:
            for scheme, prov in _SCHEME_TO_PROVIDER.items():
                if uri.startswith(scheme):
                    provider = prov
                    break
        key_name = str(data.get("key_name") or "").strip()
        if not key_name and uri:
            tail = uri.rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            key_name = re.sub(r"[^A-Za-z0-9_.-]", "_", tail).lstrip("0123456789.-") or "SECRET_KEY"
        if not uri and provider and key_name:
            prefix = _PROVIDER_SCHEME_MAP.get(provider, "")
            if provider == "keyring":
                ns = validate_namespace(str(data.get("namespace") or default_namespace))
                uri = f"keyring://mesh/{ns}/{key_name}"
            elif prefix:
                uri = f"{prefix}{key_name}"
        return cls(
            provider=provider,
            uri=uri,
            key_name=key_name,
            description=str(data.get("description") or ""),
            namespace=str(data.get("namespace") or default_namespace),
        )

    @classmethod
    def from_uri(
        cls,
        uri: str,
        key_name: Optional[str] = None,
        description: str = "",
        namespace: str = "shared",
    ) -> "SecretLocator":
        clean_uri = (uri or "").strip()
        provider = ""
        for scheme, prov in _SCHEME_TO_PROVIDER.items():
            if clean_uri.startswith(scheme):
                provider = prov
                break
        if not provider:
            raise ValueError(
                f"Unrecognized secret URI scheme in '{uri}'. Expected one of {sorted(_PROVIDER_SCHEME_MAP.values())}."
            )
        inferred_key = key_name
        if not inferred_key:
            tail = clean_uri.rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            inferred_key = re.sub(r"[^A-Za-z0-9_.-]", "_", tail).lstrip("0123456789.-") or "SECRET_KEY"
        return cls(
            provider=provider,
            uri=clean_uri,
            key_name=inferred_key,
            description=description,
            namespace=namespace,
        )


def normalize_secret_ref(
    secret_ref: Optional[SecretLocator | dict[str, Any] | str],
    default_namespace: str = "shared",
) -> Optional[dict[str, str]]:
    """Validate and normalize a `secret_ref` input into a canonical dictionary."""
    if secret_ref is None:
        return None
    if isinstance(secret_ref, SecretLocator):
        return secret_ref.to_dict()
    if isinstance(secret_ref, str):
        s = secret_ref.strip()
        if not s:
            return None
        if s.startswith("{"):
            parsed = json.loads(s)
            return SecretLocator.from_dict(parsed, default_namespace=default_namespace).to_dict()
        return SecretLocator.from_uri(s, namespace=default_namespace).to_dict()
    if isinstance(secret_ref, dict):
        return SecretLocator.from_dict(secret_ref, default_namespace=default_namespace).to_dict()
    raise ValueError("Invalid secret_ref: expected SecretLocator, dict, or URI string.")


# ==============================================================================
# Local Vault & OS Keyring Storage (Finding S1, 2a, 2b)
# ==============================================================================


def _local_vault_path() -> Path:
    return ensure_secure_mesh_dir().resolve() / "keyring_vault.json"


def _use_isolated_local_vault() -> bool:
    """
    When `MESH_HOME` / `MESH_DIR` is overridden (e.g. in tests or isolated workspaces)
    or `MESH_KEYRING_BACKEND=local` is set, use the isolated local vault inside `get_mesh_dir()`
    so tests never pollute the host OS keychain.
    """
    if os.environ.get("MESH_KEYRING_BACKEND", "").strip().lower() == "local":
        return True
    if os.environ.get("MESH_HOME") or os.environ.get("MESH_DIR"):
        return True
    return False


def _read_local_vault_locked() -> dict[str, str]:
    vault_file = _local_vault_path()
    if not vault_file.exists():
        return {}
    try:
        raw = vault_file.read_text(encoding="utf-8")
        try:
            from .crypto import decrypt_text_if_encrypted

            raw = decrypt_text_if_encrypted(raw)
        except Exception:
            pass
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def _write_local_vault_locked(data: dict[str, str]) -> None:
    vault_file = _local_vault_path()
    ensure_secure_mesh_dir(vault_file.parent)
    serialized = json.dumps(data, indent=2, sort_keys=True)
    try:
        from .crypto import encrypt_text_for_storage, is_encryption_enabled

        if is_encryption_enabled():
            serialized = encrypt_text_for_storage(serialized)
    except Exception:
        pass
    tmp_path = vault_file.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
    if os.name == "nt":
        tmp_path.write_text(serialized, encoding="utf-8")
        os.replace(tmp_path, vault_file)
        secure_file_permissions(vault_file)
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(tmp_path), flags, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(serialized)
    os.replace(tmp_path, vault_file)
    secure_file_permissions(vault_file)


def store_secret_in_keyring(
    namespace: str,
    key_name: str,
    secret_value: str,
    description: str = "",
) -> SecretLocator:
    """
    Store `secret_value` in the OS keyring (or isolated local vault fallback) and return
    a `SecretLocator` pointing to `keyring://mesh/<namespace>/<key_name>` (Finding S1).
    """
    clean_ns = validate_namespace(namespace)
    clean_key = validate_secret_key_name(key_name)
    if not secret_value:
        raise ValueError("secret_value cannot be empty.")

    service_name = f"mesh:{clean_ns}"
    stored_in_os = False

    if not _use_isolated_local_vault():
        try:
            import keyring  # type: ignore[import-not-found]

            keyring.set_password(service_name, clean_key, secret_value)
            stored_in_os = True
        except Exception:
            stored_in_os = False

    if not stored_in_os:
        with _VAULT_LOCK:
            vault = _read_local_vault_locked()
            vault[f"{clean_ns}:{clean_key}"] = secret_value
            _write_local_vault_locked(vault)

    uri = f"keyring://mesh/{clean_ns}/{clean_key}"
    return SecretLocator(
        provider="keyring",
        uri=uri,
        key_name=clean_key,
        description=description or f"Secret '{clean_key}' stored in keyring for namespace '{clean_ns}'",
        namespace=clean_ns,
    )


def get_secret_from_keyring(namespace: str, key_name: str) -> Optional[str]:
    """Retrieve a secret value from the OS keyring or local vault fallback."""
    clean_ns = validate_namespace(namespace)
    clean_key = validate_secret_key_name(key_name)
    service_name = f"mesh:{clean_ns}"

    if not _use_isolated_local_vault():
        try:
            import keyring  # type: ignore[import-not-found]

            val = keyring.get_password(service_name, clean_key)
            if val is not None:
                return str(val)
        except Exception:
            pass

    with _VAULT_LOCK:
        vault = _read_local_vault_locked()
        return vault.get(f"{clean_ns}:{clean_key}")


class SecretResolver:
    """
    Resolves a `SecretLocator` to its runtime secret value for local CLI execution (`mesh run`).
    Never exposed to MCP tools or HTTP API responses (Finding S1).
    """

    @staticmethod
    def resolve(locator: SecretLocator | dict[str, Any] | str) -> str:
        if isinstance(locator, str):
            loc = SecretLocator.from_uri(locator)
        elif isinstance(locator, dict):
            loc = SecretLocator.from_dict(locator)
        elif isinstance(locator, SecretLocator):
            loc = locator
        else:
            raise ValueError("Invalid secret locator.")

        prov = loc.provider
        uri = loc.uri

        if prov == "env":
            var_name = uri[len("env://") :].strip()
            val = os.environ.get(var_name)
            if val is None:
                raise KeyError(f"Environment variable '{var_name}' is not set.")
            return val

        if prov == "keyring":
            # Format: keyring://mesh/<namespace>/<key_name> or keyring://<service>/<key_name>
            rest = uri[len("keyring://") :]
            parts = [p for p in rest.split("/") if p]
            if len(parts) >= 3 and parts[0] == "mesh":
                ns, key = parts[1], parts[2]
            elif len(parts) == 2:
                ns, key = parts[0], parts[1]
            else:
                ns, key = loc.namespace, loc.key_name
            val = get_secret_from_keyring(ns, key)
            if val is None:
                raise KeyError(f"Secret '{key}' not found in keyring for namespace '{ns}'.")
            return val

        if prov == "1password":
            res = subprocess.run(
                ["op", "read", uri],
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False,
            )
            if res.returncode != 0:
                raise RuntimeError(f"1Password CLI ('op read') failed with exit code {res.returncode}.")
            return res.stdout.rstrip("\r\n")

        if prov == "bitwarden":
            item_id = uri[len("bw://") :].strip()
            res = subprocess.run(
                ["bw", "get", "password", item_id],
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False,
            )
            if res.returncode != 0:
                raise RuntimeError(f"Bitwarden CLI ('bw get password') failed with exit code {res.returncode}.")
            return res.stdout.rstrip("\r\n")

        if prov == "sops":
            raw_target = uri[len("sops://") :]
            file_part, _, key_part = raw_target.partition("#")
            cmd = ["sops", "-d"]
            if key_part:
                cmd.extend(["--extract", f'["{key_part}"]'])
            cmd.append(file_part)
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False,
            )
            if res.returncode != 0:
                raise RuntimeError(f"SOPS CLI failed with exit code {res.returncode}.")
            return res.stdout.rstrip("\r\n")

        if prov == "aws_sm":
            secret_id = uri[len("awssm://") :].strip()
            res = subprocess.run(
                [
                    "aws",
                    "secretsmanager",
                    "get-secret-value",
                    "--secret-id",
                    secret_id,
                    "--query",
                    "SecretString",
                    "--output",
                    "text",
                ],
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False,
            )
            if res.returncode != 0:
                raise RuntimeError(f"AWS Secrets Manager CLI failed with exit code {res.returncode}.")
            return res.stdout.rstrip("\r\n")

        if prov == "gcp_sm":
            secret_name = uri[len("gcpsm://") :].strip()
            res = subprocess.run(
                ["gcloud", "secrets", "versions", "access", "latest", f"--secret={secret_name}"],
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False,
            )
            if res.returncode != 0:
                raise RuntimeError(f"GCP Secret Manager CLI failed with exit code {res.returncode}.")
            return res.stdout.rstrip("\r\n")

        if prov == "vault":
            vault_path = uri[len("vault://") :].strip()
            path_part, _, field_part = vault_path.partition("#")
            field_name = field_part or "value"
            res = subprocess.run(
                ["vault", "kv", "get", f"-field={field_name}", path_part],
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False,
            )
            if res.returncode != 0:
                raise RuntimeError(f"HashiCorp Vault CLI failed with exit code {res.returncode}.")
            return res.stdout.rstrip("\r\n")

        raise ValueError(f"Unsupported secret provider '{prov}'.")


# ==============================================================================
# Value-Based, Contextual & Entropy Secret Detector (Findings S2, S3, S4, N2)
# ==============================================================================


def _shannon_entropy(s: str) -> float:
    """Compute Shannon entropy in bits per character."""
    if not s:
        return 0.0
    length = len(s)
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def _is_filesystem_path(val: str) -> bool:
    v = val.strip().strip("'\"")
    if v.startswith(("/", "./", "../", "~/")):
        return True
    if len(v) >= 3 and v[1] == ":" and v[2] in ("\\", "/"):
        return True
    return False


def _is_plausible_secret_value(
    raw_value: str,
    operator: str,
    keyword: str,
    was_quoted: bool = False,
) -> bool:
    """
    Determine whether `raw_value` captured after `keyword` and `operator` is a real secret
    rather than ordinary English prose (Findings S2, S3, S4).
    """
    val = raw_value.strip()
    if not val:
        return False

    # Never flag existing redaction or secret_ref placeholders or shell variable references ($VAR / ${VAR})
    if _PLACEHOLDER_ONLY_RE.match(val) or _ENV_REF_ONLY_RE.match(val):
        return False
    if val.startswith(("[REDACTED_", "[SECRET_REF:")):
        return False

    kw_low = keyword.strip().lower()
    op_low = operator.strip().lower()

    # Finding S4: `pwd: /Users/alice/...` is a working directory path, not a password
    if kw_low == "pwd" and _is_filesystem_path(val):
        return False

    val_low = val.lower().rstrip(".,;!?")
    if not was_quoted and val_low in _BENIGN_VALUE_WORDS:
        return False

    # Quoted values (e.g. "correct horse battery staple", "$2b$12$...", "p@ss,word;123", "hunter2")
    if was_quoted:
        if len(val) < 2:
            return False
        if val_low in _BENIGN_VALUE_WORDS:
            return False
        if kw_low == "pwd" and _is_filesystem_path(val):
            return False
        return True

    # CLI flags (--password x, --token y, --api-key z)
    if op_low.startswith("-"):
        return len(val) >= 2 and val_low not in _BENIGN_VALUE_WORDS

    # Short PINs / passwords with explicit `=` or `:`
    if op_low in ("=", ":", ":="):
        if len(val) < 3:
            return False
        # Purely lowercase English word after `:` (e.g., "password: required", "token: bearer")
        if op_low == ":" and val.isalpha() and val.islower() and val_low not in _COMMON_LOWERCASE_PASSWORDS:
            if _shannon_entropy(val) < 3.5:
                return False
        return True

    # Natural-language copula (`is`, `was`, `equals`)
    if len(val) < 3:
        return False
    if val_low in _COMMON_LOWERCASE_PASSWORDS:
        return True
    has_digit = any(c.isdigit() for c in val)
    has_symbol = any(not c.isalnum() for c in val)
    has_mixed_case = any(c.isupper() for c in val) and any(c.islower() for c in val)
    if has_digit or has_symbol or has_mixed_case:
        # Exclude pure version/port numbers if keyword wasn't password/pin/pwd/secret
        return True
    if len(val) >= 14 and _shannon_entropy(val) >= 3.55:
        return True
    return False


# Tier 1: Context-free well-known credential / token / key patterns (Findings S2, S3, N2)
_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----(?:[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----|[^\r\n]*)"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_KNOWN_API_KEY_RE = re.compile(
    r"\b(?:"
    r"sk-ant-[a-zA-Z0-9_-]{8,}|"
    r"sk-proj-[a-zA-Z0-9_-]{12,}|"
    r"sk-svcacct-[a-zA-Z0-9_-]{12,}|"
    r"sk-[a-zA-Z0-9_-]{6,}|"
    r"gh[pousr]_[a-zA-Z0-9]{12,}|"
    r"github_pat_[a-zA-Z0-9_]{16,}|"
    r"glpat-[a-zA-Z0-9_-]{12,}|"
    r"hf_[a-zA-Z0-9]{16,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"ASIA[0-9A-Z]{16}|"
    r"xox[baprs]-[a-zA-Z0-9-]{10,}|"
    r"AIza[0-9A-Za-z_-]{20,}|"
    r"rk_(?:live|test)_[0-9a-zA-Z]{12,}|"
    r"sk_(?:live|test)_[0-9a-zA-Z]{12,}|"
    r"pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{20,}|"
    r"npm_[A-Za-z0-9]{20,}|"
    r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"
    r")\b"
)

# Finding S2: Authorization / Bearer headers supporting optional quotes around the token
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(authorization\s*[:=]\s*(?:basic|bearer|token)\s+)(['\"]?)([^\s'\"`;\n]{4,})\2"
)
_BEARER_TOKEN_RE = re.compile(
    r"(?i)\b(Bearer\s+)(['\"]?)(?!\[REDACTED)([A-Za-z0-9_\-./+=]{8,})\2"
)

# Finding N2: URL userinfo credentials bounded so they never cross quotes/commas or match `localhost:8080/v1`
_URL_USERINFO_RE = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.-]{0,30}://[^/\s:@\"'`,;<>]*:)"
    r"(?![0-9]{1,5}(?:[/\"'`,;\s]|$))"
    r"(?!\[REDACTED)"
    r"([^\s\"'`,;<>]{1,256})"
    r"(@[A-Za-z0-9._\[\]-]+)"
)

# Finding S3: CLI flags (`--password x`, `--token y`, `--api-key z`, `--secret w`, `--client-secret v`)
_CLI_SECRET_FLAG_RE = re.compile(
    r"(?i)(--(?:password|passwd|passphrase|token|api-key|apikey|secret|client-secret|access-token|auth-token|private-key))"
    r"(\s*=\s*|\s+)"
    r"(?:(['\"])([^\r\n]+?)\3|([^\s'\"`;\n]{3,}))"
)

# Finding S3: YAML multiline block scalar (`password: |\n  secret_line`)
_YAML_BLOCK_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|passphrase|secret|api[_\s-]?key|private[_\s-]?key|access[_\s-]?token|auth[_\s-]?token|token)"
    r"(\s*:\s*[|>][-+]?[ \t]*\r?\n)"
    r"((?:[ \t]+[^\r\n]+(?:\r?\n|$))+)"
)

# Finding S2 / S3 / S4: Keyword + optional 0..5 context words (`for checkout`) + operator (`is`/`was`/`=`/`:`) + quoted or unquoted value
_KEYWORD_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:\b|(?<=_))(password|passwd|passphrase|pwd|pin|secret|api[_\s-]?key|access[_\s-]?token|"
    r"auth[_\s-]?token|refresh[_\s-]?token|private[_\s-]?key|client[_\s-]?secret|"
    r"aws_secret_access_key|aws_session_token|token)\b"
    r"(\s+(?:(?:for|of|on|in|to|at|from|the|a|an|our|my|their|this|that)\s+|[a-z0-9_.-]+\s+){0,5}?"
    r"(?:is|was|equals)\s+|['\"]?\s*(?::=|=|:)\s*)"
    r"(?:(['\"])([^\r\n]*?)\3|([^\s,'\"`;\n\r\[]{3,}))"
)

# Unquoted multi-word passphrase (`passphrase is correct horse battery staple`)
_UNQUOTED_PASSPHRASE_RE = re.compile(
    r"(?i)\b(passphrase|recovery[_\s-]?phrase|seed[_\s-]?phrase|mnemonic)\b"
    r"(\s*(?:is|was|=|:)\s*)"
    r"(?!\[REDACTED)([A-Za-z0-9]+(?:\s+[A-Za-z0-9]+){2,11})"
)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def _context_has_benign_subject_noun(intervening_text: str) -> bool:
    """Return True if the words between the keyword and operator form a benign phrase like 'token limit is'."""
    words = re.findall(r"[a-z0-9_]+", intervening_text.lower())
    # Strip trailing operator word ('is', 'was', 'equals')
    if words and words[-1] in ("is", "was", "equals"):
        words = words[:-1]
    if not words:
        return False
    # If there is no preposition ('for', 'of', 'on', 'in', 'to', 'at') and the noun is in _BENIGN_SUBJECT_NOUNS
    prepositions = {"for", "of", "on", "in", "to", "at", "from"}
    if not (set(words) & prepositions) and any(w in _BENIGN_SUBJECT_NOUNS for w in words):
        return True
    return False


def _normalize_for_detection(text: str) -> str:
    cleaned = ZERO_WIDTH_RE.sub("", str(text or ""))
    return unicodedata.normalize("NFKC", cleaned)


def is_credential(text: str) -> bool:
    """
    Return True if `text` contains a live credential, API key, private key, JWT,
    URL password, CLI secret flag, YAML block secret, or secret assignment (Findings S2, S3, S4).
    """
    norm = _normalize_for_detection(text)
    if not norm:
        return False

    if _PEM_BLOCK_RE.search(norm):
        return True
    if _JWT_RE.search(norm):
        return True
    if _KNOWN_API_KEY_RE.search(norm):
        return True
    if _URL_USERINFO_RE.search(norm):
        return True
    if _AUTH_HEADER_RE.search(norm):
        return True
    if _BEARER_TOKEN_RE.search(norm):
        return True
    if _YAML_BLOCK_SECRET_RE.search(norm):
        return True
    if _UNQUOTED_PASSPHRASE_RE.search(norm):
        return True

    for m in _CLI_SECRET_FLAG_RE.finditer(norm):
        flag_name = m.group(1)
        was_quoted = m.group(3) is not None
        val = m.group(4) if was_quoted else (m.group(5) or "")
        if _is_plausible_secret_value(val, flag_name, flag_name.lstrip("-"), was_quoted=was_quoted):
            return True

    for m in _KEYWORD_ASSIGNMENT_RE.finditer(norm):
        kw = m.group(1)
        mid = m.group(2)
        if _context_has_benign_subject_noun(mid):
            continue
        op_match = re.search(r"(?i)(is|was|equals|:=|=|:)\s*$", mid)
        op = op_match.group(1) if op_match else "="
        was_quoted = m.group(3) is not None
        val = m.group(4) if was_quoted else (m.group(5) or "")
        if _is_plausible_secret_value(val, op, kw, was_quoted=was_quoted):
            return True

    return False


def _redact_common_secrets(
    text: str,
    replacement_fn: Optional[Callable[[str, str], str]] = None,
) -> str:
    """
    Apply all secret redaction rules consistently across `text`.
    If `replacement_fn(secret_type, raw_secret_value)` is provided (used by convert-to-secret_ref),
    replaces the secret with the returned locator placeholder instead of `[REDACTED_<TYPE>]`.
    """
    redacted = _normalize_for_detection(text)

    def _rep(secret_type: str, raw_val: str, default_placeholder: str) -> str:
        if replacement_fn is not None:
            return replacement_fn(secret_type, raw_val)
        return default_placeholder

    # 1. PEM private keys (closed or unclosed)
    def _sub_pem(m: re.Match[str]) -> str:
        full = m.group(0)
        return _rep("private_key", full, "[REDACTED_PRIVATE_KEY]")

    redacted = _PEM_BLOCK_RE.sub(_sub_pem, redacted)

    # 2. JWTs
    redacted = _JWT_RE.sub(
        lambda m: _rep("jwt", m.group(0), "[REDACTED_JWT]"),
        redacted,
    )

    # 3. Known API key prefixes
    redacted = _KNOWN_API_KEY_RE.sub(
        lambda m: _rep("api_key", m.group(0), "[REDACTED_API_KEY]"),
        redacted,
    )

    # 4. Authorization headers (Basic / Bearer / Token, quoted or unquoted)
    def _sub_auth_hdr(m: re.Match[str]) -> str:
        prefix, quote, val = m.group(1), m.group(2), m.group(3)
        if val.startswith("[REDACTED") or val.startswith("[SECRET_REF:"):
            return m.group(0)
        sub_val = _rep("token", val, "[REDACTED_TOKEN]")
        return f"{prefix}{quote}{sub_val}{quote}"

    redacted = _AUTH_HEADER_RE.sub(_sub_auth_hdr, redacted)

    # 5. Standalone Bearer tokens (quoted or unquoted)
    def _sub_bearer(m: re.Match[str]) -> str:
        prefix, quote, val = m.group(1), m.group(2), m.group(3)
        if val.startswith("[REDACTED") or val.startswith("[SECRET_REF:"):
            return m.group(0)
        sub_val = _rep("token", val, "[REDACTED_TOKEN]")
        return f"{prefix}{quote}{sub_val}{quote}"

    redacted = _BEARER_TOKEN_RE.sub(_sub_bearer, redacted)

    # 6. URL userinfo passwords (bounded, Finding N2)
    def _sub_url_pw(m: re.Match[str]) -> str:
        pre, pw, host = m.group(1), m.group(2), m.group(3)
        if pw.startswith("[REDACTED") or pw.startswith("[SECRET_REF:"):
            return m.group(0)
        sub_val = _rep("password", pw, "[REDACTED_PASSWORD]")
        return f"{pre}{sub_val}{host}"

    redacted = _URL_USERINFO_RE.sub(_sub_url_pw, redacted)

    # 7. YAML multiline block scalars (Finding S3)
    def _sub_yaml_block(m: re.Match[str]) -> str:
        kw, sep, block = m.group(1), m.group(2), m.group(3)
        raw_secret = "\n".join(line.strip() for line in block.splitlines() if line.strip())
        first_indent_match = re.match(r"([ \t]+)", block)
        indent = first_indent_match.group(1) if first_indent_match else "  "
        trailing_nl = "\n" if block.endswith("\n") else ""
        sub_val = _rep(kw.lower(), raw_secret, "[REDACTED_SECRET]")
        return f"{kw}{sep}{indent}{sub_val}{trailing_nl}"

    redacted = _YAML_BLOCK_SECRET_RE.sub(_sub_yaml_block, redacted)

    # 8. Unquoted multi-word passphrases (Finding S3)
    def _sub_passphrase(m: re.Match[str]) -> str:
        kw, sep, phrase = m.group(1), m.group(2), m.group(3)
        sub_val = _rep("passphrase", phrase, "[REDACTED_SECRET]")
        return f"{kw}{sep}{sub_val}"

    redacted = _UNQUOTED_PASSPHRASE_RE.sub(_sub_passphrase, redacted)

    # 9. CLI flags (--password, --token, --api-key, etc., Finding S3)
    def _sub_cli_flag(m: re.Match[str]) -> str:
        flag_name, sep, quote = m.group(1), m.group(2), m.group(3) or ""
        was_quoted = m.group(3) is not None
        val = m.group(4) if was_quoted else (m.group(5) or "")
        if not _is_plausible_secret_value(val, flag_name, flag_name.lstrip("-"), was_quoted=was_quoted):
            return m.group(0)
        sub_val = _rep(flag_name.lstrip("-").lower(), val, "[REDACTED_SECRET]")
        return f"{flag_name}{sep}{quote}{sub_val}{quote}"

    redacted = _CLI_SECRET_FLAG_RE.sub(_sub_cli_flag, redacted)

    # 10. Keyword assignments & natural language (`is`/`was`/`=`/`:`, Findings S2, S3, S4)
    def _sub_kw_assign(m: re.Match[str]) -> str:
        kw, mid, quote = m.group(1), m.group(2), m.group(3) or ""
        if _context_has_benign_subject_noun(mid):
            return m.group(0)
        op_match = re.search(r"(?i)(is|was|equals|:=|=|:)\s*$", mid)
        op = op_match.group(1) if op_match else "="
        was_quoted = m.group(3) is not None
        val = m.group(4) if was_quoted else (m.group(5) or "")
        if not _is_plausible_secret_value(val, op, kw, was_quoted=was_quoted):
            return m.group(0)
        # Preserve trailing sentence punctuation on unquoted natural-language tokens
        trailing_punct = ""
        clean_val = val
        if not was_quoted and op.lower() in ("is", "was", "equals") and clean_val and clean_val[-1] in ".,;!?":
            trailing_punct = clean_val[-1]
            clean_val = clean_val[:-1]
        sub_val = _rep(kw.lower(), clean_val, "[REDACTED_SECRET]")
        return f"{kw}{mid}{quote}{sub_val}{quote}{trailing_punct}"

    redacted = _KEYWORD_ASSIGNMENT_RE.sub(_sub_kw_assign, redacted)
    return redacted


def redact_secrets_local(text: str) -> str:
    """
    Redact credentials, API keys, tokens, and passwords for local storage.
    Deliberately preserves email addresses so local operational notes like
    'the admin contact alice@example.com is in the runbook' remain intact (Finding S10).
    """
    return _redact_common_secrets(text)


def redact_secrets_for_cloud(text: str) -> str:
    """
    Redact all credentials, API keys, tokens, passwords, AND email addresses
    before transmitting session log text to any external cloud LLM provider (Finding S10).
    """
    redacted = _redact_common_secrets(text)
    redacted = _EMAIL_RE.sub("[REDACTED_EMAIL]", redacted)
    return redacted


def convert_text_secrets_to_refs(
    text: str,
    namespace: str = "shared",
    key_name_hint: Optional[str] = None,
) -> tuple[str, list[SecretLocator]]:
    """
    Convert-instead-of-destroy helper (Finding S1 / 2b):
    Vaults every detected secret value in `text` into the OS keyring (or local vault fallback),
    replaces the secret in `text` with `[SECRET_REF:keyring://mesh/<ns>/<key>]`,
    and returns `(converted_text, list_of_locators)`.
    """
    clean_ns = validate_namespace(namespace)
    locators: list[SecretLocator] = []
    counter = 0

    def _vault_and_replace(secret_type: str, raw_val: str) -> str:
        nonlocal counter
        counter += 1
        if key_name_hint and counter == 1:
            base_key = re.sub(r"[^A-Za-z0-9_.-]", "_", key_name_hint).lstrip("0123456789.-") or "SECRET"
        else:
            clean_type = re.sub(r"[^A-Za-z0-9_]", "_", secret_type).upper().strip("_") or "SECRET"
            digest = hashlib.sha256(raw_val.encode("utf-8")).hexdigest()[:8]
            base_key = f"{clean_type}_{digest}"
        loc = store_secret_in_keyring(
            namespace=clean_ns,
            key_name=base_key,
            secret_value=raw_val,
            description=f"Auto-vaulted {secret_type} from namespace '{clean_ns}'",
        )
        locators.append(loc)
        return loc.format_placeholder()

    converted = _redact_common_secrets(text, replacement_fn=_vault_and_replace)
    return converted, locators


def run_with_secrets(
    command: list[str],
    namespace: str = "shared",
    env_specs: Optional[list[str]] = None,
) -> int:
    """
    Resolve `secret_ref` memories in `namespace` (or explicit `ENV_VAR=URI` / `KEY_NAME` specs)
    and execute `command` with the resolved secrets injected into the child environment (Finding S1 / 2a).
    Never logs or prints any secret value.
    """
    if not command:
        raise ValueError("No command specified for `mesh run`.")
    clean_ns = validate_namespace(namespace)
    child_env = os.environ.copy()

    # 1. Load secret_ref memories from the namespace if it exists on disk
    from .store import MeshStore, NamespaceNotFoundError

    stored_locators: dict[str, SecretLocator] = {}
    try:
        store = MeshStore(namespace=clean_ns, create=False)
        for row in store.list_all(limit=500, memory_type="secret_ref", include_private=True):
            raw_ref = row.get("secret_ref")
            if raw_ref:
                try:
                    ref_dict = json.loads(raw_ref) if isinstance(raw_ref, str) else raw_ref
                    loc = SecretLocator.from_dict(ref_dict, default_namespace=clean_ns)
                    stored_locators[loc.key_name] = loc
                except Exception:
                    continue
    except NamespaceNotFoundError:
        pass

    # 2. Resolve requested env_specs (or all secret_ref memories in namespace if env_specs omitted)
    if env_specs:
        for spec in env_specs:
            clean_spec = spec.strip()
            if not clean_spec:
                continue
            if "=" in clean_spec:
                env_var, target = clean_spec.split("=", 1)
                env_var = validate_secret_key_name(env_var)
                target = target.strip()
                if any(target.startswith(s) for s in _PROVIDER_SCHEME_MAP.values()):
                    loc = SecretLocator.from_uri(target, key_name=env_var, namespace=clean_ns)
                elif target in stored_locators:
                    loc = stored_locators[target]
                else:
                    loc = SecretLocator(
                        provider="keyring",
                        uri=f"keyring://mesh/{clean_ns}/{target}",
                        key_name=env_var,
                        namespace=clean_ns,
                    )
                child_env[env_var] = SecretResolver.resolve(loc)
            else:
                key_name = validate_secret_key_name(clean_spec)
                loc = stored_locators.get(key_name) or SecretLocator(
                    provider="keyring",
                    uri=f"keyring://mesh/{clean_ns}/{key_name}",
                    key_name=key_name,
                    namespace=clean_ns,
                )
                child_env[key_name] = SecretResolver.resolve(loc)
    else:
        for key_name, loc in stored_locators.items():
            child_env[key_name] = SecretResolver.resolve(loc)

    proc = subprocess.run(command, env=child_env, shell=False, check=False)
    return int(proc.returncode)


# Legacy underscore aliases for internal/test backwards compatibility
_is_credential = is_credential
_redact_secrets_for_cloud = redact_secrets_for_cloud
_redact_secrets_local = redact_secrets_local
