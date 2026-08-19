import os
import time
from typing import Any

import httpx


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quota_window(name: str, payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    used = _number(payload.get("used_percent"))
    if used is None:
        return None
    used = min(100.0, max(0.0, used))
    return {
        "name": name,
        "used_percent": used,
        "remaining_percent": 100.0 - used,
        "window_seconds": _number(
            payload.get("limit_window_seconds", payload.get("window_seconds"))
        ),
        "reset_at": _number(payload.get("reset_at")),
        "reset_after_seconds": _number(payload.get("reset_after_seconds")),
    }


def normalize_codex_account(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "alias": "unknown",
            "available": False,
            "error": "Broker returned an invalid account record",
            "windows": [],
        }
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    rate_limit = usage.get("rate_limit") if isinstance(usage.get("rate_limit"), dict) else {}
    windows = [
        window
        for window in (
            _quota_window("primary", rate_limit.get("primary_window")),
            _quota_window("secondary", rate_limit.get("secondary_window")),
        )
        if window is not None
    ]
    credits = usage.get("credits") if isinstance(usage.get("credits"), dict) else {}
    return {
        "alias": str(payload.get("alias") or "unknown"),
        "available": bool(payload.get("available")),
        "plan_type": payload.get("plan_type"),
        "usage_score": _number(payload.get("usage_score")),
        "active_leases": int(_number(payload.get("active_leases")) or 0),
        "access_token_expires_at": _number(payload.get("access_token_expires_at")),
        "credits_balance": _number(credits.get("balance")),
        "credits_unlimited": bool(credits.get("unlimited", False)),
        "windows": windows,
        "error": str(payload.get("error")) if payload.get("error") else None,
    }


async def get_codex_broker_status(refresh: bool = False) -> dict[str, Any]:
    broker_url = os.getenv("CWS_CODEX_BROKER_URL", "http://127.0.0.1:8765").rstrip("/")
    admin_token = os.getenv("CWS_CODEX_ADMIN_TOKEN", "").strip()
    if not admin_token:
        return {
            "configured": False,
            "reachable": False,
            "accounts": [],
            "fetched_at": time.time(),
            "error": "CWS_CODEX_ADMIN_TOKEN is not configured in the CWS backend",
        }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{broker_url}/v1/admin/accounts",
                params={"refresh": str(refresh).lower()},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
    except httpx.HTTPError as exc:
        return {
            "configured": True,
            "reachable": False,
            "accounts": [],
            "fetched_at": time.time(),
            "error": f"Cannot connect to Codex Broker ({exc.__class__.__name__})",
        }

    if response.status_code != 200:
        message = "Codex Broker rejected the admin request" if response.status_code == 401 else "Codex Broker request failed"
        return {
            "configured": True,
            "reachable": True,
            "accounts": [],
            "fetched_at": time.time(),
            "error": f"{message} (HTTP {response.status_code})",
        }

    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("accounts"), list):
        return {
            "configured": True,
            "reachable": True,
            "accounts": [],
            "fetched_at": time.time(),
            "error": "Codex Broker returned an invalid response",
        }
    return {
        "configured": True,
        "reachable": True,
        "accounts": [normalize_codex_account(account) for account in payload["accounts"]],
        "fetched_at": _number(payload.get("fetched_at")) or time.time(),
        "error": None,
    }


def normalize_codex_device_usage(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    token_id = payload.get("device_token_id")
    if not isinstance(token_id, str) or not token_id:
        return None
    integer_fields = (
        "session_count",
        "lease_count",
        "report_count",
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    result: dict[str, Any] = {
        "device_token_id": token_id,
        "device_label": str(payload.get("device_label") or token_id),
        "employee_name": str(payload.get("employee_name")) if payload.get("employee_name") else None,
        "employee_id": str(payload.get("employee_id")) if payload.get("employee_id") else None,
        "enabled": bool(payload.get("enabled", False)),
        "client_ip": str(payload.get("client_ip")) if payload.get("client_ip") else None,
        "source_ip": str(payload.get("source_ip")) if payload.get("source_ip") else None,
        "first_seen_at": _number(payload.get("first_seen_at")),
        "last_seen_at": _number(payload.get("last_seen_at")),
        "first_reported_at": _number(payload.get("first_reported_at")),
        "last_reported_at": _number(payload.get("last_reported_at")),
    }
    for field in integer_fields:
        result[field] = max(0, int(_number(payload.get(field)) or 0))
    return result


async def get_codex_device_usage() -> dict[str, Any]:
    broker_url = os.getenv("CWS_CODEX_BROKER_URL", "http://127.0.0.1:8765").rstrip("/")
    admin_token = os.getenv("CWS_CODEX_ADMIN_TOKEN", "").strip()
    if not admin_token:
        return {
            "configured": False,
            "reachable": False,
            "devices": [],
            "fetched_at": time.time(),
            "error": "CWS_CODEX_ADMIN_TOKEN is not configured in the CWS backend",
        }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{broker_url}/v1/admin/device-usage",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
    except httpx.HTTPError as exc:
        return {
            "configured": True,
            "reachable": False,
            "devices": [],
            "fetched_at": time.time(),
            "error": f"Cannot connect to Codex Broker ({exc.__class__.__name__})",
        }
    if response.status_code != 200:
        return {
            "configured": True,
            "reachable": True,
            "devices": [],
            "fetched_at": time.time(),
            "error": f"Codex Broker device usage request failed (HTTP {response.status_code})",
        }
    try:
        payload = response.json()
    except ValueError:
        payload = None
    devices = payload.get("devices") if isinstance(payload, dict) else None
    if not isinstance(devices, list):
        return {
            "configured": True,
            "reachable": True,
            "devices": [],
            "fetched_at": time.time(),
            "error": "Codex Broker returned an invalid device usage response",
        }
    normalized = [item for item in (normalize_codex_device_usage(device) for device in devices) if item]
    return {
        "configured": True,
        "reachable": True,
        "devices": normalized,
        "fetched_at": _number(payload.get("fetched_at")) or time.time(),
        "error": None,
    }
