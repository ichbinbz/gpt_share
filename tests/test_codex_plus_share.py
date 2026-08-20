import base64
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.codex_plus_broker import (
    DailySessionUsage,
    DeviceIdentity,
    DeviceUsageStore,
    SessionTokenUsage,
    TokenBroker,
    account_selection_key,
    normalize_ip,
    token_account_id,
    token_expiry,
    token_plan_type,
    token_email,
    usage_score,
)
from scripts.codex_plus_sync import DEFAULT_BROKER_URL, DEFAULT_PROXY_URL, codex_auth_payload
from scripts.codex_plus_sync import request_lease


def jwt(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"e30.{encoded}.sig"


def test_decodes_official_chatgpt_claims():
    token = jwt(
        {
            "exp": 2_000_000_000,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct-test",
                "chatgpt_plan_type": "plus",
            },
        }
    )
    assert token_expiry(token) == 2_000_000_000
    assert token_account_id(token) == "acct-test"
    assert token_plan_type(token) == "plus"


def test_reads_real_account_email_from_server_owned_token_claims():
    id_token = jwt({"email": "employee@example.com"})
    assert token_email(id_token, None) == "employee@example.com"
    assert token_email("invalid", jwt({"preferred_username": "fallback@example.com"})) == "fallback@example.com"


def test_client_auth_uses_official_external_chatgpt_mode_without_refresh_token():
    token = jwt({"exp": 2_000_000_000})
    payload = codex_auth_payload(token, "acct-test")
    assert payload["auth_mode"] == "chatgptAuthTokens"
    assert payload["OPENAI_API_KEY"] is None
    assert payload["tokens"]["access_token"] == token
    assert payload["tokens"]["refresh_token"] == ""
    assert payload["tokens"]["account_id"] == "acct-test"


def test_usage_score_uses_most_constrained_window():
    assert usage_score({"rate_limit": {"primary_window": {"used_percent": 20}, "secondary_window": {"used_percent": 75}}}) == 75
    assert usage_score(None) == 50


def test_account_selection_prefers_large_allowance_that_resets_sooner():
    fast_reset = {
        "rate_limit": {
            "primary_window": {"used_percent": 20, "reset_after_seconds": 3600},
            "secondary_window": {"used_percent": 10, "reset_after_seconds": 604800},
        }
    }
    slow_reset = {
        "rate_limit": {
            "primary_window": {"used_percent": 10, "reset_after_seconds": 36000},
            "secondary_window": {"used_percent": 10, "reset_after_seconds": 604800},
        }
    }
    assert account_selection_key(fast_reset, 0, now=1_000_000) < account_selection_key(
        slow_reset, 0, now=1_000_000
    )


def test_account_selection_avoids_nearly_depleted_account_even_if_reset_is_close():
    nearly_empty = {
        "rate_limit": {
            "primary_window": {"used_percent": 95, "reset_after_seconds": 60},
            "secondary_window": {"used_percent": 95, "reset_after_seconds": 60},
        }
    }
    healthy = {
        "rate_limit": {
            "primary_window": {"used_percent": 50, "reset_after_seconds": 86400},
            "secondary_window": {"used_percent": 50, "reset_after_seconds": 86400},
        }
    }
    assert account_selection_key(healthy, 0) < account_selection_key(nearly_empty, 0)


def test_account_selection_penalizes_active_leases_for_equal_allowance():
    usage = {
        "rate_limit": {
            "primary_window": {"used_percent": 25, "reset_after_seconds": 7200},
            "secondary_window": {"used_percent": 20, "reset_after_seconds": 604800},
        }
    }
    assert account_selection_key(usage, 0) < account_selection_key(usage, 2)


def test_sync_source_never_mentions_server_refresh_field_in_output(tmp_path: Path):
    payload = codex_auth_payload(jwt({"exp": 2_000_000_000}), "acct-test")
    rendered = json.dumps(payload)
    assert '"refresh_token": ""' in rendered
    assert "server_refresh_token" not in rendered


