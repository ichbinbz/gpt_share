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
import secrets
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request as FastAPIRequest
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CHATGPT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DEFAULT_PRIMARY_AUTH = "/var/lib/cws-codex/auth.json"
DEFAULT_ACCOUNTS_DIR = "/var/lib/cws-codex/accounts"
DEFAULT_STATE_FILE = "/var/lib/cws-codex/broker-state.json"
DEFAULT_USAGE_STATE_FILE = "/var/lib/cws-codex/device-usage.json"
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


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
    device_tokens: tuple[str, ...]
    admin_token: str | None
    lease_seconds: int
    refresh_window_seconds: int
    usage_cache_seconds: int

    @classmethod
    def from_env(cls) -> "BrokerSettings":
        raw_tokens = os.getenv("CWS_CODEX_DEVICE_TOKENS", "")
        device_tokens = tuple(token.strip() for token in raw_tokens.split(",") if token.strip())
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
            device_tokens=device_tokens,
            admin_token=os.getenv("CWS_CODEX_ADMIN_TOKEN") or None,
            lease_seconds=int(os.getenv("CWS_CODEX_LEASE_SECONDS", "28800")),
            refresh_window_seconds=int(os.getenv("CWS_CODEX_REFRESH_WINDOW_SECONDS", "900")),
            usage_cache_seconds=int(os.getenv("CWS_CODEX_USAGE_CACHE_SECONDS", "30")),
        )


class LeaseRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=200)
    lease_id: str | None = Field(default=None, max_length=200)
    client_ip: str | None = Field(default=None, max_length=45)


class SessionTokenUsage(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    input_tokens: int = Field(default=0, ge=0, le=10**15)
    cached_input_tokens: int = Field(default=0, ge=0, le=10**15)
    cache_write_input_tokens: int = Field(default=0, ge=0, le=10**15)
    output_tokens: int = Field(default=0, ge=0, le=10**15)
    reasoning_output_tokens: int = Field(default=0, ge=0, le=10**15)
    total_tokens: int = Field(default=0, ge=0, le=10**15)


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
            return {"version": 1, "devices": {}}
        if not isinstance(payload, dict) or not isinstance(payload.get("devices"), dict):
            return {"version": 1, "devices": {}}
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
                current = stored_sessions.setdefault(session_id, {})
                for field in TOKEN_FIELDS:
                    current[field] = max(int(current.get(field, 0)), int(incoming.get(field, 0)))
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
        state_devices = state.get("devices", {})
        token_ids = set(state_devices) | set(registry)
        for token_id in token_ids:
            device = state_devices.get(token_id, {})
            if not isinstance(device, dict):
                continue
            registered = registry.get(token_id, {})
            totals = {field: 0 for field in TOKEN_FIELDS}
            sessions = device.get("sessions")
            if isinstance(sessions, dict):
                for session in sessions.values():
                    if not isinstance(session, dict):
                        continue
                    for field in TOKEN_FIELDS:
                        totals[field] += max(0, int(session.get(field, 0)))
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
                    **totals,
                }
            )
        return sorted(
            results,
            key=lambda item: (-float(item["last_reported_at"] or 0), item["device_label"]),
        )


class AccountRecord:
    def __init__(self, alias: str, auth_path: Path):
        self.alias = alias
        self.auth_path = auth_path
        self.lock = asyncio.Lock()
        self.usage_cache: tuple[float, dict[str, Any] | None] = (0.0, None)

    def read_auth(self) -> dict[str, Any]:
        payload = json.loads(self.auth_path.read_text(encoding="utf-8"))
        tokens = payload.get("tokens")
        if not isinstance(tokens, dict):
            raise ValueError(f"{self.alias}: tokens are missing")
        for key in ("access_token", "refresh_token"):
            if not isinstance(tokens.get(key), str) or not tokens[key]:
                raise ValueError(f"{self.alias}: {key} is missing")
        return payload

    async def ensure_fresh(self, client: httpx.AsyncClient, refresh_window: int) -> dict[str, Any]:
        async with self.lock:
            payload = self.read_auth()
            tokens = payload["tokens"]
            try:
                expires_at = token_expiry(tokens["access_token"])
            except ValueError:
                expires_at = 0
            if expires_at > int(time.time()) + refresh_window:
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
                raise RuntimeError(f"OAuth refresh failed with HTTP {response.status_code}")
            refreshed = response.json()
            for key in ("id_token", "access_token", "refresh_token"):
                value = refreshed.get(key)
                if isinstance(value, str) and value:
                    tokens[key] = value
            payload["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            atomic_write_json(self.auth_path, payload)
            self.usage_cache = (0.0, None)
            return payload

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
            raise RuntimeError(f"usage query failed with HTTP {response.status_code}")
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
) -> tuple[int, float, int, float]:
    """Prefer healthy allowance that has lots left and will reset soon.

    The reset-seconds-per-remaining-percent ratio models allowance expiry:
    lower values mean more unused allowance is about to disappear. Accounts
    below 20% effective remaining capacity are kept behind healthy/unknown
    accounts, while active leases add a modest anti-concentration penalty.
    """
    windows = _usage_windows(usage)
    if not windows:
        return (1, float("inf"), max(0, active_leases), -50.0)
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
        return (1, float("inf"), max(0, active_leases), -50.0)
    effective_remaining = min(remaining_values)
    capacity_band = 0 if effective_remaining >= 20.0 else 2
    expiry_ratio = min(expiry_ratios) if expiry_ratios else float("inf")
    load_adjusted_ratio = expiry_ratio * (1.0 + max(0, active_leases) * 0.35)
    return (capacity_band, load_adjusted_ratio, max(0, active_leases), -effective_remaining)


