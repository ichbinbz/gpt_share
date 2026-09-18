"""Pure model-health filtering, classification, and durable state helpers."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


MODEL_STATUSES = {"available", "capacity", "quota", "auth", "unsupported", "probe_error"}
BLOCKING_STATUSES = {"capacity", "quota", "auth"}


def filter_codex_text_models(payload: dict[str, Any]) -> list[str]:
    """Return sorted, unique names for available Codex text models."""
    models = payload.get("models")
    if not isinstance(models, list):
        models = payload.get("data")
    if not isinstance(models, list):
        return []

    names: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            continue
        name = model.get("slug") or model.get("id")
        if "visibility" in model or "supported_in_api" in model:
            supported = (
                model.get("visibility") == "list"
                and model.get("supported_in_api") is True
            )
        else:
            supported = (
                model.get("supports_text") is True
                and model.get("available") is True
            )
        if isinstance(name, str) and name and supported:
            names.add(name)
    return sorted(names)


def classify_probe_failure(
    http_status: int | None, error_code: str | None, message: str | None
) -> str:
    """Classify a failed model probe without retaining its response body."""
    code = str(error_code or "").strip().lower().replace("-", "_")
    text = str(message or "").casefold()

    if code == "server_overloaded" or "selected model is at capacity" in text:
        return "capacity"
    if (
        http_status == 429
        or "rate_limit" in code
        or "quota" in code
        or "usage limit" in text
        or "quota" in text
    ):
        return "quota"
    if (
        http_status in {401, 403}
        or code in {"token_expired", "invalid_token", "unauthorized", "forbidden"}
        or "unauthorized" in text
    ):
        return "auth"
    if (
        http_status == 404
        or code in {"model_not_found", "unsupported_model", "model_unsupported"}
        or "model not found" in text
    ):
        return "unsupported"
    return "probe_error"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _model_record(record: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}

    cleaned: dict[str, Any] = {}
    status = record.get("status")
    if isinstance(status, str) and status in MODEL_STATUSES:
        cleaned["status"] = status
    available = record.get("available")
    if available is True or available is False or (available is None and "available" in record):
        cleaned["available"] = available
    for field in ("last_probe_at", "last_probe_error_at", "cooldown_until"):
        if _is_number(record.get(field)):
            cleaned[field] = record[field]
    if isinstance(record.get("http_status"), int) and not isinstance(record["http_status"], bool):
        cleaned["http_status"] = record["http_status"]
    if isinstance(record.get("error_code"), str):
        cleaned["error_code"] = record["error_code"]
    if isinstance(record.get("error_message"), str):
        cleaned["error_message"] = record["error_message"][:500]
    return cleaned


def _account_record(record: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}

    cleaned: dict[str, Any] = {}
    available = record.get("available")
    if available is True or available is False or (available is None and "available" in record):
        cleaned["available"] = available
    if isinstance(record.get("discovery_source"), str):
        cleaned["discovery_source"] = record["discovery_source"]
    for field in ("last_probe_at", "next_probe_at"):
        if _is_number(record.get(field)):
            cleaned[field] = record[field]
    models = record.get("models")
    if isinstance(models, dict):
        cleaned["models"] = {
            name: _model_record(model)
            for name, model in models.items()
            if isinstance(name, str) and name and isinstance(model, dict)
        }
    return cleaned


def _state_record(state: dict[str, Any] | None) -> dict[str, Any]:
    state = state if isinstance(state, dict) else {}
    cleaned: dict[str, Any] = {"version": 1, "accounts": {}}
    if _is_number(state.get("updated_at")):
        cleaned["updated_at"] = state["updated_at"]
    accounts = state.get("accounts")
    if isinstance(accounts, dict):
        cleaned["accounts"] = {
            alias: _account_record(record)
            for alias, record in accounts.items()
            if isinstance(alias, str) and alias and isinstance(record, dict)
        }
    return cleaned


def merge_model_probe(
    previous: dict[str, Any] | None,
    result: dict[str, Any],
    now: float,
    cooldown_seconds: int,
) -> dict[str, Any]:
    """Merge a probe result while retaining a recent known-good result on errors."""
    status = result.get("status") if isinstance(result, dict) else None
    if status not in MODEL_STATUSES:
        status = "probe_error"

    prior = _model_record(previous)
    if status == "probe_error":
        last_success = prior.get("last_probe_at")
        if (
            prior.get("status") == "available"
            and prior.get("available") is True
            and _is_number(last_success)
            and now - last_success <= cooldown_seconds
        ):
            retained = dict(prior)
            retained["last_probe_error_at"] = now
            return retained

    merged = _model_record(result)
    merged["status"] = status
    merged["last_probe_at"] = now
    if status == "available":
        merged["available"] = True
        merged.pop("cooldown_until", None)
    elif status in BLOCKING_STATUSES or status == "unsupported":
        merged["available"] = False
        if status == "capacity":
            merged["cooldown_until"] = now + cooldown_seconds
        else:
            merged.pop("cooldown_until", None)
    else:
        merged["available"] = None
        merged["last_probe_error_at"] = now
    return merged


def account_probe_available(record: dict[str, Any] | None, now: float) -> bool | None:
    """Return usable, blocked, or unknown for an account's supported models."""
    models = record.get("models") if isinstance(record, dict) else None
    if not isinstance(models, dict):
        return None

    supported = [
        model
        for model in models.values()
        if isinstance(model, dict) and model.get("status") != "unsupported"
    ]
    if not supported:
        return None
    if any(model.get("available") is True for model in supported):
        return True

    def is_blocked(model: dict[str, Any]) -> bool:
        status = model.get("status")
        if status in BLOCKING_STATUSES and status != "capacity":
            return True
        if status != "capacity":
            return False
        cooldown_until = model.get("cooldown_until")
        return not _is_number(cooldown_until) or cooldown_until > now

    return False if all(is_blocked(model) for model in supported) else None


class ModelHealthStore:
    """Atomically persist the documented, non-secret model-health state."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "accounts": {}}
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"invalid model health state: {self.path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"invalid model health state: {self.path}")
        return _state_record(payload)

    def save(self, state: dict[str, Any]) -> None:
        payload = _state_record(state)
        payload["updated_at"] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.chmod(temporary_name, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
            os.chmod(self.path, 0o600)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def account(self, alias: str) -> dict[str, Any] | None:
        record = self.load()["accounts"].get(alias)
        return record if isinstance(record, dict) else None

    def replace_account(self, alias: str, record: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(alias, str) or not alias:
            raise ValueError("account alias must be a non-empty string")
        if not isinstance(record, dict):
            raise ValueError("account record must be a dictionary")
        state = self.load()
        cleaned = _account_record(record)
        state["accounts"][alias] = cleaned
        self.save(state)
        return cleaned
