import asyncio
import importlib.util
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "backend" / "api" / "sources" / "codex_broker_status.py"
SPEC = importlib.util.spec_from_file_location("codex_broker_status", SOURCE)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
get_codex_broker_status = MODULE.get_codex_broker_status
normalize_codex_account = MODULE.normalize_codex_account
normalize_codex_device_usage = MODULE.normalize_codex_device_usage


def test_normalizes_codex_quota_without_exposing_raw_usage():
    account = normalize_codex_account(
        {
            "alias": "account-03",
            "email": "employee@example.com",
            "available": True,
            "plan_type": "pro",
            "usage_score": 72,
            "active_leases": 2,
            "usage": {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 72,
                        "limit_window_seconds": 18_000,
                        "reset_at": 2_000_000_000,
                    },
                    "secondary_window": {
                        "used_percent": 25,
                        "limit_window_seconds": 604_800,
                        "reset_after_seconds": 3600,
                    },
                },
                "private_field": "must-not-leak",
            },
        }
    )

    assert account["alias"] == "account-03"
    assert account["email"] == "employee@example.com"
    assert account["active_leases"] == 2
    assert account["windows"][0]["remaining_percent"] == 28
    assert account["windows"][1]["remaining_percent"] == 75
    assert "usage" not in account
    assert "private_field" not in account


def test_missing_admin_token_returns_safe_configuration_status(monkeypatch):
    monkeypatch.delenv("CWS_CODEX_ADMIN_TOKEN", raising=False)
    status = asyncio.run(get_codex_broker_status())
    assert status["configured"] is False
    assert status["reachable"] is False
    assert status["accounts"] == []


def test_normalizes_employee_user_input_and_period_usage():
    device = normalize_codex_device_usage(
        {
            "device_token_id": "device-1",
            "user_message_count": 5,
            "user_text_characters": 40,
            "user_text_tokens_estimated": 12,
            "weekly": {"total_tokens": 100, "user_message_count": 2},
            "monthly": {"total_tokens": 300, "user_message_count": 4},
            "week_start_date": "2026-08-17",
            "month_start_date": "2026-08-01",
        }
    )
    assert device is not None
    assert device["user_message_count"] == 5
    assert device["weekly"]["total_tokens"] == 100
    assert device["weekly"]["user_message_count"] == 2
    assert device["monthly"]["total_tokens"] == 300
    assert device["week_start_date"] == "2026-08-17"