class TokenBroker:
    def __init__(self, settings: BrokerSettings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=20.0)
        self.accounts = self._discover_accounts()
        self.state_lock = asyncio.Lock()

    def _discover_accounts(self) -> dict[str, AccountRecord]:
        paths: list[tuple[str, Path]] = []
        if self.settings.primary_auth.is_file():
            paths.append(("primary", self.settings.primary_auth))
        if self.settings.accounts_dir.is_dir():
            for auth_path in sorted(self.settings.accounts_dir.glob("*/auth.json")):
                paths.append((auth_path.parent.name, auth_path))
        return {alias: AccountRecord(alias, path) for alias, path in paths}

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
        async with self.state_lock:
            state = self._load_state()
            leases = state.setdefault("leases", {})
            for lease_id, lease in list(leases.items()):
                if not isinstance(lease, dict) or int(lease.get("expires_at", 0)) <= now:
                    leases.pop(lease_id, None)

            existing = leases.get(requested_lease_id) if requested_lease_id else None
            if isinstance(existing, dict) and existing.get("device_token_id") != identity.token_id:
                existing = None
            alias = existing.get("account_alias") if isinstance(existing, dict) else None
            if alias not in self.accounts:
                alias = None

            snapshots: dict[str, tuple[dict[str, Any], dict[str, Any] | None]] = {}
            if alias is None:
                active_counts = {name: 0 for name in self.accounts}
                for lease in leases.values():
                    leased_alias = lease.get("account_alias") if isinstance(lease, dict) else None
                    if leased_alias in active_counts:
                        active_counts[leased_alias] += 1
                for name, account in self.accounts.items():
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
                "client_device_hash": hashlib.sha256(client_device_id.encode("utf-8")).hexdigest(),
                "client_ip": client_ip,
                "source_ip": source_ip,
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
                results.append(
                    {
                        "alias": alias,
                        "email": token_email(tokens.get("id_token"), access_token),
                        "available": True,
                        "plan_type": token_plan_type(access_token),
                        "access_token_expires_at": token_expiry(access_token),
                        "usage_score": usage_score(usage),
                        "active_leases": active_counts.get(alias, 0),
                        "usage": usage,
                    }
                )
            except Exception as exc:
                results.append({"alias": alias, "available": False, "error": str(exc)})
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


settings = BrokerSettings.from_env()
broker = TokenBroker(settings)
usage_store = DeviceUsageStore(settings.usage_state_file)
app = FastAPI(title="CWS Codex Plus Broker", version="0.1.3")


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


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "accounts": len(broker.accounts)}


@app.get("/quota", response_class=HTMLResponse, include_in_schema=False)
async def quota_dashboard() -> HTMLResponse:
    dashboard_path = Path(__file__).with_name("codex_quota_dashboard.html")
    try:
        return HTMLResponse(dashboard_path.read_text(encoding="utf-8"))
    except OSError:
        raise HTTPException(status_code=503, detail="quota dashboard asset is unavailable")


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
        "accounts": await broker.account_status(refresh_usage=refresh),
        "fetched_at": time.time(),
    }


@app.get("/v1/admin/device-usage", dependencies=[Depends(require_admin)])
async def list_device_usage() -> dict[str, Any]:
    return {
        "devices": await usage_store.summary(_device_registry_summary()),
        "fetched_at": time.time(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("CWS_CODEX_HOST", "127.0.0.1"), port=int(os.getenv("CWS_CODEX_PORT", "8765")))