def test_request_lease_uses_explicit_http_proxy(monkeypatch):
    opened = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"lease_id":"lease-test"}'

    class Opener:
        def open(self, request, timeout):
            opened["url"] = request.full_url
            opened["timeout"] = timeout
            return Response()

    def fake_build_opener(handler):
        opened["proxies"] = handler.proxies
        return Opener()

    monkeypatch.setattr("urllib.request.build_opener", fake_build_opener)
    result = request_lease(
        "http://codex.cws.internal:8765",
        "device-token",
        "device-1",
        None,
        "http://127.0.0.1:10809",
    )
    assert result["lease_id"] == "lease-test"
    assert opened["proxies"]["http"] == "http://127.0.0.1:10809"
    assert opened["url"] == "http://codex.cws.internal:8765/v1/lease"


def test_employee_network_defaults():
    assert DEFAULT_BROKER_URL == "http://codex.cws.internal:8765"
    assert DEFAULT_PROXY_URL == "http://192.168.2.38:7897"


def test_quota_dashboard_keeps_admin_token_in_tab_session_only():
    dashboard = (Path(__file__).parents[1] / "scripts" / "codex_quota_dashboard.html").read_text(encoding="utf-8")
    assert "/v1/admin/accounts" in dashboard
    assert "sessionStorage" in dashboard
    assert "localStorage" not in dashboard
    assert "access_token" not in dashboard
    assert "account.email" in dashboard


def test_empty_account_status_can_count_active_leases():
    broker = TokenBroker.__new__(TokenBroker)
    broker.accounts = {}
    broker._load_state = lambda: {"leases": {}}
    assert asyncio.run(broker.account_status(refresh_usage=True)) == []


def test_device_usage_is_attributed_by_token_and_idempotent(tmp_path: Path):
    store = DeviceUsageStore(tmp_path / "usage.json")
    employee = DeviceIdentity(token_id="device-token-id-1", label="employee-zhang")
    first = SessionTokenUsage(
        session_id="session-hash",
        input_tokens=100,
        cached_input_tokens=40,
        output_tokens=20,
        reasoning_output_tokens=5,
        total_tokens=120,
    )
    asyncio.run(store.record(employee, [first]))
    asyncio.run(store.record(employee, [first]))
    summary = asyncio.run(store.summary({employee.token_id}))
    assert len(summary) == 1
    assert summary[0]["device_token_id"] == employee.token_id
    assert summary[0]["device_label"] == employee.label
    assert summary[0]["input_tokens"] == 100
    assert summary[0]["total_tokens"] == 120
    assert summary[0]["report_count"] == 2

    larger = SessionTokenUsage(
        session_id="session-hash",
        input_tokens=160,
        cached_input_tokens=60,
        output_tokens=30,
        reasoning_output_tokens=8,
        total_tokens=190,
    )
    asyncio.run(store.record(employee, [larger]))
    updated = asyncio.run(store.summary({employee.token_id}))[0]
    assert updated["total_tokens"] == 190
    assert updated["session_count"] == 1


def test_device_usage_accepts_legacy_reports_without_new_fields(tmp_path: Path):
    store = DeviceUsageStore(tmp_path / "usage.json")
    employee = DeviceIdentity(token_id="legacy-device", label="legacy")
    legacy = SessionTokenUsage(session_id="old-client", input_tokens=10, total_tokens=10)
    asyncio.run(store.record(employee, [legacy]))
    summary = asyncio.run(store.summary({employee.token_id}))[0]
    assert summary["total_tokens"] == 10
    assert summary["user_message_count"] == 0
    assert summary["weekly"]["total_tokens"] == 0
    assert summary["monthly"]["total_tokens"] == 0


