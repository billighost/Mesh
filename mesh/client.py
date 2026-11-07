"""Small, policy-preserving SDK client and reviewed local connection profiles.

Importing this module does not load the model, MCP, FastAPI, or a database. Tokens
are read from a protected file only when an existing HTTP endpoint is requested.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .audit import mesh_dir_context, reject_symlink, validate_namespace
from .operation_contract import UnknownOutcome, supports_operation_receipts, validate_mutation_acknowledgement

_MAX_PROFILE_BYTES = 16384
_MAX_ARGUMENT_BYTES = 262144
_POLICY_FIELDS = {"include_private", "read_only", "enforce_agent_ownership", "allow_namespace_override"}
_PROFILE_FIELDS = {"version", "namespace", "mesh_home", "agent_id", "policy", "endpoint", "token_file", "producer_scope"}
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HTTP_CONNECTION_MAX_CALLS = 512
_HTTP_CONNECTION_MAX_SECONDS = 60.0


class ProfileError(ValueError):
    pass


class TransportUnavailable(RuntimeError):
    pass


def _pairs_unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProfileError("Duplicate JSON fields are not permitted.")
        result[key] = value
    return result


def _checked_path(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    path = Path(os.path.abspath(path))
    for candidate in (path, *path.parents):
        reject_symlink(candidate)
    return path


def store_identity(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConnectionPolicy:
    include_private: bool = False
    read_only: bool = False
    enforce_agent_ownership: bool = False
    allow_namespace_override: bool = False


@dataclass(frozen=True)
class ConnectionProfile:
    namespace: str
    mesh_home: Path
    agent_id: str = "mesh-client"
    policy: ConnectionPolicy = field(default_factory=ConnectionPolicy)
    endpoint: str | None = None
    token_file: Path | None = None
    version: int = 1
    producer_scope: str = "local-user"

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise ProfileError("Unsupported Mesh connection profile version.")
        object.__setattr__(self, "namespace", validate_namespace(self.namespace))
        if (
            not isinstance(self.agent_id, str)
            or not self.agent_id
            or len(self.agent_id) > 64
            or re.search(r"[\x00-\x1f\x7f]", self.agent_id)
        ):
            raise ProfileError("Agent label must contain 1–64 printable characters.")
        if not isinstance(self.policy, ConnectionPolicy) or any(
            type(getattr(self.policy, key)) is not bool for key in _POLICY_FIELDS
        ):
            raise ProfileError("Profile policy values must be booleans.")
        from .operation_contract import validate_producer_scope

        try:
            validate_producer_scope(self.producer_scope)
        except ValueError as exc:
            raise ProfileError(str(exc)) from exc
        if self.producer_scope.startswith("principal:") and self.producer_scope != "principal:" + self.agent_id:
            raise ProfileError("An authenticated profile must use its reviewed principal as agent_id.")
        object.__setattr__(self, "mesh_home", _checked_path(self.mesh_home, Path.cwd()))
        if self.endpoint is not None:
            if not isinstance(self.endpoint, str):
                raise ProfileError("Endpoint must be a literal loopback URL.")
            try:
                parsed = urlsplit(self.endpoint)
                local = ipaddress.ip_address(parsed.hostname or "").is_loopback
                port = parsed.port
            except ValueError:
                raise ProfileError("Endpoint must be a literal loopback URL.") from None
            if (
                not local
                or parsed.scheme not in ("http", "https")
                or parsed.username
                or parsed.password
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
                or port == 0
            ):
                raise ProfileError("Endpoint must be a literal loopback HTTP(S) origin.")
        if self.token_file is not None:
            object.__setattr__(self, "token_file", _checked_path(self.token_file, self.mesh_home))
        if self.endpoint is not None and self.token_file is None:
            raise ProfileError("HTTP profiles require a token_file pointer; tokens cannot be inline.")

    @classmethod
    def from_file(cls, path: str | Path) -> ConnectionProfile:
        profile_path = _checked_path(path, Path.cwd())
        if profile_path.stat().st_size > _MAX_PROFILE_BYTES:
            raise ProfileError("Connection profile is too large.")
        try:
            data = json.loads(profile_path.read_text(encoding="utf-8"), object_pairs_hook=_pairs_unique)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ProfileError("Connection profile must contain valid UTF-8 JSON.") from exc
        return cls._from_document(data, profile_path)

    @classmethod
    def _from_document(cls, data: Any, profile_path: Path) -> ConnectionProfile:
        """Share strict validation with installation planning without rereading a file."""
        if not isinstance(data, dict) or set(data) - _PROFILE_FIELDS:
            raise ProfileError("Connection profile contains unsupported fields.")
        if not {"version", "namespace", "mesh_home", "agent_id", "policy"} <= set(data):
            raise ProfileError("Connection profile is missing a required binding or policy.")
        policy = data["policy"]
        if (
            not isinstance(policy, dict)
            or set(policy) - _POLICY_FIELDS
            or any(type(value) is not bool for value in policy.values())
        ):
            raise ProfileError("Connection policy contains unsupported fields or non-boolean values.")
        if not isinstance(data["mesh_home"], str) or not data["mesh_home"].strip():
            raise ProfileError("mesh_home must be a nonempty path.")
        token = data.get("token_file")
        if token is not None and (not isinstance(token, str) or not token.strip()):
            raise ProfileError("token_file must be a path pointer.")
        return cls(
            version=data["version"],
            namespace=data["namespace"],
            agent_id=data["agent_id"],
            mesh_home=_checked_path(data["mesh_home"], profile_path.parent),
            policy=ConnectionPolicy(**policy),
            producer_scope=data.get("producer_scope", "local-user"),
            endpoint=data.get("endpoint"),
            token_file=_checked_path(token, profile_path.parent) if token is not None else None,
        )


class MeshClient:
    def __init__(self, profile: ConnectionProfile, *, transport: str = "auto", timeout: float = 10.0):
        if not isinstance(profile, ConnectionProfile):
            raise TypeError("A reviewed ConnectionProfile is required.")
        if transport not in ("auto", "sdk", "http"):
            raise ValueError("Transport must be auto, sdk, or http.")
        if not 0 < timeout <= 120:
            raise ValueError("Timeout must be between 0 and 120 seconds.")
        self.profile = profile
        self.transport = transport
        self.timeout = timeout
        self._http_lock = threading.Lock()
        self._http_connection = None
        self._http_binding = None
        self._http_created = 0.0
        self._http_calls = 0
        self._closed = False

    def _discard_http_connection(self) -> None:
        connection = getattr(self, "_http_connection", None)
        self._http_connection = None
        self._http_binding = None
        self._http_created = 0.0
        self._http_calls = 0
        if connection is not None:
            try:
                connection.close()
            except Exception:
                # Cleanup cannot replace a confirmed acknowledgement or its ID.
                pass

    def close(self) -> None:
        with self._http_lock:
            self._closed = True
            self._discard_http_connection()

    def __enter__(self) -> MeshClient:
        if self._closed:
            raise RuntimeError("The Mesh client is closed.")
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self._discard_http_connection()

    def _get_http_connection(self):
        binding = (self.profile, self.timeout)
        now = time.monotonic()
        connection = self._http_connection
        if connection is not None and (
            binding != self._http_binding
            or self._http_calls >= _HTTP_CONNECTION_MAX_CALLS
            or now - self._http_created >= _HTTP_CONNECTION_MAX_SECONDS
            or getattr(connection, "sock", connection) is None
        ):
            self._discard_http_connection()
            connection = None
        reused = connection is not None
        if connection is None:
            parsed = urlsplit(self.profile.endpoint)
            kind = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
            connection = kind(parsed.hostname, parsed.port, timeout=self.timeout)
            self._http_connection = connection
            self._http_binding = binding
            self._http_created = now
        return connection, reused

    def call(
        self, tool_name: str, arguments: dict[str, Any] | None = None, operation_id: str | None = None
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("The Mesh client is closed.")
        if not isinstance(tool_name, str) or not tool_name.startswith("mesh_") or len(tool_name) > 64:
            raise ValueError("Expected a Mesh tool name.")
        if arguments is not None and not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object.")
        args = dict(arguments or {})
        if "operation_id" in args and tool_name != "mesh_operation_get":
            if operation_id is not None and operation_id != args["operation_id"]:
                raise ValueError("Conflicting operation IDs.")
            operation_id = args.pop("operation_id")
        if operation_id is not None and (
            not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id)
        ):
            raise ValueError("Invalid operation ID.")
        namespace = validate_namespace(args.get("namespace", self.profile.namespace))
        if namespace != self.profile.namespace and not self.profile.policy.allow_namespace_override:
            raise ProfileError("Namespace override is blocked by this connection profile.")
        args["namespace"] = namespace
        private = args.get("include_private", self.profile.policy.include_private)
        if type(private) is not bool:
            raise ValueError("include_private must be a boolean.")
        args["include_private"] = private and self.profile.policy.include_private
        from .operation_contract import MUTATING_TOOLS

        mutating = tool_name in MUTATING_TOOLS
        if mutating and self.profile.policy.read_only:
            raise ProfileError("This connection profile is read-only.")
        if mutating and operation_id is None:
            operation_id = secrets.token_hex(16)
        try:
            body = json.dumps(args, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("Tool arguments must be finite JSON values.") from exc
        if len(body) > _MAX_ARGUMENT_BYTES:
            raise ValueError("Tool arguments exceed the request size limit.")
        _checked_path(self.profile.mesh_home, Path.cwd())
        try:
            if self.transport != "http":
                try:
                    return self._call_sdk(tool_name, args, operation_id)
                except TransportUnavailable:
                    if self.transport == "sdk":
                        raise
            return self._call_http(tool_name, args, operation_id, mutating)
        except Exception as exc:
            if mutating:
                exc.operation_id = operation_id
            raise

    def _call_sdk(self, name: str, arguments: dict[str, Any], operation_id: str | None) -> dict[str, Any]:
        from .execution import CapacityError
        from .operation_contract import MUTATING_TOOLS

        policy = self.profile.policy
        mutating = name in MUTATING_TOOLS
        with mesh_dir_context(self.profile.mesh_home):
            try:
                from .operations import execute_tool
            except ImportError as exc:
                raise TransportUnavailable("The local Mesh SDK dependency is unavailable.") from exc
            try:
                result = execute_tool(
                    name,
                    arguments,
                    default_namespace=self.profile.namespace,
                    agent_id=self.profile.agent_id,
                    operation_id=operation_id,
                    context={
                        "default_include_private": policy.include_private,
                        "private_locked_off": not policy.include_private,
                        "enforce_agent_ownership": policy.enforce_agent_ownership,
                        "read_only": policy.read_only,
                        "approved_producer_scope": self.profile.producer_scope,
                    },
                )
            except (ValueError, CapacityError) as exc:
                if mutating:
                    exc.operation_id = operation_id
                raise
            except Exception as exc:
                if mutating:
                    raise UnknownOutcome(operation_id) from exc
                if isinstance(exc, TransportUnavailable):
                    raise RuntimeError("The local SDK failed after dispatch.") from exc
                raise
            if mutating:
                return validate_mutation_acknowledgement(
                    result, operation_id, arguments["namespace"], operation=name,
                    producer_scope=self.profile.producer_scope,
                )
            return result

    def _call_http(
        self, name: str, arguments: dict[str, Any], operation_id: str | None, mutating: bool
    ) -> dict[str, Any]:
        with self._http_lock:
            if self._closed:
                raise RuntimeError("The Mesh client is closed.")
            try:
                return self._call_http_locked(name, arguments, operation_id, mutating)
            except BaseException:
                self._discard_http_connection()
                raise

    def _call_http_locked(
        self, name: str, arguments: dict[str, Any], operation_id: str | None, mutating: bool
    ) -> dict[str, Any]:
        if self.profile.endpoint is None or self.profile.token_file is None:
            raise TransportUnavailable("No existing authenticated HTTP endpoint is configured in the profile.")
        token_path = _checked_path(self.profile.token_file, self.profile.mesh_home)
        if token_path.stat().st_size > 1024:
            raise ProfileError("Invalid token pointer target.")
        token = token_path.read_text(encoding="utf-8").strip()
        if not token or len(token) > 512 or re.search(r"[\x00-\x20\x7f]", token):
            raise ProfileError("Invalid token pointer target.")
        conn, reused = self._get_http_connection()
        possibly_sent = False
        try:
            identity_deadline = time.monotonic() + self.timeout
            for attempt in range(2):
                try:
                    remaining = identity_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("HTTP identity verification timed out.")
                    conn.timeout = remaining
                    if getattr(conn, "sock", None) is not None:
                        conn.sock.settimeout(remaining)
                    nonce = secrets.token_hex(24)
                    conn.request("GET", "/identity?nonce=" + nonce)
                    response = conn.getresponse()
                    raw = response.read(16385)
                    break
                except (OSError, http.client.HTTPException):
                    # A stale keep-alive may reconnect only before any POST.
                    if not reused or attempt or time.monotonic() >= identity_deadline:
                        raise
                    self._discard_http_connection()
                    conn, reused = self._get_http_connection()
            if getattr(response, "will_close", False) or getattr(conn, "sock", conn) is None:
                # HTTPConnection would otherwise reopen before sending the bearer.
                raise TransportUnavailable("HTTP endpoint closed the identity connection.")
            if response.status != 200 or len(raw) > 16384:
                raise ProfileError("HTTP endpoint identity verification failed.")
            identity = json.loads(raw)
            expected_store = store_identity(self.profile.mesh_home)
            signature = hmac.new(token.encode(), (nonce + "|" + expected_store).encode(), hashlib.sha256).hexdigest()
            if (
                not isinstance(identity, dict)
                or identity.get("store_id") != expected_store
                or not hmac.compare_digest(str(identity.get("scope_hmac", "")), signature)
            ):
                raise ProfileError("HTTP endpoint did not prove the configured store identity.")
            if mutating and not supports_operation_receipts(identity, token, nonce, expected_store):
                raise ProfileError("HTTP endpoint did not prove support for durable operation receipts.")
            policy = self.profile.policy
            payload = {
                "name": name,
                "arguments": arguments,
                "default_namespace": self.profile.namespace,
                "agent_id": self.profile.agent_id,
                "operation_id": operation_id,
                "expected_producer_scope": self.profile.producer_scope,
                "expected_store_id": expected_store,
                "client_policy": {
                    "include_private": policy.include_private,
                    "read_only": policy.read_only,
                    "enforce_agent_ownership": policy.enforce_agent_ownership,
                },
            }
            conn.timeout = self.timeout
            if getattr(conn, "sock", None) is not None:
                conn.sock.settimeout(self.timeout)
            possibly_sent = True
            conn.request(
                "POST",
                "/mcp",
                body=json.dumps(payload, allow_nan=False).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
            )
            response = conn.getresponse()
            raw = response.read(2097153)
            if len(raw) > 2097152:
                if mutating:
                    raise UnknownOutcome(operation_id)
                raise RuntimeError("HTTP response exceeded the size limit.")
            if response.status in (401, 403):
                raise ProfileError("HTTP authentication or policy rejected this operation.")
            data = json.loads(raw)
            if response.status != 200:
                if (
                    mutating
                    and response.status >= 500
                    and not (isinstance(data, dict) and data.get("not_admitted") is True)
                ):
                    raise UnknownOutcome(operation_id)
                raise RuntimeError("HTTP endpoint rejected the operation (HTTP " + str(response.status) + ").")
            result = data.get("payload") if isinstance(data, dict) else None
            if not isinstance(result, dict):
                if mutating:
                    raise UnknownOutcome(operation_id)
                raise RuntimeError("HTTP endpoint returned an invalid tool result.")
            if mutating:
                result = validate_mutation_acknowledgement(
                    result, operation_id, arguments["namespace"], operation=name,
                    producer_scope=self.profile.producer_scope,
                )
            self._http_calls += 1
            if getattr(response, "will_close", False) or getattr(conn, "sock", conn) is None:
                self._discard_http_connection()
            return result
        except (OSError, socket.timeout, http.client.HTTPException, json.JSONDecodeError, UnicodeError) as exc:
            if possibly_sent and mutating:
                raise UnknownOutcome(operation_id) from exc
            raise TransportUnavailable("The configured HTTP endpoint is unavailable.") from exc
