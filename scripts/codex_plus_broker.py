#!/usr/bin/env python3
"""Lease official ChatGPT/Codex access tokens without exposing refresh tokens.

The broker owns one or more Codex ``auth.json`` files.  Clients authenticate
with a CWS device token and receive only the short-lived access token needed by
Codex's built-in OpenAI provider.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import tempfile
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request as FastAPIRequest, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

try:
    from scripts.codex_model_health import (
        ModelHealthStore,
        account_probe_available,
        classify_probe_failure,
        filter_codex_text_models,
        merge_model_probe,
    )
except ModuleNotFoundError as exc:
    if exc.name != "scripts":
        raise
    from codex_model_health import (  # type: ignore[no-redef]
        ModelHealthStore,
        account_probe_available,
        classify_probe_failure,
        filter_codex_text_models,
        merge_model_probe,
    )

try:
    from scripts.codex_release_manifest import (
        ASSET_PLATFORMS,
        DOWNLOAD_PREFIX,
        RELEASE_VERSION,
        sha256_file,
        valid_asset_name,
    )
except ModuleNotFoundError as exc:
    if exc.name != "scripts":
        raise
    from codex_release_manifest import (  # type: ignore[no-redef]
        ASSET_PLATFORMS,
        DOWNLOAD_PREFIX,
        RELEASE_VERSION,
        sha256_file,
        valid_asset_name,
    )


OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CHATGPT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DEFAULT_CODEX_MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
DEFAULT_CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
MODEL_PROBE_INPUT = "Reply with OK."
DEFAULT_PRIMARY_AUTH = "/var/lib/cws-codex/auth.json"
DEFAULT_ACCOUNTS_DIR = "/var/lib/cws-codex/accounts"
DEFAULT_STATE_FILE = "/var/lib/cws-codex/broker-state.json"
DEFAULT_USAGE_STATE_FILE = "/var/lib/cws-codex/device-usage.json"
DEFAULT_RELEASE_DIR = "/var/lib/cws-codex/releases"
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
USER_INPUT_FIELDS = (
    "user_message_count",
    "user_text_characters",
    "user_text_tokens_estimated",
)
USAGE_FIELDS = TOKEN_FIELDS + USER_INPUT_FIELDS
USAGE_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
BROKER_VERSION = "0.1.6"
USERNAME_PATTERN = re.compile(r"^[A-Z]{1,16}\.[A-Z]{1,32}$")
EMPLOYEE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PASSWORD_HASH_ITERATIONS = 310_000
TOKEN_QUERY_WINDOW_SECONDS = 900
TOKEN_QUERY_MAX_FAILURES = 8


def _validated_env_int(name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        expected = f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
        raise ValueError(f"{name} must be {expected}")
    return value


def _validated_endpoint(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    parsed = urlsplit(value)
    is_https = parsed.scheme == "https" and bool(parsed.netloc)
    is_loopback_http = (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and bool(parsed.netloc)
    )
    if not (is_https or is_loopback_http):
        raise ValueError(f"{name} must use HTTPS (loopback http://127.0.0.1 is allowed for tests)")
    return value


def normalize_username(value: str) -> str:
    username = value.strip().upper()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError("username must use the uppercase initials.surname format, for example YX.GUO")
    return username


def normalize_employee_id(value: str) -> str:
    employee_id = value.strip()
    if not EMPLOYEE_ID_PATTERN.fullmatch(employee_id):
        raise ValueError("employee id must be 1-64 letters, numbers, dots, underscores, or hyphens")
    return employee_id


def hash_user_password(password: str, *, salt: bytes | None = None) -> str:
    actual_salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), actual_salt, PASSWORD_HASH_ITERATIONS
    )
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_HASH_ITERATIONS,
        base64.urlsafe_b64encode(actual_salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
    )


def verify_user_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_text, expected_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(expected_text + "=" * (-len(expected_text) % 4))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def derive_user_device_token(secret: str, username: str, employee_id: str) -> str:
    if len(secret) < 32:
        raise ValueError("CWS_CODEX_USER_TOKEN_SECRET must contain at least 32 characters")
    message = f"cws-user-token-v1\0{username}\0{employee_id}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    return "cwsdt_" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def public_portal_user(device: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": device.get("id"),
        "username": device.get("username"),
        "employee_id": device.get("employee_id"),
        "label": device.get("label"),
        "enabled": bool(device.get("enabled", False)),
        "created_at": device.get("created_at"),
        "revoked_at": device.get("revoked_at"),
    }


def create_portal_user(
    registry: dict[str, Any], username: str, employee_id: str, token_secret: str
) -> tuple[dict[str, Any], str]:
    normalized_username = normalize_username(username)
    normalized_employee_id = normalize_employee_id(employee_id)
    devices = registry.setdefault("devices", [])
    if not isinstance(devices, list):
        raise ValueError("device token registry is invalid")
    for device in devices:
        if not isinstance(device, dict):
            continue
        if str(device.get("username") or "").upper() == normalized_username:
            raise ValueError("username already exists")
        if device.get("employee_id") == normalized_employee_id:
            raise ValueError("employee id already exists")
    token = derive_user_device_token(
        token_secret, normalized_username, normalized_employee_id
    )
    device = {
        "id": str(uuid.uuid4()),
        "label": f"{normalized_username}-{normalized_employee_id}",
        "employee_name": normalized_username,
        "employee_id": normalized_employee_id,
        "username": normalized_username,
        "password_hash": hash_user_password(normalized_employee_id),
        "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "enabled": True,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "portal_user": True,
    }
    devices.append(device)
    registry["version"] = max(2, int(registry.get("version", 1)))
    return device, token


def find_portal_user(registry: dict[str, Any], username: str) -> dict[str, Any] | None:
    try:
        normalized = normalize_username(username)
    except ValueError:
        # Keep already-created legacy usernames queryable while enforcing the
        # new initials.surname format for all newly created users.
        normalized = username.strip().upper()
    devices = registry.get("devices") if isinstance(registry, dict) else None
    if not isinstance(devices, list):
        return None
    for device in reversed(devices):
        if (
            isinstance(device, dict)
            and device.get("portal_user") is True
            and str(device.get("username") or "").upper() == normalized
        ):
            return device
    return None


def _b64url_json(value: str) -> dict[str, Any]:
    padded = value + "=" * (-len(value) % 4)
    decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    result = json.loads(decoded)
    if not isinstance(result, dict):
        raise ValueError("JWT payload is not an object")
    return result


def decode_jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise ValueError("invalid JWT format")
    return _b64url_json(parts[1])


def token_expiry(token: str) -> int:
    exp = decode_jwt_claims(token).get("exp")
    if not isinstance(exp, (int, float)):
        raise ValueError("access token has no numeric exp claim")
    return int(exp)


def _nested(claims: dict[str, Any], key: str) -> dict[str, Any]:
    value = claims.get(key)
    return value if isinstance(value, dict) else {}


def token_plan_type(token: str) -> str | None:
    claims = decode_jwt_claims(token)
    auth = _nested(claims, "https://api.openai.com/auth")
    value = auth.get("chatgpt_plan_type")
    return value if isinstance(value, str) and value else None


def token_account_id(token: str) -> str | None:
    claims = decode_jwt_claims(token)
    auth = _nested(claims, "https://api.openai.com/auth")
    value = auth.get("chatgpt_account_id")
    return value if isinstance(value, str) and value else None


def token_email(*tokens: str | None) -> str | None:
    """Read the login email from server-owned ID/access token claims for display."""
    for token in tokens:
        if not isinstance(token, str) or not token:
            continue
        try:
            claims = decode_jwt_claims(token)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        candidates = (
            claims.get("email"),
            claims.get("preferred_username"),
            _nested(claims, "https://api.openai.com/profile").get("email"),
        )
        for candidate in candidates:
            if isinstance(candidate, str) and "@" in candidate:
                return candidate.strip()
    return None


def atomic_write_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        else:
            os.chmod(temp_name, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, mode)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


@dataclass(frozen=True)
class BrokerSettings:
    primary_auth: Path
    accounts_dir: Path
    state_file: Path
    usage_state_file: Path
    device_token_file: Path
    release_dir: Path
    device_tokens: tuple[str, ...]
    admin_token: str | None
    user_token_secret: str | None
    lease_seconds: int
    lease_rotation_seconds: int
    refresh_window_seconds: int
    usage_cache_seconds: int
    model_probe_interval_seconds: int
    model_cooldown_seconds: int
    model_probe_concurrency: int
    model_probe_timeout_seconds: int
    model_health_file: Path
    model_fallbacks: tuple[str, ...]
    codex_models_url: str
    codex_responses_url: str

    @classmethod
    def from_env(cls) -> "BrokerSettings":
        raw_tokens = os.getenv("CWS_CODEX_DEVICE_TOKENS", "")
        device_tokens = tuple(token.strip() for token in raw_tokens.split(",") if token.strip())
        raw_fallbacks = os.getenv("CWS_CODEX_MODEL_FALLBACKS", "")
        model_fallbacks = tuple(
            dict.fromkeys(model.strip() for model in raw_fallbacks.split(",") if model.strip())
        )
        return cls(
            primary_auth=Path(os.getenv("CWS_CODEX_PRIMARY_AUTH", DEFAULT_PRIMARY_AUTH)),
            accounts_dir=Path(os.getenv("CWS_CODEX_ACCOUNTS_DIR", DEFAULT_ACCOUNTS_DIR)),
            state_file=Path(os.getenv("CWS_CODEX_STATE_FILE", DEFAULT_STATE_FILE)),
            usage_state_file=Path(
                os.getenv("CWS_CODEX_USAGE_STATE_FILE", DEFAULT_USAGE_STATE_FILE)
            ),
            device_token_file=Path(
                os.getenv("CWS_CODEX_DEVICE_TOKEN_FILE", "/var/lib/cws-codex/device-tokens.json")
            ),
            release_dir=Path(os.getenv("CWS_CODEX_RELEASE_DIR", DEFAULT_RELEASE_DIR)),
            device_tokens=device_tokens,
            admin_token=os.getenv("CWS_CODEX_ADMIN_TOKEN") or None,
            user_token_secret=(
                os.getenv("CWS_CODEX_USER_TOKEN_SECRET")
                or os.getenv("CWS_CODEX_ADMIN_TOKEN")
                or None
            ),
            lease_seconds=int(os.getenv("CWS_CODEX_LEASE_SECONDS", "28800")),
            lease_rotation_seconds=max(
                3600, int(os.getenv("CWS_CODEX_LEASE_ROTATION_SECONDS", "86400"))
            ),
            refresh_window_seconds=int(os.getenv("CWS_CODEX_REFRESH_WINDOW_SECONDS", "900")),
            usage_cache_seconds=int(os.getenv("CWS_CODEX_USAGE_CACHE_SECONDS", "30")),
            model_probe_interval_seconds=_validated_env_int(
                "CWS_CODEX_MODEL_PROBE_INTERVAL_SECONDS", 900, 60
            ),
            model_cooldown_seconds=_validated_env_int(
                "CWS_CODEX_MODEL_COOLDOWN_SECONDS", 1800, 60
            ),
            model_probe_concurrency=_validated_env_int(
                "CWS_CODEX_MODEL_PROBE_CONCURRENCY", 2, 1, 8
            ),
            model_probe_timeout_seconds=_validated_env_int(
                "CWS_CODEX_MODEL_PROBE_TIMEOUT_SECONDS", 45, 5, 120
            ),
            model_health_file=Path(
                os.getenv("CWS_CODEX_MODEL_HEALTH_FILE", "/var/lib/cws-codex/model-health.json")
            ),
            model_fallbacks=model_fallbacks,
            codex_models_url=_validated_endpoint(
                "CWS_CODEX_MODELS_URL", DEFAULT_CODEX_MODELS_URL
            ),
            codex_responses_url=_validated_endpoint(
                "CWS_CODEX_RESPONSES_URL", DEFAULT_CODEX_RESPONSES_URL
            ),
        )


class LeaseRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=200)
    lease_id: str | None = Field(default=None, max_length=200)
    client_ip: str | None = Field(default=None, max_length=45)


class PortalUserCreateRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    employee_id: str = Field(min_length=1, max_length=64)


class PortalTokenQueryRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class DailySessionUsage(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    input_tokens: int = Field(default=0, ge=0, le=10**15)
    cached_input_tokens: int = Field(default=0, ge=0, le=10**15)
    cache_write_input_tokens: int = Field(default=0, ge=0, le=10**15)
    output_tokens: int = Field(default=0, ge=0, le=10**15)
    reasoning_output_tokens: int = Field(default=0, ge=0, le=10**15)
    total_tokens: int = Field(default=0, ge=0, le=10**15)
    user_message_count: int = Field(default=0, ge=0, le=10**12)
    user_text_characters: int = Field(default=0, ge=0, le=10**15)
    user_text_tokens_estimated: int = Field(default=0, ge=0, le=10**15)


class SessionTokenUsage(DailySessionUsage):
    session_id: str = Field(min_length=1, max_length=200)
    date: str = Field(default="1970-01-01", exclude=True)
    daily_usage: list[DailySessionUsage] = Field(default_factory=list, max_length=4000)


class UsageReport(BaseModel):
    sessions: list[SessionTokenUsage]
    client_ip: str | None = Field(default=None, max_length=45)


@dataclass(frozen=True)
class DeviceIdentity:
    token_id: str
    label: str
    legacy: bool = False
    employee_name: str | None = None
    employee_id: str | None = None


class DeviceUsageStore:
    """Persist absolute per-session counters keyed by trusted device-token identity."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"version": 2, "devices": {}}
        if not isinstance(payload, dict) or not isinstance(payload.get("devices"), dict):
            return {"version": 2, "devices": {}}
        return payload

    @staticmethod
    def _upsert_device(
        state: dict[str, Any], identity: DeviceIdentity, now: float
    ) -> dict[str, Any]:
        devices = state.setdefault("devices", {})
        device = devices.setdefault(
            identity.token_id,
            {
                "device_token_id": identity.token_id,
                "device_label": identity.label,
                "first_seen_at": now,
                "last_seen_at": now,
                "lease_count": 0,
                "report_count": 0,
                "sessions": {},
            },
        )
        device["device_label"] = identity.label
        device["last_seen_at"] = now
        if identity.employee_name:
            device["employee_name"] = identity.employee_name
        if identity.employee_id:
            device["employee_id"] = identity.employee_id
        return device

    @staticmethod
    def _record_ip(device: dict[str, Any], kind: str, value: str | None, now: float) -> None:
        if not value:
            return
        device[f"last_{kind}"] = value
        history_key = f"{kind}_history"
        history = device.setdefault(history_key, [])
        if not isinstance(history, list):
            history = []
            device[history_key] = history
        entry = next(
            (item for item in history if isinstance(item, dict) and item.get("ip") == value),
            None,
        )
        if entry is None:
            entry = {"ip": value, "first_seen_at": now, "last_seen_at": now, "count": 0}
            history.append(entry)
        entry["last_seen_at"] = now
        entry["count"] = int(entry.get("count", 0)) + 1
        history.sort(key=lambda item: float(item.get("last_seen_at", 0)), reverse=True)
        del history[20:]

    async def record_lease(
        self,
        identity: DeviceIdentity,
        client_ip: str | None,
        source_ip: str | None,
    ) -> None:
        now = time.time()
        async with self.lock:
            state = self._load()
            state["version"] = 2
            device = self._upsert_device(state, identity, now)
            device["last_lease_at"] = now
            device["lease_count"] = int(device.get("lease_count", 0)) + 1
            self._record_ip(device, "client_ip", client_ip, now)
            self._record_ip(device, "source_ip", source_ip, now)
            atomic_write_json(self.path, state)

    async def record(
        self,
        identity: DeviceIdentity,
        sessions: list[SessionTokenUsage],
        client_ip: str | None = None,
        source_ip: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        async with self.lock:
            state = self._load()
            state["version"] = 2
            device = self._upsert_device(state, identity, now)
            device.setdefault("first_reported_at", now)
            device["last_reported_at"] = now
            device["report_count"] = int(device.get("report_count", 0)) + 1
            self._record_ip(device, "client_ip", client_ip, now)
            self._record_ip(device, "source_ip", source_ip, now)
            stored_sessions = device.setdefault("sessions", {})
            for item in sessions:
                incoming = item.model_dump() if hasattr(item, "model_dump") else item.dict()
                session_id = incoming.pop("session_id")
                incoming_days = incoming.pop("daily_usage", [])
                incoming.pop("date", None)
                current = stored_sessions.setdefault(session_id, {})
                for field in USAGE_FIELDS:
                    current[field] = max(int(current.get(field, 0)), int(incoming.get(field, 0)))
                stored_days = current.setdefault("daily_usage", {})
                if not isinstance(stored_days, dict):
                    stored_days = {}
                    current["daily_usage"] = stored_days
                for incoming_day in incoming_days:
                    if isinstance(incoming_day, dict):
                        day = dict(incoming_day)
                    elif hasattr(incoming_day, "model_dump"):
                        day = incoming_day.model_dump()
                    else:
                        day = incoming_day.dict()
                    date_key = str(day.pop("date"))
                    stored_day = stored_days.setdefault(date_key, {})
                    for field in USAGE_FIELDS:
                        stored_day[field] = max(
                            int(stored_day.get(field, 0)), int(day.get(field, 0))
                        )
                current["updated_at"] = now
            atomic_write_json(self.path, state)
            return {
                "accepted_sessions": len(sessions),
                "device_token_id": identity.token_id,
                "reported_at": now,
            }

    async def summary(
        self, registered_devices: dict[str, dict[str, Any]] | set[str]
    ) -> list[dict[str, Any]]:
        async with self.lock:
            state = self._load()
        if isinstance(registered_devices, set):
            registry = {token_id: {"enabled": True} for token_id in registered_devices}
        else:
            registry = registered_devices
        results: list[dict[str, Any]] = []
        today = datetime.now(USAGE_TIMEZONE).date()
        week_start = today - timedelta(days=today.weekday())
        month_start = today.replace(day=1)
        state_devices = state.get("devices", {})
        token_ids = set(state_devices) | set(registry)
        for token_id in token_ids:
            device = state_devices.get(token_id, {})
            if not isinstance(device, dict):
                continue
            registered = registry.get(token_id, {})
            totals = {field: 0 for field in USAGE_FIELDS}
            weekly = {field: 0 for field in USAGE_FIELDS}
            monthly = {field: 0 for field in USAGE_FIELDS}
            sessions = device.get("sessions")
            if isinstance(sessions, dict):
                for session in sessions.values():
                    if not isinstance(session, dict):
                        continue
                    for field in USAGE_FIELDS:
                        totals[field] += max(0, int(session.get(field, 0)))
                    daily_usage = session.get("daily_usage")
                    if not isinstance(daily_usage, dict):
                        continue
                    for date_key, daily in daily_usage.items():
                        if not isinstance(daily, dict):
                            continue
                        try:
                            usage_date = datetime.strptime(str(date_key), "%Y-%m-%d").date()
                        except ValueError:
                            continue
                        for field in USAGE_FIELDS:
                            value = max(0, int(daily.get(field, 0)))
                            if week_start <= usage_date <= today:
                                weekly[field] += value
                            if month_start <= usage_date <= today:
                                monthly[field] += value
            results.append(
                {
                    "device_token_id": str(token_id),
                    "device_label": str(
                        registered.get("device_label") or device.get("device_label") or token_id
                    ),
                    "employee_name": registered.get("employee_name") or device.get("employee_name"),
                    "employee_id": registered.get("employee_id") or device.get("employee_id"),
                    "enabled": bool(registered.get("enabled", False)),
                    "session_count": len(sessions) if isinstance(sessions, dict) else 0,
                    "lease_count": max(0, int(device.get("lease_count", 0))),
                    "report_count": max(0, int(device.get("report_count", 0))),
                    "first_seen_at": device.get("first_seen_at"),
                    "last_seen_at": device.get("last_seen_at"),
                    "first_reported_at": device.get("first_reported_at"),
                    "last_reported_at": device.get("last_reported_at"),
                    "client_ip": device.get("last_client_ip"),
                    "source_ip": device.get("last_source_ip"),
                    "client_ip_history": device.get("client_ip_history", []),
                    "source_ip_history": device.get("source_ip_history", []),
                    "usage_timezone": "Asia/Shanghai",
                    "week_start_date": week_start.isoformat(),
                    "month_start_date": month_start.isoformat(),
                    "weekly": weekly,
                    "monthly": monthly,
                    **totals,
                }
            )
        return sorted(
            results,
            key=lambda item: (-float(item["last_reported_at"] or 0), item["device_label"]),
        )


class UsageQueryError(RuntimeError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"usage query failed with HTTP {status_code}")


class OAuthRefreshError(RuntimeError):
    def __init__(self, status_code: int, error_code: str | None = None):
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(f"OAuth refresh failed with HTTP {status_code}")


def _oauth_refresh_probe_result(exc: OAuthRefreshError) -> dict[str, Any]:
    status = classify_probe_failure(exc.status_code, exc.error_code, None)
    if status == "probe_error" and 400 <= exc.status_code < 500:
        status = "auth"
    return {
        "status": status,
        "available": None if status == "probe_error" else False,
        "http_status": exc.status_code,
        "error_code": exc.error_code or "oauth_refresh_failed",
        "error_message": "OAuth refresh failed",
    }


class AccountRecord:
    def __init__(
        self,
        alias: str,
        auth_path: Path,
        *,
        models_url: str = DEFAULT_CODEX_MODELS_URL,
        responses_url: str = DEFAULT_CODEX_RESPONSES_URL,
    ):
        self.alias = alias
        self.auth_path = auth_path
        self.models_url = models_url
        self.responses_url = responses_url
        self.lock = asyncio.Lock()
        self.usage_cache: tuple[float, dict[str, Any] | None] = (0.0, None)
        self.official_refresh_required = False

    def read_auth(self) -> dict[str, Any]:
        payload = json.loads(self.auth_path.read_text(encoding="utf-8"))
        tokens = payload.get("tokens")
        if not isinstance(tokens, dict):
            raise ValueError(f"{self.alias}: tokens are missing")
        for key in ("access_token", "refresh_token"):
            if not isinstance(tokens.get(key), str) or not tokens[key]:
                raise ValueError(f"{self.alias}: {key} is missing")
        return payload

    async def ensure_fresh(
        self,
        client: httpx.AsyncClient,
        refresh_window: int,
        *,
        force: bool = False,
        reason: str = "expiry_window",
        rejected_access_token: str | None = None,
    ) -> dict[str, Any]:
        async with self.lock:
            payload = self.read_auth()
            tokens = payload["tokens"]
            if (
                force
                and rejected_access_token
                and tokens["access_token"] != rejected_access_token
            ):
                self.official_refresh_required = False
                return payload
            try:
                expires_at = token_expiry(tokens["access_token"])
            except ValueError:
                expires_at = 0
            if not force and expires_at > int(time.time()) + refresh_window:
                return payload

            response = await client.post(
                OAUTH_TOKEN_URL,
                headers={"Content-Type": "application/json"},
                json={
                    "client_id": OAUTH_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": tokens["refresh_token"],
                },
            )
            if response.status_code >= 400:
                error_code = None
                try:
                    error_payload = response.json()
                except (json.JSONDecodeError, ValueError):
                    error_payload = None
                if isinstance(error_payload, dict):
                    error = error_payload.get("error")
                    if isinstance(error, dict) and isinstance(error.get("code"), str):
                        error_code = error["code"]
                    elif isinstance(error, str):
                        error_code = error
                raise OAuthRefreshError(response.status_code, error_code)
            refreshed = response.json()
            for key in ("id_token", "access_token", "refresh_token"):
                value = refreshed.get(key)
                if isinstance(value, str) and value:
                    tokens[key] = value
            payload["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            payload["last_refresh_reason"] = reason
            atomic_write_json(self.auth_path, payload)
            self.usage_cache = (0.0, None)
            self.official_refresh_required = False
            return payload

    @staticmethod
    def _codex_headers(payload: dict[str, Any]) -> dict[str, str]:
        tokens = payload["tokens"]
        access_token = tokens["access_token"]
        account_id = tokens.get("account_id") or token_account_id(access_token)
        if not account_id:
            raise ValueError("ChatGPT account id is missing")
        return {
            "Authorization": f"Bearer {access_token}",
            "ChatGPT-Account-Id": account_id,
            "Content-Type": "application/json",
            "User-Agent": "codex-cli",
        }

    async def _codex_request(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
        method: str,
        url: str,
        timeout: int,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        async def send(auth_payload: dict[str, Any]) -> httpx.Response:
            request = client.request(
                method,
                url,
                headers=self._codex_headers(auth_payload),
                json=json_body,
                timeout=timeout,
            )
            return await asyncio.wait_for(request, timeout=timeout)

        response = await send(payload)
        if response.status_code != 401:
            return response

        rejected_token = payload["tokens"]["access_token"]
        refreshed = await self.ensure_fresh(
            client,
            0,
            force=True,
            reason="model_probe_unauthorized",
            rejected_access_token=rejected_token,
        )
        if refreshed is not payload:
            payload.clear()
            payload.update(refreshed)
        return await send(payload)

    async def discover_models(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
        timeout: int,
    ) -> list[str]:
        """Discover supported Codex text models without retaining response content."""
        try:
            response = await self._codex_request(
                client, payload, "GET", self.models_url, timeout
            )
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
            raise RuntimeError("model discovery timed out") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError("model discovery request failed") from exc
        if response.status_code >= 400:
            raise RuntimeError(f"model discovery failed with HTTP {response.status_code}")
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError("model discovery returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise RuntimeError("model discovery returned a non-object")
        models = filter_codex_text_models(body)
        if not models:
            raise RuntimeError("model discovery returned no supported text models")
        return models

    @staticmethod
    def _probe_error(response: httpx.Response) -> tuple[str | None, str | None] | None:
        payloads: list[dict[str, Any]] = []
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            body = None
        if isinstance(body, dict):
            payloads.append(body)
        else:
            for line in response.text.splitlines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(event, dict):
                    payloads.append(event)

        for payload in payloads:
            error = payload.get("error")
            if not isinstance(error, dict):
                response_payload = payload.get("response")
                if isinstance(response_payload, dict):
                    error = response_payload.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message")
                return (
                    code if isinstance(code, str) else None,
                    message if isinstance(message, str) else None,
                )
        if response.status_code >= 400:
            return (None, f"HTTP {response.status_code}")
        return None

    async def probe_model(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
        model: str,
        timeout: int,
    ) -> dict[str, Any]:
        """Send one fixed minimal probe and return only sanitized structured status."""
        request_body = {
            "model": model,
            "input": MODEL_PROBE_INPUT,
            "store": False,
            "max_output_tokens": 16,
        }
        try:
            response = await self._codex_request(
                client,
                payload,
                "POST",
                self.responses_url,
                timeout,
                json_body=request_body,
            )
        except (asyncio.TimeoutError, httpx.TimeoutException):
            return {
                "status": "probe_error",
                "available": None,
                "error_code": "timeout",
                "error_message": "request timed out",
            }
        except OAuthRefreshError as exc:
            return _oauth_refresh_probe_result(exc)
        except (httpx.HTTPError, OSError, ValueError):
            return {
                "status": "probe_error",
                "available": None,
                "error_code": "request_failed",
                "error_message": "probe request failed",
            }

        error = self._probe_error(response)
        if error is None:
            return {
                "status": "available",
                "available": True,
                "http_status": response.status_code,
            }
        error_code, error_message = error
        status = classify_probe_failure(response.status_code, error_code, error_message)
        return {
            "status": status,
            "available": None if status == "probe_error" else False,
            "http_status": response.status_code,
            **({"error_code": error_code} if error_code else {}),
            **({"error_message": error_message} if error_message else {}),
        }

    async def usage(self, client: httpx.AsyncClient, payload: dict[str, Any], cache_seconds: int) -> dict[str, Any]:
        cached_at, cached = self.usage_cache
        if cached is not None and time.time() - cached_at < cache_seconds:
            return cached
        tokens = payload["tokens"]
        access_token = tokens["access_token"]
        account_id = tokens.get("account_id") or token_account_id(access_token)
        headers = {"Authorization": f"Bearer {access_token}", "User-Agent": "codex-cli"}
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        response = await client.get(CHATGPT_USAGE_URL, headers=headers)
        if response.status_code >= 400:
            raise UsageQueryError(response.status_code)
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError("usage query returned a non-object")
        self.usage_cache = (time.time(), result)
        return result


def _window_used_percent(window: Any) -> float | None:
    if not isinstance(window, dict):
        return None
    value = window.get("used_percent")
    return float(value) if isinstance(value, (int, float)) else None


def usage_score(usage: dict[str, Any] | None) -> float:
    if not usage:
        return 50.0
    candidates: list[float] = []
    for key in ("primary_window", "secondary_window"):
        value = _window_used_percent(usage.get(key))
        if value is not None:
            candidates.append(value)
    rate_limit = usage.get("rate_limit")
    if isinstance(rate_limit, dict):
        for key in ("primary_window", "secondary_window"):
            value = _window_used_percent(rate_limit.get(key))
            if value is not None:
                candidates.append(value)
    return max(candidates) if candidates else 50.0


def _usage_windows(usage: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(usage, dict):
        return []
    windows: list[dict[str, Any]] = []
    sources = [usage]
    rate_limit = usage.get("rate_limit")
    if isinstance(rate_limit, dict):
        sources.insert(0, rate_limit)
    for source in sources:
        for key in ("primary_window", "secondary_window"):
            window = source.get(key)
            if isinstance(window, dict):
                windows.append(window)
    return windows


def _seconds_until_reset(window: dict[str, Any], now: float) -> float | None:
    reset_after = window.get("reset_after_seconds")
    if isinstance(reset_after, (int, float)):
        return max(60.0, float(reset_after))
    reset_at = window.get("reset_at")
    if isinstance(reset_at, (int, float)):
        return max(60.0, float(reset_at) - now)
    window_seconds = window.get("limit_window_seconds", window.get("window_seconds"))
    if isinstance(window_seconds, (int, float)) and window_seconds > 0:
        return float(window_seconds)
    return None


def account_selection_key(
    usage: dict[str, Any] | None,
    active_leases: int,
    *,
    now: float | None = None,
) -> tuple[int, int, float, float]:
    """Prefer healthy allowance that has lots left and will reset soon.

    The reset-seconds-per-remaining-percent ratio models allowance expiry:
    lower values mean more unused allowance is about to disappear. Accounts
    below 20% effective remaining capacity are kept behind healthy/unknown
    accounts. Within the same capacity band, the fewest active leases wins;
    reset urgency then breaks ties so allowance nearing expiry is still used.
    """
    windows = _usage_windows(usage)
    if not windows:
        return (1, max(0, active_leases), float("inf"), -50.0)
    current_time = time.time() if now is None else now
    remaining_values: list[float] = []
    expiry_ratios: list[float] = []
    for window in windows:
        used = _window_used_percent(window)
        if used is None:
            continue
        remaining = max(0.0, 100.0 - min(100.0, max(0.0, used)))
        remaining_values.append(remaining)
        seconds = _seconds_until_reset(window, current_time)
        if seconds is not None and remaining > 0:
            expiry_ratios.append(seconds / remaining)
    if not remaining_values:
        return (1, max(0, active_leases), float("inf"), -50.0)
    effective_remaining = min(remaining_values)
    capacity_band = 0 if effective_remaining >= 20.0 else 2
    expiry_ratio = min(expiry_ratios) if expiry_ratios else float("inf")
    return (capacity_band, max(0, active_leases), expiry_ratio, -effective_remaining)


def _model_health_number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def sanitized_model_health(record: dict[str, Any] | None, now: float) -> dict[str, Any]:
    """Project persistent probe state into the small, non-secret admin contract."""
    record = record if isinstance(record, dict) else {}
    result: dict[str, Any] = {
        "available": account_probe_available(record, now),
        "models": [],
    }
    for field in ("last_probe_at", "next_probe_at"):
        value = _model_health_number(record.get(field))
        if value is not None:
            result[field] = value

    models = record.get("models")
    if not isinstance(models, dict):
        return result
    allowed_statuses = {"available", "capacity", "quota", "auth", "unsupported", "probe_error"}
    for name in sorted(name for name in models if isinstance(name, str) and name):
        model = models[name]
        if not isinstance(model, dict):
            continue
        status = model.get("status")
        availability = model.get("available")
        item: dict[str, Any] = {
            "name": name,
            "status": status if status in allowed_statuses else "probe_error",
            "available": availability if availability is True or availability is False or availability is None else None,
        }
        for field in ("last_probe_at", "last_probe_error_at", "cooldown_until"):
            value = _model_health_number(model.get(field))
            if value is not None:
                item[field] = value
        http_status = model.get("http_status")
        if isinstance(http_status, int) and not isinstance(http_status, bool):
            item["http_status"] = http_status
        error_code = model.get("error_code")
        if isinstance(error_code, str):
            item["error_code"] = error_code[:120]
        error_message = model.get("error_message")
        if isinstance(error_message, str):
            item["error_message"] = error_message[:500]
        result["models"].append(item)
    return result


class TokenBroker:
    def __init__(self, settings: BrokerSettings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.owns_client = client is None
        self.client = client if client is not None else httpx.AsyncClient(timeout=20.0)
        self.accounts = self._discover_accounts()
        self.state_lock = asyncio.Lock()
        self.model_health_store = ModelHealthStore(settings.model_health_file)
        self.probe_lock = asyncio.Lock()

    def _discover_accounts(self) -> dict[str, AccountRecord]:
        paths: list[tuple[str, Path]] = []
        if self.settings.primary_auth.is_file():
            paths.append(("primary", self.settings.primary_auth))
        if self.settings.accounts_dir.is_dir():
            for auth_path in sorted(self.settings.accounts_dir.glob("*/auth.json")):
                paths.append((auth_path.parent.name, auth_path))
        return {
            alias: AccountRecord(
                alias,
                path,
                models_url=self.settings.codex_models_url,
                responses_url=self.settings.codex_responses_url,
            )
            for alias, path in paths
        }

    async def probe_models_once(self, now: float | None = None) -> dict[str, Any]:
        """Probe every account once, with account-level concurrency and no overlapping rounds."""
        async with self.probe_lock:
            probed_at = time.time() if now is None else now
            prior_state = self.model_health_store.load()
            prior_accounts = prior_state.get("accounts", {})
            semaphore = asyncio.Semaphore(self.settings.model_probe_concurrency)

            async def probe_account(alias: str, account: AccountRecord) -> tuple[str, dict[str, Any]]:
                async with semaphore:
                    previous = prior_accounts.get(alias)
                    previous_models = (
                        previous.get("models", {}) if isinstance(previous, dict) else {}
                    )
                    auth_failure: dict[str, Any] | None = None
                    try:
                        auth = await account.ensure_fresh(
                            self.client, self.settings.refresh_window_seconds
                        )
                    except OAuthRefreshError as exc:
                        auth = None
                        auth_failure = _oauth_refresh_probe_result(exc)
                    except Exception:
                        auth = None

                    discovery_source = "remote"
                    if auth is None:
                        models = list(self.settings.model_fallbacks) or list(previous_models)
                        discovery_source = "fallback"
                    else:
                        try:
                            models = await account.discover_models(
                                self.client,
                                auth,
                                self.settings.model_probe_timeout_seconds,
                            )
                        except OAuthRefreshError as exc:
                            auth_failure = _oauth_refresh_probe_result(exc)
                            models = list(self.settings.model_fallbacks) or list(previous_models)
                            discovery_source = "fallback"
                        except Exception:
                            models = list(self.settings.model_fallbacks)
                            discovery_source = "fallback"

                    model_records: dict[str, dict[str, Any]] = {}
                    for model in dict.fromkeys(models):
                        if auth_failure is not None:
                            result = auth_failure
                        elif auth is None:
                            result = {
                                "status": "probe_error",
                                "available": None,
                                "error_code": "auth_setup_failed",
                                "error_message": "account authentication failed",
                            }
                        else:
                            try:
                                result = await account.probe_model(
                                    self.client,
                                    auth,
                                    model,
                                    self.settings.model_probe_timeout_seconds,
                                )
                            except Exception:
                                result = {
                                    "status": "probe_error",
                                    "available": None,
                                    "error_code": "request_failed",
                                    "error_message": "probe request failed",
                                }
                        model_records[model] = merge_model_probe(
                            previous_models.get(model),
                            result,
                            probed_at,
                            self.settings.model_cooldown_seconds,
                        )

                    record: dict[str, Any] = {
                        "discovery_source": discovery_source,
                        "last_probe_at": probed_at,
                        "next_probe_at": probed_at
                        + self.settings.model_probe_interval_seconds,
                        "models": model_records,
                    }
                    record["available"] = account_probe_available(record, probed_at)
                    return alias, record

            results = await asyncio.gather(
                *(probe_account(alias, account) for alias, account in self.accounts.items())
            )
            for alias, record in results:
                self.model_health_store.replace_account(alias, record)
            return self.model_health_store.load()

    async def model_probe_loop(self) -> None:
        """Run one immediate model-health round and then repeat at the configured interval."""
        while True:
            try:
                await self.probe_models_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient round failure must not terminate future health checks.
                pass
            await asyncio.sleep(self.settings.model_probe_interval_seconds)

    def _load_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.settings.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"leases": {}}
        return state if isinstance(state, dict) else {"leases": {}}

    def _save_state(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.settings.state_file, state)

    async def _account_snapshot(self, account: AccountRecord) -> tuple[dict[str, Any], dict[str, Any] | None]:
        auth = await account.ensure_fresh(self.client, self.settings.refresh_window_seconds)
        try:
            usage = await account.usage(self.client, auth, self.settings.usage_cache_seconds)
        except UsageQueryError as exc:
            if exc.status_code != 401:
                return auth, None
            account.official_refresh_required = True
            auth = await account.ensure_fresh(
                self.client,
                self.settings.refresh_window_seconds,
                force=True,
                reason="official_unauthorized",
                rejected_access_token=auth["tokens"]["access_token"],
            )
            usage = await account.usage(self.client, auth, self.settings.usage_cache_seconds)
        except Exception:
            usage = None
        return auth, usage

    async def lease(
        self,
        identity: DeviceIdentity,
        client_device_id: str,
        requested_lease_id: str | None,
        client_ip: str | None = None,
        source_ip: str | None = None,
    ) -> dict[str, Any]:
        if not self.accounts:
            raise HTTPException(status_code=503, detail="no Codex accounts are configured")
        now = int(time.time())
        client_device_hash = hashlib.sha256(client_device_id.encode("utf-8")).hexdigest()
        async with self.state_lock:
            state = self._load_state()
            leases = state.setdefault("leases", {})
            health_store = getattr(self, "model_health_store", None)
            health_snapshot = health_store.load() if health_store is not None else {"accounts": {}}
            health_accounts = health_snapshot.get("accounts", {})
            if not isinstance(health_accounts, dict):
                health_accounts = {}

            def probe_available(alias: str) -> bool | None:
                record = health_accounts.get(alias)
                return account_probe_available(record if isinstance(record, dict) else None, now)

            for lease_id, lease in list(leases.items()):
                if not isinstance(lease, dict) or int(lease.get("expires_at", 0)) <= now:
                    leases.pop(lease_id, None)

            existing = leases.get(requested_lease_id) if requested_lease_id else None
            if isinstance(existing, dict) and existing.get("device_token_id") != identity.token_id:
                existing = None
            if isinstance(existing, dict):
                created_at = int(existing.get("created_at", now))
                if created_at + self.settings.lease_rotation_seconds <= now:
                    leases.pop(str(requested_lease_id), None)
                    existing = None
            if existing is None:
                for lease_id, lease in list(leases.items()):
                    if (
                        isinstance(lease, dict)
                        and lease.get("device_token_id") == identity.token_id
                        and lease.get("client_device_hash") == client_device_hash
                    ):
                        leases.pop(lease_id, None)
            alias = existing.get("account_alias") if isinstance(existing, dict) else None
            if alias not in self.accounts:
                alias = None
            elif probe_available(alias) is False:
                leases.pop(str(requested_lease_id), None)
                self._save_state(state)
                existing = None
                alias = None

            snapshots: dict[str, tuple[dict[str, Any], dict[str, Any] | None]] = {}
            if alias is None:
                active_counts = {name: 0 for name in self.accounts}
                for lease in leases.values():
                    leased_alias = lease.get("account_alias") if isinstance(lease, dict) else None
                    if leased_alias in active_counts:
                        active_counts[leased_alias] += 1
                candidates = [name for name in self.accounts if probe_available(name) is not False]
                if not candidates:
                    raise HTTPException(status_code=503, detail="all Codex accounts have blocked models")
                for name in candidates:
                    account = self.accounts[name]
                    try:
                        snapshots[name] = await self._account_snapshot(account)
                    except Exception:
                        continue
                if not snapshots:
                    raise HTTPException(status_code=503, detail="all Codex accounts are unavailable")
                alias = min(
                    snapshots,
                    key=lambda name: (
                        account_selection_key(
                            snapshots[name][1], active_counts[name], now=now
                        ),
                        name,
                    ),
                )

            if alias not in snapshots:
                try:
                    snapshots[alias] = await self._account_snapshot(self.accounts[alias])
                except Exception as exc:
                    raise HTTPException(status_code=503, detail=f"leased account is unavailable: {exc}") from exc

            auth, usage = snapshots[alias]
            tokens = auth["tokens"]
            access_token = tokens["access_token"]
            account_id = tokens.get("account_id") or token_account_id(access_token)
            if not account_id:
                raise HTTPException(status_code=503, detail="selected account has no ChatGPT account id")

            lease_id = requested_lease_id if isinstance(existing, dict) else secrets.token_urlsafe(24)
            lease_expires = now + self.settings.lease_seconds
            leases[lease_id] = {
                "account_alias": alias,
                "device_token_id": identity.token_id,
                "device_label": identity.label,
                "client_device_hash": client_device_hash,
                "client_ip": client_ip,
                "source_ip": source_ip,
                "created_at": int(existing.get("created_at", now)) if isinstance(existing, dict) else now,
                "expires_at": lease_expires,
            }
            self._save_state(state)
            return {
                "lease_id": lease_id,
                "lease_expires_at": lease_expires,
                "account_alias": alias,
                "account_id": account_id,
                "plan_type": token_plan_type(access_token),
                "access_token": access_token,
                "access_token_expires_at": token_expiry(access_token),
                "usage": usage,
            }

    async def account_status(self, refresh_usage: bool = False) -> list[dict[str, Any]]:
        state = self._load_state()
        now = time.time()
        health_store = getattr(self, "model_health_store", None)
        health_snapshot = health_store.load() if health_store is not None else {"accounts": {}}
        health_accounts = health_snapshot.get("accounts", {})
        if not isinstance(health_accounts, dict):
            health_accounts = {}
        active_counts: Counter[str] = Counter(
            lease.get("account_alias")
            for lease in state.get("leases", {}).values()
            if isinstance(lease, dict) and float(lease.get("expires_at", 0)) > now
        )
        results: list[dict[str, Any]] = []
        for alias, account in self.accounts.items():
            try:
                if refresh_usage:
                    account.usage_cache = (0.0, None)
                auth, usage = await self._account_snapshot(account)
                tokens = auth["tokens"]
                access_token = tokens["access_token"]
                access_token_expires_at = token_expiry(access_token)
                results.append(
                    {
                        "alias": alias,
                        "email": token_email(tokens.get("id_token"), access_token),
                        "available": True,
                        "plan_type": token_plan_type(access_token),
                        "access_token_expires_at": access_token_expires_at,
                        "token_refresh_required": access_token_expires_at
                        <= int(time.time()) + self.settings.refresh_window_seconds
                        or account.official_refresh_required,
                        "last_token_refresh_at": auth.get("last_refresh"),
                        "last_token_refresh_reason": auth.get("last_refresh_reason"),
                        "usage_score": usage_score(usage),
                        "active_leases": active_counts.get(alias, 0),
                        "usage": usage,
                        "model_health": sanitized_model_health(
                            health_accounts.get(alias) if isinstance(health_accounts.get(alias), dict) else None,
                            now,
                        ),
                    }
                )
            except Exception as exc:
                result: dict[str, Any] = {
                    "alias": alias,
                    "available": False,
                    "error": str(exc),
                    "model_health": sanitized_model_health(
                        health_accounts.get(alias) if isinstance(health_accounts.get(alias), dict) else None,
                        now,
                    ),
                }
                try:
                    auth = account.read_auth()
                    access_token = auth["tokens"]["access_token"]
                    access_token_expires_at = token_expiry(access_token)
                    result.update(
                        {
                            "email": token_email(auth["tokens"].get("id_token"), access_token),
                            "plan_type": token_plan_type(access_token),
                            "access_token_expires_at": access_token_expires_at,
                            "token_refresh_required": access_token_expires_at
                            <= int(time.time()) + self.settings.refresh_window_seconds
                            or account.official_refresh_required,
                            "last_token_refresh_at": auth.get("last_refresh"),
                            "last_token_refresh_reason": auth.get("last_refresh_reason"),
                        }
                    )
                except Exception:
                    pass
                results.append(result)
        return results


def _bearer_value(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization[7:]


def normalize_ip(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid IP address") from exc


def request_source_ip(request: FastAPIRequest) -> str | None:
    return normalize_ip(request.client.host if request.client else None)


def load_device_registry() -> dict[str, Any]:
    try:
        registry = json.loads(settings.device_token_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 2, "devices": []}
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=503, detail="device token registry is unavailable") from exc
    if not isinstance(registry, dict) or not isinstance(registry.get("devices"), list):
        raise HTTPException(status_code=503, detail="device token registry is invalid")
    return registry


def save_device_registry_with_lock(
    update: Any,
) -> Any:
    lock_path = settings.device_token_file.with_suffix(
        settings.device_token_file.suffix + ".lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        try:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
        except ImportError:
            pass
        registry = load_device_registry()
        result = update(registry)
        atomic_write_json(settings.device_token_file, registry)
        return result


def portal_users() -> list[dict[str, Any]]:
    devices = load_device_registry().get("devices", [])
    users = [
        public_portal_user(device)
        for device in devices
        if isinstance(device, dict) and device.get("portal_user") is True
    ]
    return sorted(users, key=lambda item: str(item.get("username") or ""))


settings = BrokerSettings.from_env()
broker = TokenBroker(settings)
usage_store = DeviceUsageStore(settings.usage_state_file)


@asynccontextmanager
async def lifespan(application: FastAPI):
    probe_task = asyncio.create_task(broker.model_probe_loop())
    application.state.model_probe_task = probe_task
    try:
        yield
    finally:
        probe_task.cancel()
        try:
            await probe_task
        except asyncio.CancelledError:
            pass
        if broker.owns_client:
            await broker.client.aclose()


app = FastAPI(title="CWS Codex Plus Broker", version=BROKER_VERSION, lifespan=lifespan)
device_registry_write_lock = asyncio.Lock()
token_query_failure_lock = asyncio.Lock()
token_query_failures: dict[str, list[float]] = {}
dummy_password_hash = hash_user_password("invalid-portal-password")


def token_query_key(request: FastAPIRequest, username: str) -> str:
    try:
        source = request_source_ip(request) or "unknown"
    except HTTPException:
        # Some trusted ASGI proxies and test clients expose a hostname instead
        # of a literal IP address. Authentication must still work, while the
        # username portion keeps the fallback rate-limit key specific.
        source = "unknown"
    return f"{source}:{username.strip().upper()[:64]}"


async def enforce_token_query_limit(key: str) -> None:
    now = time.time()
    async with token_query_failure_lock:
        recent = [
            value
            for value in token_query_failures.get(key, [])
            if now - value < TOKEN_QUERY_WINDOW_SECONDS
        ]
        if recent:
            token_query_failures[key] = recent
        else:
            token_query_failures.pop(key, None)
        if len(recent) >= TOKEN_QUERY_MAX_FAILURES:
            raise HTTPException(status_code=429, detail="too many failed token queries")


async def record_token_query_failure(key: str) -> None:
    async with token_query_failure_lock:
        token_query_failures.setdefault(key, []).append(time.time())


async def clear_token_query_failures(key: str) -> None:
    async with token_query_failure_lock:
        token_query_failures.pop(key, None)


def _legacy_device_identity(supplied: str) -> DeviceIdentity:
    suffix = hashlib.sha256(supplied.encode("utf-8")).hexdigest()[:20]
    return DeviceIdentity(token_id=f"legacy-{suffix}", label=f"legacy-{suffix[:8]}", legacy=True)


def _registered_device_identity(supplied: str) -> DeviceIdentity | None:
    try:
        registry = json.loads(settings.device_token_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    supplied_hash = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
    devices = registry.get("devices") if isinstance(registry, dict) else None
    if not isinstance(devices, list):
        return None
    for device in devices:
        if not isinstance(device, dict) or not device.get("enabled", False):
            continue
        expected_hash = device.get("token_hash")
        if isinstance(expected_hash, str) and hmac.compare_digest(supplied_hash, expected_hash):
            token_id = device.get("id")
            label = device.get("label")
            if isinstance(token_id, str) and token_id:
                employee_name = device.get("employee_name")
                employee_id = device.get("employee_id")
                return DeviceIdentity(
                    token_id=token_id,
                    label=str(label or token_id),
                    employee_name=str(employee_name) if employee_name else None,
                    employee_id=str(employee_id) if employee_id else None,
                )
    return None


def _device_registry_summary() -> dict[str, dict[str, Any]]:
    # Legacy environment tokens remain valid for compatibility, but are not
    # listed as employees until they produce an actual activity record.
    result: dict[str, dict[str, Any]] = {}
    try:
        registry = json.loads(settings.device_token_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return result
    devices = registry.get("devices") if isinstance(registry, dict) else None
    if not isinstance(devices, list):
        return result
    for device in devices:
        if not isinstance(device, dict) or not isinstance(device.get("id"), str):
            continue
        token_id = str(device["id"])
        result[token_id] = {
            "device_label": str(device.get("label") or token_id),
            "employee_name": device.get("employee_name"),
            "employee_id": device.get("employee_id"),
            "enabled": bool(device.get("enabled", False)),
        }
    return result


async def require_device(authorization: str | None = Header(default=None)) -> DeviceIdentity:
    supplied = _bearer_value(authorization)
    for expected in settings.device_tokens:
        if hmac.compare_digest(supplied, expected):
            return _legacy_device_identity(supplied)
    identity = _registered_device_identity(supplied)
    if identity is not None:
        return identity
    raise HTTPException(status_code=401, detail="invalid device token")


async def require_admin(authorization: str | None = Header(default=None)) -> None:
    supplied = _bearer_value(authorization)
    if not settings.admin_token or not hmac.compare_digest(supplied, settings.admin_token):
        raise HTTPException(status_code=401, detail="invalid admin token")


RELEASE_MEDIA_TYPES = {
    "CWS-Codex-Setup-v0.1.7.exe": "application/vnd.microsoft.portable-executable",
    "CWS-Codex-Release-v0.1.7.zip": "application/zip",
    "CWS-Codex-Windows-v0.1.7.zip": "application/zip",
    "CWS-Codex-Linux-v0.1.7.tar.gz": "application/gzip",
    "CWS-Codex-Server-v0.1.7.tar.gz": "application/gzip",
}


def load_validated_release_manifest() -> tuple[dict[str, Any], dict[str, Path]]:
    release_dir = settings.release_dir.resolve()
    manifest_path = release_dir / "latest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="release not found")
    except (json.JSONDecodeError, UnicodeError, OSError):
        raise HTTPException(status_code=503, detail="release metadata is unavailable")

    if (
        not isinstance(payload, dict)
        or set(payload) != {"release_version", "published_at", "assets"}
        or payload.get("release_version") != RELEASE_VERSION
        or not isinstance(payload.get("published_at"), str)
        or not payload["published_at"]
        or not isinstance(payload.get("assets"), list)
        or not payload["assets"]
    ):
        raise HTTPException(status_code=503, detail="release metadata is invalid")

    candidates: dict[str, Path] = {}
    for asset in payload["assets"]:
        if (
            not isinstance(asset, dict)
            or set(asset) != {"platform", "name", "size", "sha256", "download_url"}
        ):
            raise HTTPException(status_code=503, detail="release metadata is invalid")
        name = asset.get("name")
        size = asset.get("size")
        checksum = asset.get("sha256")
        if (
            not valid_asset_name(name)
            or name in candidates
            or asset.get("platform") != ASSET_PLATFORMS[name]
            or type(size) is not int
            or size < 0
            or not isinstance(checksum, str)
            or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
            or asset.get("download_url") != f"{DOWNLOAD_PREFIX}{name}"
        ):
            raise HTTPException(status_code=503, detail="release metadata is invalid")

        candidate = (release_dir / name).resolve()
        if candidate.parent != release_dir:
            raise HTTPException(status_code=503, detail="release metadata is invalid")
        try:
            stat = candidate.stat()
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="release asset not found")
        except OSError:
            raise HTTPException(status_code=503, detail="release asset is unavailable")
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="release asset not found")
        try:
            actual_checksum = sha256_file(candidate)
        except OSError:
            raise HTTPException(status_code=503, detail="release asset is unavailable")
        if stat.st_size != size or not hmac.compare_digest(actual_checksum, checksum):
            raise HTTPException(status_code=503, detail="release asset validation failed")
        candidates[name] = candidate
    return payload, candidates


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "accounts": len(broker.accounts), "version": BROKER_VERSION}


@app.get("/v1/client/releases/latest")
async def latest_client_release(
    response: Response,
    _identity: DeviceIdentity = Depends(require_device),
) -> dict[str, Any]:
    manifest, _candidates = load_validated_release_manifest()
    response.headers["Cache-Control"] = "private, no-cache"
    return manifest


@app.get("/v1/client/releases/download/{filename}")
async def download_client_release(
    filename: str,
    _identity: DeviceIdentity = Depends(require_device),
) -> FileResponse:
    manifest, candidates = load_validated_release_manifest()
    if not valid_asset_name(filename):
        raise HTTPException(status_code=404, detail="release asset not found")
    candidate = (settings.release_dir.resolve() / filename).resolve()
    if candidate.parent != settings.release_dir.resolve():
        raise HTTPException(status_code=404, detail="release asset not found")
    if filename not in candidates:
        raise HTTPException(status_code=404, detail="release asset not found")
    asset = next(item for item in manifest["assets"] if item["name"] == filename)
    return FileResponse(
        candidate,
        media_type=RELEASE_MEDIA_TYPES[filename],
        filename=filename,
        headers={
            "Cache-Control": "private",
            "Content-Length": str(asset["size"]),
        },
    )


@app.get("/quota", response_class=HTMLResponse, include_in_schema=False)
async def quota_dashboard() -> HTMLResponse:
    dashboard_path = Path(__file__).with_name("codex_quota_dashboard.html")
    try:
        return HTMLResponse(dashboard_path.read_text(encoding="utf-8"))
    except OSError:
        raise HTTPException(status_code=503, detail="quota dashboard asset is unavailable")


@app.get("/token", response_class=HTMLResponse, include_in_schema=False)
async def token_portal() -> HTMLResponse:
    portal_path = Path(__file__).with_name("codex_token_portal.html")
    try:
        return HTMLResponse(
            portal_path.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )
    except OSError:
        raise HTTPException(status_code=503, detail="token portal asset is unavailable")


@app.post("/v1/token/query")
async def query_own_token(
    request: PortalTokenQueryRequest,
    http_request: FastAPIRequest,
    response: Response,
) -> dict[str, Any]:
    key = token_query_key(http_request, request.username)
    await enforce_token_query_limit(key)
    registry = load_device_registry()
    user = find_portal_user(registry, request.username)
    encoded = str(user.get("password_hash")) if user else dummy_password_hash
    valid = verify_user_password(request.password, encoded)
    if not user or not user.get("enabled", False) or not valid:
        await record_token_query_failure(key)
        raise HTTPException(status_code=401, detail="invalid username or password")
    if not settings.user_token_secret:
        raise HTTPException(status_code=503, detail="user token secret is not configured")
    token = derive_user_device_token(
        settings.user_token_secret,
        str(user["username"]),
        str(user["employee_id"]),
    )
    expected_hash = str(user.get("token_hash") or "")
    actual_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not expected_hash or not hmac.compare_digest(actual_hash, expected_hash):
        raise HTTPException(status_code=503, detail="user token secret does not match registry")
    await clear_token_query_failures(key)
    response.headers["Cache-Control"] = "no-store"
    return {
        "username": user["username"],
        "employee_id": user["employee_id"],
        "device_token": token,
    }


@app.post("/v1/lease")
async def create_lease(
    request: LeaseRequest,
    http_request: FastAPIRequest,
    identity: DeviceIdentity = Depends(require_device),
) -> dict[str, Any]:
    client_ip = normalize_ip(request.client_ip)
    source_ip = request_source_ip(http_request)
    result = await broker.lease(
        identity,
        request.device_id,
        request.lease_id,
        client_ip=client_ip,
        source_ip=source_ip,
    )
    await usage_store.record_lease(identity, client_ip, source_ip)
    return result


@app.post("/v1/usage/report")
async def report_usage(
    request: UsageReport,
    http_request: FastAPIRequest,
    identity: DeviceIdentity = Depends(require_device),
) -> dict[str, Any]:
    if not request.sessions or len(request.sessions) > 500:
        raise HTTPException(status_code=422, detail="sessions must contain between 1 and 500 records")
    return await usage_store.record(
        identity,
        request.sessions,
        client_ip=normalize_ip(request.client_ip),
        source_ip=request_source_ip(http_request),
    )


@app.get("/v1/admin/accounts", dependencies=[Depends(require_admin)])
async def list_accounts(refresh: bool = False) -> dict[str, Any]:
    return {
        "version": BROKER_VERSION,
        "accounts": await broker.account_status(refresh_usage=refresh),
        "fetched_at": time.time(),
    }


@app.get("/v1/admin/device-usage", dependencies=[Depends(require_admin)])
async def list_device_usage() -> dict[str, Any]:
    return {
        "version": BROKER_VERSION,
        "devices": await usage_store.summary(_device_registry_summary()),
        "fetched_at": time.time(),
    }


@app.get("/v1/admin/users", dependencies=[Depends(require_admin)])
async def list_portal_users() -> dict[str, Any]:
    return {"version": BROKER_VERSION, "users": portal_users(), "fetched_at": time.time()}


@app.post("/v1/admin/users", dependencies=[Depends(require_admin)])
async def add_portal_user(request: PortalUserCreateRequest) -> dict[str, Any]:
    if not settings.user_token_secret:
        raise HTTPException(status_code=503, detail="user token secret is not configured")
    async with device_registry_write_lock:
        try:
            user, _token = save_device_registry_with_lock(
                lambda registry: create_portal_user(
                    registry,
                    request.username,
                    request.employee_id,
                    settings.user_token_secret or "",
                )
            )
        except ValueError as exc:
            message = str(exc)
            status_code = 409 if "already exists" in message else 422
            raise HTTPException(status_code=status_code, detail=message) from exc
    return {"user": public_portal_user(user)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("CWS_CODEX_HOST", "127.0.0.1"), port=int(os.getenv("CWS_CODEX_PORT", "8765")))