def test_device_usage_summarizes_daily_week_month_and_user_input(tmp_path: Path):
    store = DeviceUsageStore(tmp_path / "usage.json")
    employee = DeviceIdentity(token_id="daily-device", label="daily")
    today = datetime.now(timezone(timedelta(hours=8))).date()
    old_date = today - timedelta(days=40)
    usage = SessionTokenUsage(
        session_id="daily-session",
        input_tokens=130,
        output_tokens=20,
        total_tokens=150,
        user_message_count=3,
        user_text_characters=24,
        user_text_tokens_estimated=12,
        daily_usage=[
            DailySessionUsage(
                date=today.isoformat(),
                input_tokens=100,
                output_tokens=20,
                total_tokens=120,
                user_message_count=2,
                user_text_characters=20,
                user_text_tokens_estimated=10,
            ),
            DailySessionUsage(
                date=old_date.isoformat(),
                input_tokens=30,
                total_tokens=30,
                user_message_count=1,
                user_text_characters=4,
                user_text_tokens_estimated=2,
            ),
        ],
    )
    asyncio.run(store.record(employee, [usage]))
    asyncio.run(store.record(employee, [usage]))
    summary = asyncio.run(store.summary({employee.token_id}))[0]
    assert summary["total_tokens"] == 150
    assert summary["user_message_count"] == 3
    assert summary["weekly"]["total_tokens"] == 120
    assert summary["monthly"]["user_text_tokens_estimated"] == 10


def test_device_usage_history_is_marked_revoked_when_token_is_disabled(tmp_path: Path):
    store = DeviceUsageStore(tmp_path / "usage.json")
    employee = DeviceIdentity(token_id="device-token-id-2", label="employee-li")
    asyncio.run(store.record(employee, [SessionTokenUsage(session_id="session-hash", total_tokens=25)]))
    assert asyncio.run(store.summary(set()))[0]["enabled"] is False


def test_usage_state_never_contains_raw_employee_token(tmp_path: Path):
    store = DeviceUsageStore(tmp_path / "usage.json")
    raw_token = "cws-device-secret-that-must-not-be-stored"
    employee = DeviceIdentity(token_id="opaque-id", label="employee-wang")
    asyncio.run(store.record(employee, [SessionTokenUsage(session_id="session-hash", total_tokens=1)]))
    assert raw_token not in (tmp_path / "usage.json").read_text(encoding="utf-8")


def test_device_activity_records_client_and_proxy_ip_history(tmp_path: Path):
    store = DeviceUsageStore(tmp_path / "usage.json")
    employee = DeviceIdentity(
        token_id="device-token-id-ip",
        label="P.HU-00465",
        employee_name="胡鹏",
        employee_id="00465",
    )
    asyncio.run(store.record_lease(employee, "192.168.2.51", "127.0.0.1"))
    asyncio.run(store.record_lease(employee, "192.168.2.52", "127.0.0.1"))
    summary = asyncio.run(store.summary({employee.token_id}))[0]
    assert summary["employee_name"] == "胡鹏"
    assert summary["employee_id"] == "00465"
    assert summary["client_ip"] == "192.168.2.52"
    assert summary["source_ip"] == "127.0.0.1"
    assert summary["lease_count"] == 2
    assert [item["ip"] for item in summary["client_ip_history"]] == [
        "192.168.2.52",
        "192.168.2.51",
    ]


def test_ip_normalization_accepts_ipv4_and_rejects_invalid_values():
    assert normalize_ip(" 192.168.2.51 ") == "192.168.2.51"
    assert normalize_ip("") is None
    try:
        normalize_ip("not-an-ip")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 422
    else:
        raise AssertionError("invalid IP address was accepted")


def test_registered_employee_is_visible_before_first_client_connection(tmp_path: Path):
    store = DeviceUsageStore(tmp_path / "usage.json")
    rows = asyncio.run(
        store.summary(
            {
                "registered-only-id": {
                    "device_label": "P.HU-00465",
                    "employee_name": "胡鹏",
                    "employee_id": "00465",
                    "enabled": True,
                }
            }
        )
    )
    assert rows[0]["employee_name"] == "胡鹏"
    assert rows[0]["enabled"] is True
    assert rows[0]["session_count"] == 0
    assert rows[0]["client_ip"] is None
