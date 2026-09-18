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


def test_filters_data_id_shape_and_returns_sorted_deduplicated_names():
    payload = {
        "data": [
            {"id": "gpt-z", "supports_text": True, "available": True},
            {"id": "gpt-a", "supports_text": True, "available": True},
            {"id": "gpt-z", "supports_text": True, "available": True},
            {"id": "gpt-image", "supports_text": False, "available": True},
            {"id": "gpt-disabled", "supports_text": True, "available": False},
        ]
    }

    assert filter_codex_text_models(payload) == ["gpt-a", "gpt-z"]


def test_filters_current_codex_catalog_visibility_shape():
    payload = {
        "models": [
            {"slug": "gpt-6-astra", "visibility": "list", "supported_in_api": True},
            {"slug": "gpt-5.6-sol", "visibility": "list", "supported_in_api": True},
            {
                "slug": "gpt-reserve",
                "visibility": "hide",
                "supported_in_api": True,
                "supports_text": True,
                "available": True,
            },
            {"slug": "gpt-disabled", "visibility": "list", "supported_in_api": False},
        ]
    }

    assert filter_codex_text_models(payload) == ["gpt-5.6-sol", "gpt-6-astra"]


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


@pytest.mark.parametrize(
    ("status", "code", "message", "expected"),
    [
        (429, "server_overloaded", "usage limit reached", "capacity"),
        (401, "rate_limit_exceeded", "unauthorized", "quota"),
        (404, "token_expired", "model not found", "auth"),
    ],
)
def test_classifier_uses_documented_precedence_for_conflicting_signals(
    status, code, message, expected
):
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


@pytest.mark.parametrize(
    ("record", "now"),
    [
        ({"models": {"image-only": {"status": "unsupported", "available": False}}}, 2100),
        ({"models": {"one": {"status": "probe_error", "available": None}}}, 2100),
        (
            {
                "models": {
                    "one": {
                        "status": "capacity",
                        "available": False,
                        "cooldown_until": 2000,
                    }
                }
            },
            2100,
        ),
    ],
)
def test_account_probe_availability_is_unknown_when_not_fully_blocked(record, now):
    assert account_probe_available(record, now) is None


def test_store_filters_secret_fields_and_truncates_saved_error_message(tmp_path):
    path = tmp_path / "model-health.json"
    store = ModelHealthStore(path)

    store.replace_account(
        "chatgpt024",
        {
            "available": False,
            "discovery_source": "remote",
            "access_token": "must-not-persist",
            "refresh_token": "refresh-token-must-not-persist",
            "admin_token": "admin-token-must-not-persist",
            "device_token": "device-token-must-not-persist",
            "prompt": "probe-prompt-must-not-persist",
            "response_body": {"body": "response-body-must-not-persist"},
            "models": {
                "gpt-text": {
                    "status": "capacity",
                    "available": False,
                    "last_probe_at": 2000,
                    "error_message": "x" * 501,
                    "probe_response": {"secret": "must-not-persist"},
                    "response_body": {"body": "model-response-body-must-not-persist"},
                }
            },
        },
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    model = saved["accounts"]["chatgpt024"]["models"]["gpt-text"]
    assert store.account("chatgpt024") == saved["accounts"]["chatgpt024"]
    assert model["error_message"] == "x" * 500
    serialized = json.dumps(saved)
    for prohibited in (
        "access_token",
        "refresh_token",
        "admin_token",
        "device_token",
        "prompt",
        "response_body",
        "probe_response",
        "must-not-persist",
    ):
        assert prohibited not in serialized
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
