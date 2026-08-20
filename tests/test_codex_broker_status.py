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
