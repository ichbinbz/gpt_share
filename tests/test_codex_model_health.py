import json
import os
import stat

import pytest

from scripts.codex_model_health import (
    ModelHealthStore,
    account_probe_available,
    classify_probe_failure,
    filter_codex_text_models,
    merge_model_probe,
)


def test_filters_only_available_codex_text_models():
    payload = {
        "models": [
            {"slug": "gpt-text", "supports_text": True, "available": True},
            {"slug": "gpt-image", "supports_text": False, "available": True},
            {"slug": "gpt-disabled", "supports_text": True, "available": False},
        ]
    }

    assert filter_codex_text_models(payload) == ["gpt-text"]


@pytest.mark.parametrize(
    ("status", "code", "message", "expected"),
    [
        (
            503,
            "server_overloaded",
            "Selected model is at capacity. Please try a different model.",
            "capacity",
        ),
        (429, "rate_limit_exceeded", "usage limit reached", "quota"),
        (401, "token_expired", "unauthorized", "auth"),
        (404, "model_not_found", "model not found", "unsupported"),
        (None, None, "connection reset", "probe_error"),
    ],
)
def test_classifies_probe_failures(status, code, message, expected):
    assert classify_probe_failure(status, code, message) == expected


def test_transient_probe_error_preserves_recent_success(tmp_path):
    now = 2_000.0
    previous = {"status": "available", "available": True, "last_probe_at": 1_900.0}

    merged = merge_model_probe(previous, {"status": "probe_error", "available": None}, now, 1800)

    assert merged["status"] == "available"
    assert merged["last_probe_error_at"] == now


def test_account_is_blocked_only_when_every_supported_model_is_blocked():
    record = {
        "models": {
            "one": {"status": "capacity", "available": False, "cooldown_until": 3000},
            "two": {"status": "available", "available": True, "last_probe_at": 2000},
        }
    }

    assert account_probe_available(record, 2100) is True

    record["models"]["two"] = {"status": "auth", "available": False}
    assert account_probe_available(record, 2100) is False


def test_store_filters_secret_fields_and_truncates_saved_error_message(tmp_path):
    path = tmp_path / "model-health.json"
    store = ModelHealthStore(path)

    store.replace_account(
        "chatgpt024",
        {
            "available": False,
            "discovery_source": "remote",
            "access_token": "must-not-persist",
            "models": {
                "gpt-text": {
                    "status": "capacity",
                    "available": False,
                    "last_probe_at": 2000,
                    "error_message": "x" * 501,
                    "probe_response": {"secret": "must-not-persist"},
                }
            },
        },
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    model = saved["accounts"]["chatgpt024"]["models"]["gpt-text"]
    assert store.account("chatgpt024") == saved["accounts"]["chatgpt024"]
    assert model["error_message"] == "x" * 500
    assert "access_token" not in json.dumps(saved)
    assert "probe_response" not in json.dumps(saved)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
