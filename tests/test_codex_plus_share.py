import base64
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from fastapi.testclient import TestClient
import scripts.codex_plus_broker as broker_module
from scripts.codex_plus_broker import (
    AccountRecord,
    BrokerSettings,
    DailySessionUsage,
    DeviceIdentity,
    DeviceUsageStore,
    SessionTokenUsage,
    TokenBroker,
    account_selection_key,
    create_portal_user,
    derive_user_device_token,
    find_portal_user,
    normalize_ip,
    token_account_id,
    token_expiry,
    token_plan_type,
    token_email,
    usage_score,
    verify_user_password,
)
from scripts.codex_model_health import ModelHealthStore
from scripts.codex_plus_sync import DEFAULT_BROKER_URL, DEFAULT_PROXY_URL, codex_auth_payload
from scripts.codex_plus_sync import request_lease


def jwt(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"e30.{encoded}.sig"


def probe_auth_payload(access_token: str | None = None) -> dict:
    return {
        "tokens": {
            "access_token": access_token or jwt({"exp": 2_100_000_000}),
            "refresh_token": "refresh-test",
            "account_id": "acct-test",
        }
    }


def probe_account(tmp_path: Path, payload: dict | None = None) -> AccountRecord:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps(payload or probe_auth_payload()), encoding="utf-8")
    return AccountRecord("account-test", auth_path)


def client_release_manifest(asset_name: str, payload: bytes) -> dict:
    return {
        "release_version": "0.1.7",
        "published_at": "2026-09-18T00:00:00Z",
        "assets": [
            {
                "platform": "windows-installer",
                "name": asset_name,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "download_url": f"/v1/client/releases/download/{asset_name}",
            }
        ],
    }


def configure_client_release(tmp_path: Path, monkeypatch, payload: bytes = b"installer"):
    asset_name = "CWS-Codex-Setup-v0.1.7.exe"
    (tmp_path / asset_name).write_bytes(payload)
    manifest = client_release_manifest(asset_name, payload)
    (tmp_path / "latest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        broker_module,
        "settings",
        SimpleNamespace(
            release_dir=tmp_path,
            device_tokens=("device-token",),
            device_token_file=tmp_path / "device-tokens.json",
        ),
    )
    return asset_name, manifest


async def render_release_response(response, send_override=None) -> bytes:
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)
        if send_override is not None:
            await send_override(message)

    await response(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/client/releases/download/test",
            "headers": [],
            "asgi": {"spec_version": "2.4"},
        },
        receive,
        send,
    )
    return b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )


def test_client_release_routes_require_device_authentication(tmp_path: Path, monkeypatch):
    asset_name, _manifest = configure_client_release(tmp_path, monkeypatch)
    client = TestClient(broker_module.app)

    assert client.get("/v1/client/releases/latest").status_code == 401
    assert client.get(f"/v1/client/releases/download/{asset_name}").status_code == 401


def test_client_release_latest_returns_revalidated_manifest(tmp_path: Path, monkeypatch):
    _asset_name, manifest = configure_client_release(tmp_path, monkeypatch)

    response = TestClient(broker_module.app).get(
        "/v1/client/releases/latest",
        headers={"Authorization": "Bearer device-token"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == manifest
    assert response.headers["cache-control"] == "private, no-cache"


@pytest.mark.parametrize(
    "filename",
    [
        "auth.json",
        "CWS-Codex-Release-v0.1.7.zip",
        "..%5Cauth.json",
        "%2e%2e%2fauth.json",
    ],
)
def test_client_release_download_rejects_traversal_and_unlisted_files(
    tmp_path: Path, monkeypatch, filename: str
):
    configure_client_release(tmp_path, monkeypatch)
    (tmp_path / "auth.json").write_text("secret", encoding="utf-8")

    response = TestClient(broker_module.app).get(
        f"/v1/client/releases/download/{filename}",
        headers={"Authorization": "Bearer device-token"},
    )

    assert response.status_code == 404
    assert b"secret" not in response.content


@pytest.mark.parametrize("tamper", ["size", "sha256", "metadata", "unexpected"])
def test_client_release_rejects_tampered_manifest_or_asset(
    tmp_path: Path, monkeypatch, tamper: str
):
    asset_name, manifest = configure_client_release(tmp_path, monkeypatch)
    if tamper == "size":
        manifest["assets"][0]["size"] += 1
    elif tamper == "sha256":
        manifest["assets"][0]["sha256"] = "0" * 64
    elif tamper == "metadata":
        manifest["assets"][0]["download_url"] = "/v1/client/releases/download/auth.json"
    else:
        manifest["private"] = "must-not-be-reflected"
    (tmp_path / "latest.json").write_text(json.dumps(manifest), encoding="utf-8")

    client = TestClient(broker_module.app)
    latest = client.get(
        "/v1/client/releases/latest",
        headers={"Authorization": "Bearer device-token"},
    )
    download = client.get(
        f"/v1/client/releases/download/{asset_name}",
        headers={"Authorization": "Bearer device-token"},
    )

    assert latest.status_code == 503
    assert download.status_code == 503
    assert b"installer" not in download.content
    assert b"must-not-be-reflected" not in latest.content


def test_client_release_missing_manifest_or_asset_returns_404(tmp_path: Path, monkeypatch):
    asset_name, _manifest = configure_client_release(tmp_path, monkeypatch)
    client = TestClient(broker_module.app)
    (tmp_path / asset_name).unlink()

    assert client.get(
        "/v1/client/releases/latest",
        headers={"Authorization": "Bearer device-token"},
    ).status_code == 404
    assert client.get(
        f"/v1/client/releases/download/{asset_name}",
        headers={"Authorization": "Bearer device-token"},
    ).status_code == 404


def test_client_release_download_returns_validated_bytes_and_fixed_headers(
    tmp_path: Path, monkeypatch
):
    asset_name, _manifest = configure_client_release(tmp_path, monkeypatch)

    response = TestClient(broker_module.app).get(
        f"/v1/client/releases/download/{asset_name}",
        headers={"Authorization": "Bearer device-token"},
    )

    assert response.status_code == 200, response.text
    assert response.content == b"installer"
    assert response.headers["content-type"] == "application/vnd.microsoft.portable-executable"
    assert response.headers["content-length"] == "9"
    assert response.headers["content-disposition"] == (
        'attachment; filename="CWS-Codex-Setup-v0.1.7.exe"'
    )
    assert response.headers["cache-control"] == "private"


def test_client_release_download_streams_the_descriptor_that_was_validated(
    tmp_path: Path, monkeypatch
):
    asset_name, _manifest = configure_client_release(tmp_path, monkeypatch)
    asset_path = tmp_path / asset_name
    replacement_path = tmp_path / "replacement.tmp"
    replacement_path.write_bytes(b"attacker-controlled-replacement")

    async def request_then_swap() -> bytes:
        response = await broker_module.download_client_release(
            asset_name,
            DeviceIdentity(token_id="test", label="test"),
        )
        try:
            os.replace(replacement_path, asset_path)
        except PermissionError:
            # Windows prevents replacement while the validated descriptor is open.
            # POSIX permits replacement, but the open descriptor must remain bound
            # to the validated inode in either case.
            pass
        return await render_release_response(response)

    assert asyncio.run(request_then_swap()) == b"installer"


def test_client_release_download_streams_verified_snapshot_after_in_place_source_write(
    tmp_path: Path, monkeypatch
):
    asset_name, _manifest = configure_client_release(tmp_path, monkeypatch)
    asset_path = tmp_path / asset_name

    async def request_then_modify() -> bytes:
        response = await broker_module.download_client_release(
            asset_name,
            DeviceIdentity(token_id="test", label="test"),
        )
        asset_path.write_bytes(b"attacker!")
        return await render_release_response(response)

    assert asyncio.run(request_then_modify()) == b"installer"


@pytest.mark.parametrize("failure", ["send", "cancel"])
def test_client_release_snapshot_closes_on_stream_failure(
    tmp_path: Path, monkeypatch, failure: str
):
    asset_name, _manifest = configure_client_release(tmp_path, monkeypatch)
    real_temporary_file = broker_module.tempfile.TemporaryFile
    snapshots = []

    def tracked_temporary_file(*args, **kwargs):
        snapshot = real_temporary_file(*args, **kwargs)
        snapshots.append(snapshot)
        return snapshot

    monkeypatch.setattr(broker_module.tempfile, "TemporaryFile", tracked_temporary_file)

    async def request_then_fail() -> None:
        response = await broker_module.download_client_release(
            asset_name,
            DeviceIdentity(token_id="test", label="test"),
        )
        assert snapshots and snapshots[0].closed is False

        async def fail_on_body(message):
            if message["type"] != "http.response.body":
                return
            if failure == "cancel":
                raise asyncio.CancelledError
            raise RuntimeError("simulated send failure")

        expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
        with pytest.raises(expected):
            await render_release_response(response, fail_on_body)

    asyncio.run(request_then_fail())
    assert len(snapshots) == 1
    assert snapshots[0].closed is True


@pytest.mark.skipif(os.name != "nt", reason="Windows handle semantics")
def test_windows_release_open_rejects_reparse_handle_before_fd_conversion(
    tmp_path: Path, monkeypatch
):
    closed = []
    converted = []
    monkeypatch.setattr(
        broker_module,
        "_windows_create_file_handle",
        lambda _path: 123,
        raising=False,
    )
    monkeypatch.setattr(
        broker_module,
        "_windows_file_attributes",
        lambda _handle: 0x400,
        raising=False,
    )
    monkeypatch.setattr(
        broker_module,
        "_windows_handle_to_fd",
        lambda handle: converted.append(handle),
        raising=False,
    )
    monkeypatch.setattr(
        broker_module,
        "_windows_close_handle",
        lambda handle: closed.append(handle),
        raising=False,
    )

    with pytest.raises(OSError, match="reparse"):
        broker_module._open_windows_release_asset(tmp_path / "asset")

    assert converted == []
    assert closed == [123]


def test_client_release_download_rejects_same_directory_symlink(
    tmp_path: Path, monkeypatch
):
    asset_name, _manifest = configure_client_release(tmp_path, monkeypatch)
    asset_path = tmp_path / asset_name
    target = tmp_path / "unlisted-release-payload"
    target.write_bytes(asset_path.read_bytes())
    asset_path.unlink()
    try:
        asset_path.symlink_to(target.name)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    response = TestClient(broker_module.app).get(
        f"/v1/client/releases/download/{asset_name}",
        headers={"Authorization": "Bearer device-token"},
    )

    assert response.status_code == 503
    assert b"installer" not in response.content


def test_broker_imports_with_side_by_side_health_module_like_server_install():
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        entry
        for entry in env.get("PYTHONPATH", "").split(os.pathsep)
        if entry and Path(entry).resolve() != root
    )
    result = subprocess.run(
        [sys.executable, "-c", "import codex_plus_broker"],
        cwd=root / "scripts",
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_model_probe_settings_have_safe_defaults(monkeypatch):
    for name in (
        "CWS_CODEX_MODEL_PROBE_INTERVAL_SECONDS",
        "CWS_CODEX_MODEL_COOLDOWN_SECONDS",
        "CWS_CODEX_MODEL_PROBE_CONCURRENCY",
        "CWS_CODEX_MODEL_PROBE_TIMEOUT_SECONDS",
        "CWS_CODEX_MODEL_FALLBACKS",
        "CWS_CODEX_MODEL_HEALTH_FILE",
        "CWS_CODEX_MODELS_URL",
        "CWS_CODEX_RESPONSES_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = BrokerSettings.from_env()

    assert settings.model_probe_interval_seconds == 900
    assert settings.model_cooldown_seconds == 1800
    assert settings.model_probe_concurrency == 2
    assert settings.model_probe_timeout_seconds == 45
    assert settings.model_health_file == Path("/var/lib/cws-codex/model-health.json")
    assert settings.codex_models_url == "https://chatgpt.com/backend-api/codex/models"
    assert settings.codex_responses_url == "https://chatgpt.com/backend-api/codex/responses"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CWS_CODEX_MODEL_PROBE_INTERVAL_SECONDS", "59"),
        ("CWS_CODEX_MODEL_COOLDOWN_SECONDS", "59"),
        ("CWS_CODEX_MODEL_PROBE_CONCURRENCY", "0"),
        ("CWS_CODEX_MODEL_PROBE_CONCURRENCY", "9"),
        ("CWS_CODEX_MODEL_PROBE_TIMEOUT_SECONDS", "4"),
        ("CWS_CODEX_MODEL_PROBE_TIMEOUT_SECONDS", "121"),
    ],
)
def test_model_probe_settings_reject_out_of_range_values(monkeypatch, name: str, value: str):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        BrokerSettings.from_env()


@pytest.mark.parametrize("name", ["CWS_CODEX_MODELS_URL", "CWS_CODEX_RESPONSES_URL"])
def test_model_probe_settings_reject_insecure_remote_endpoints(monkeypatch, name: str):
    monkeypatch.setenv(name, "http://example.test/backend-api/codex")
    with pytest.raises(ValueError, match="HTTPS"):
        BrokerSettings.from_env()

    monkeypatch.setenv(name, "http://127.0.0.1:8080/test")
    assert getattr(
        BrokerSettings.from_env(),
        "codex_models_url" if name.endswith("MODELS_URL") else "codex_responses_url",
    ).startswith("http://127.0.0.1:8080/")


def test_discover_models_uses_oauth_headers_and_filters_codex_text_models(tmp_path: Path):
    account = probe_account(tmp_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/backend-api/codex/models")
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["chatgpt-account-id"] == "acct-test"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"slug": "gpt-text", "supports_text": True, "available": True},
                    {"slug": "gpt-image", "supports_text": False, "available": True},
                    {"slug": "gpt-off", "supports_text": True, "available": False},
                ]
            },
        )

    async def run() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await account.discover_models(client, probe_auth_payload(), timeout=5)

    assert asyncio.run(run()) == ["gpt-text"]


def test_probe_model_sends_fixed_minimal_non_user_request(tmp_path: Path):
    account = probe_account(tmp_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path.endswith("/backend-api/codex/responses")
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["chatgpt-account-id"] == "acct-test"
        assert body["model"] == "gpt-text"
        assert body["store"] is False
        assert "tools" not in body
        assert body["input"] == "Reply with OK."
        assert "company" not in request.content.decode().lower()
        return httpx.Response(200, json={"id": "resp-test", "status": "completed"})

    async def run() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await account.probe_model(client, probe_auth_payload(), "gpt-text", timeout=5)

    assert asyncio.run(run()) == {
        "status": "available",
        "available": True,
        "http_status": 200,
    }


def test_probe_model_classifies_json_server_overloaded(tmp_path: Path):
    account = probe_account(tmp_path)
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            503,
            json={
                "error": {
                    "code": "server_overloaded",
                    "message": "Selected model is at capacity. Please try a different model.",
                    "type": "server_error",
                    "param": None,
                }
            },
        )

    async def run() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await account.probe_model(client, probe_auth_payload(), "gpt-text", timeout=5)

    result = asyncio.run(run())
    assert result == {
        "status": "capacity",
        "available": False,
        "http_status": 503,
        "error_code": "server_overloaded",
        "error_message": "Selected model is at capacity. Please try a different model.",
    }
    assert attempts == 1
    assert "response_body" not in result


def test_probe_model_classifies_sse_error_event(tmp_path: Path):
    account = probe_account(tmp_path)
    event = {
        "type": "error",
        "error": {
            "code": "rate_limit_exceeded",
            "message": "Usage limit reached.",
            "type": "rate_limit_error",
            "param": None,
        },
    }

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=f"event: error\ndata: {json.dumps(event)}\n\n",
        )

    async def run() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await account.probe_model(client, probe_auth_payload(), "gpt-text", timeout=5)

    result = asyncio.run(run())
    assert result["status"] == "quota"
    assert result["available"] is False
    assert result["error_code"] == "rate_limit_exceeded"
    assert "response_body" not in result


def test_probe_model_refreshes_once_after_401_and_retries(tmp_path: Path):
    old_access = jwt({"exp": 2_100_000_000})
    new_access = jwt({"exp": 2_200_000_000})
    payload = probe_auth_payload(old_access)
    account = probe_account(tmp_path, payload)
    attempts = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": new_access, "refresh_token": "new-refresh"},
            )
        attempts.append(request.headers["authorization"])
        if len(attempts) == 1:
            return httpx.Response(
                401,
                json={"error": {"code": "token_expired", "message": "Unauthorized"}},
            )
        return httpx.Response(200, json={"id": "resp-test", "status": "completed"})

    async def run() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await account.probe_model(client, payload, "gpt-text", timeout=5)

    result = asyncio.run(run())
    assert result["status"] == "available"
    assert attempts == [f"Bearer {old_access}", f"Bearer {new_access}"]
    stored = json.loads(account.auth_path.read_text(encoding="utf-8"))
    assert stored["last_refresh_reason"] == "model_probe_unauthorized"


def test_discovery_refresh_updates_auth_snapshot_for_all_later_model_probes(tmp_path: Path):
    old_access = jwt({"exp": 2_100_000_000})
    new_access = jwt({"exp": 2_200_000_000})
    payload = probe_auth_payload(old_access)
    account = probe_account(tmp_path, payload)
    codex_authorizations = []
    refreshes = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refreshes
        if request.url.path == "/oauth/token":
            refreshes += 1
            return httpx.Response(
                200,
                json={"access_token": new_access, "refresh_token": "new-refresh"},
            )
        authorization = request.headers["authorization"]
        codex_authorizations.append(authorization)
        if request.url.path.endswith("/backend-api/codex/models"):
            if authorization == f"Bearer {old_access}":
                return httpx.Response(
                    401,
                    json={"error": {"code": "token_expired", "message": "Unauthorized"}},
                )
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"slug": "gpt-one", "supports_text": True, "available": True},
                        {"slug": "gpt-two", "supports_text": True, "available": True},
                    ]
                },
            )
        return httpx.Response(200, json={"id": "resp-test", "status": "completed"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            models = await account.discover_models(client, payload, timeout=5)
            results = [
                await account.probe_model(client, payload, model, timeout=5)
                for model in models
            ]
            return models, results

    models, results = asyncio.run(run())

    assert models == ["gpt-one", "gpt-two"]
    assert all(result["status"] == "available" for result in results)
    assert refreshes == 1
    assert payload["tokens"]["access_token"] == new_access
    assert codex_authorizations == [
        f"Bearer {old_access}",
        f"Bearer {new_access}",
        f"Bearer {new_access}",
        f"Bearer {new_access}",
    ]


def test_probe_model_timeout_is_a_transient_probe_error(tmp_path: Path):
    account = probe_account(tmp_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async def run() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await account.probe_model(client, probe_auth_payload(), "gpt-text", timeout=5)

    assert asyncio.run(run()) == {
        "status": "probe_error",
        "available": None,
        "error_code": "timeout",
        "error_message": "request timed out",
    }


def probe_broker(tmp_path: Path, accounts: dict, **setting_overrides) -> TokenBroker:
    broker = TokenBroker.__new__(TokenBroker)
    broker.client = object()
    broker.owns_client = False
    broker.accounts = accounts
    broker.model_health_store = ModelHealthStore(tmp_path / "model-health.json")
    broker.probe_lock = asyncio.Lock()
    defaults = {
        "refresh_window_seconds": 900,
        "model_probe_timeout_seconds": 5,
        "model_probe_concurrency": 2,
        "model_cooldown_seconds": 1800,
        "model_probe_interval_seconds": 900,
        "model_fallbacks": ("fallback-one", "fallback-two"),
    }
    defaults.update(setting_overrides)
    broker.settings = SimpleNamespace(**defaults)
    return broker


class FakeProbeAccount:
    def __init__(self, models=("gpt-text",), *, discovery_error=False, result=None):
        self.models = list(models)
        self.discovery_error = discovery_error
        self.result = result or {"status": "available", "available": True, "http_status": 200}
        self.probed_models = []
        self.active_counter = None

    async def ensure_fresh(self, _client, _refresh_window):
        if self.active_counter is not None:
            self.active_counter["active"] += 1
            self.active_counter["peak"] = max(
                self.active_counter["peak"], self.active_counter["active"]
            )
        return probe_auth_payload()

    async def discover_models(self, _client, _payload, _timeout):
        if self.discovery_error:
            raise RuntimeError("discovery unavailable")
        return self.models

    async def probe_model(self, _client, _payload, model, _timeout):
        self.probed_models.append(model)
        await asyncio.sleep(0.01)
        if self.active_counter is not None and len(self.probed_models) == len(self.models):
            self.active_counter["active"] -= 1
        return dict(self.result)


def test_probe_models_once_limits_seven_accounts_to_configured_concurrency(tmp_path: Path):
    counter = {"active": 0, "peak": 0}
    accounts = {f"account-{index}": FakeProbeAccount() for index in range(7)}
    for account in accounts.values():
        account.active_counter = counter
    broker = probe_broker(tmp_path, accounts, model_probe_concurrency=2)

    state = asyncio.run(broker.probe_models_once(now=2_000.0))

    assert counter["peak"] == 2
    assert len(state["accounts"]) == 7
    assert all(record["available"] is True for record in state["accounts"].values())


def test_probe_models_once_uses_all_fallback_models_when_discovery_fails(tmp_path: Path):
    account = FakeProbeAccount(discovery_error=True)
    account.models = ["fallback-one", "fallback-two"]
    broker = probe_broker(tmp_path, {"account-test": account})

    state = asyncio.run(broker.probe_models_once(now=2_000.0))

    record = state["accounts"]["account-test"]
    assert account.probed_models == ["fallback-one", "fallback-two"]
    assert record["discovery_source"] == "fallback"
    assert sorted(record["models"]) == ["fallback-one", "fallback-two"]


def test_probe_models_once_preserves_recent_known_good_on_transient_error(tmp_path: Path):
    store = ModelHealthStore(tmp_path / "model-health.json")
    store.replace_account(
        "account-test",
        {
            "available": True,
            "models": {
                "gpt-text": {
                    "status": "available",
                    "available": True,
                    "last_probe_at": 1_900.0,
                }
            },
        },
    )
    account = FakeProbeAccount(
        result={
            "status": "probe_error",
            "available": None,
            "error_code": "timeout",
            "error_message": "request timed out",
        }
    )
    broker = probe_broker(tmp_path, {"account-test": account})

    state = asyncio.run(broker.probe_models_once(now=2_000.0))

    model = state["accounts"]["account-test"]["models"]["gpt-text"]
    assert model["status"] == "available"
    assert model["available"] is True
    assert model["last_probe_error_at"] == 2_000.0


def test_probe_models_once_marks_failed_oauth_refresh_auth_over_prior_healthy_state(
    tmp_path: Path,
):
    old_access = jwt({"exp": 2_100_000_000})
    account = probe_account(tmp_path, probe_auth_payload(old_access))
    broker = probe_broker(tmp_path, {"account-test": account})
    broker.model_health_store.replace_account(
        "account-test",
        {
            "available": True,
            "models": {
                "gpt-text": {
                    "status": "available",
                    "available": True,
                    "last_probe_at": 1_900.0,
                }
            },
        },
    )
    refreshes = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refreshes
        if request.url.path == "/oauth/token":
            refreshes += 1
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "invalid_grant",
                        "message": "Refresh token is revoked.",
                        "type": "invalid_request_error",
                        "param": None,
                    }
                },
            )
        if request.url.path.endswith("/backend-api/codex/models"):
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"slug": "gpt-text", "supports_text": True, "available": True}
                    ]
                },
            )
        return httpx.Response(
            401,
            json={"error": {"code": "token_expired", "message": "Unauthorized"}},
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            broker.client = client
            return await broker.probe_models_once(now=2_000.0)

    state = asyncio.run(run())

    model = state["accounts"]["account-test"]["models"]["gpt-text"]
    assert refreshes == 1
    assert model["status"] == "auth"
    assert model["available"] is False
    assert model["http_status"] == 400
    assert model["error_code"] == "invalid_grant"


def test_probe_models_once_does_not_overlap_slow_rounds(tmp_path: Path):
    counter = {"active": 0, "peak": 0}
    account = FakeProbeAccount()
    account.active_counter = counter
    broker = probe_broker(tmp_path, {"account-test": account}, model_probe_concurrency=1)

    async def run():
        await asyncio.gather(
            broker.probe_models_once(now=2_000.0),
            broker.probe_models_once(now=2_001.0),
        )

    asyncio.run(run())
    assert counter["peak"] == 1


def test_probe_loop_runs_immediately_and_is_cancellable(tmp_path: Path):
    broker = probe_broker(tmp_path, {})
    started = asyncio.Event()
    calls = 0

    async def probe_once(now=None):
        nonlocal calls
        calls += 1
        started.set()
        return {"version": 1, "accounts": {}}

    broker.probe_models_once = probe_once

    async def run():
        task = asyncio.create_task(broker.model_probe_loop())
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert calls == 1


def test_lifespan_starts_probe_loop_then_cancels_it_and_closes_owned_client(monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()

    class Client:
        closed = False

        async def aclose(self):
            self.closed = True

    class Broker:
        owns_client = True
        client = Client()

        async def model_probe_loop(self):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    fake_broker = Broker()
    monkeypatch.setattr(broker_module, "broker", fake_broker)

    async def run():
        application = SimpleNamespace(state=SimpleNamespace())
        async with broker_module.lifespan(application):
            await asyncio.wait_for(started.wait(), timeout=1)
            assert fake_broker.client.closed is False
        assert stopped.is_set()
        assert fake_broker.client.closed is True

    asyncio.run(run())


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


def test_portal_user_has_hashed_password_and_recoverable_fixed_device_token():
    registry = {"version": 1, "devices": []}
    secret = "portal-secret-that-is-longer-than-thirty-two-characters"
    user, token = create_portal_user(registry, "yx.guo", "00123", secret)
    assert user["username"] == "YX.GUO"
    assert user["employee_id"] == "00123"
    assert user["password_hash"].startswith("pbkdf2_sha256$")
    assert verify_user_password("00123", user["password_hash"])
    assert not verify_user_password("00124", user["password_hash"])
    assert token == derive_user_device_token(secret, "YX.GUO", "00123")
    assert token not in json.dumps(registry)
    assert find_portal_user(registry, "yx.guo") is user
    with pytest.raises(ValueError, match="YX.GUO"):
        create_portal_user(registry, "guoyuxiang", "00124", secret)


def test_portal_pages_expose_admin_create_and_self_service_query_endpoints():
    root = Path(__file__).resolve().parents[1]
    dashboard = (root / "scripts" / "codex_quota_dashboard.html").read_text(encoding="utf-8")
    portal = (root / "scripts" / "codex_token_portal.html").read_text(encoding="utf-8")
    broker_source = (root / "scripts" / "codex_plus_broker.py").read_text(encoding="utf-8")
    assert "/v1/admin/users" in dashboard
    assert "/v1/token/query" in portal
    assert '@app.get("/token"' in broker_source
    assert '@app.post("/v1/admin/users"' in broker_source


def test_admin_creates_user_and_user_queries_token_without_plaintext_storage(
    tmp_path: Path, monkeypatch
):
    registry_path = tmp_path / "device-tokens.json"
    monkeypatch.setattr(
        broker_module,
        "settings",
        SimpleNamespace(
            device_token_file=registry_path,
            admin_token="admin-token-that-is-longer-than-thirty-two-characters",
            user_token_secret="portal-secret-that-is-longer-than-thirty-two-characters",
        ),
    )
    broker_module.token_query_failures.clear()
    client = TestClient(broker_module.app)
    created = client.post(
        "/v1/admin/users",
        headers={"Authorization": "Bearer admin-token-that-is-longer-than-thirty-two-characters"},
        json={"username": "yx.guo", "employee_id": "00042"},
    )
    assert created.status_code == 200
    queried = client.post(
        "/v1/token/query",
        json={"username": "YX.GUO", "password": "00042"},
    )
    assert queried.status_code == 200, queried.text
    assert queried.headers["cache-control"] == "no-store"
    token = queried.json()["device_token"]
    assert token.startswith("cwsdt_")
    assert token not in registry_path.read_text(encoding="utf-8")
    identity = broker_module._registered_device_identity(token)
    assert identity is not None
    assert identity.employee_id == "00042"
    duplicate = client.post(
        "/v1/admin/users",
        headers={"Authorization": "Bearer admin-token-that-is-longer-than-thirty-two-characters"},
        json={"username": "yx.guo", "employee_id": "00043"},
    )
    assert duplicate.status_code == 409
    assert client.post(
        "/v1/token/query", json={"username": "yx.guo", "password": "wrong"}
    ).status_code == 401


def test_official_401_forces_oauth_refresh_and_retries_usage(tmp_path: Path):
    old_access = jwt({"exp": 2_000_000_000})
    new_access = jwt({"exp": 2_100_000_000})
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": old_access,
                    "refresh_token": "old-refresh",
                    "account_id": "acct-test",
                }
            }
        ),
        encoding="utf-8",
    )

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def json(self):
            return self.payload

    class Client:
        def __init__(self):
            self.posts = 0
            self.gets = 0

        async def post(self, *_args, **_kwargs):
            self.posts += 1
            return Response(
                200,
                {"access_token": new_access, "refresh_token": "new-refresh"},
            )

        async def get(self, *_args, **_kwargs):
            self.gets += 1
            if self.gets == 1:
                return Response(401, {"error": {"code": "token_expired"}})
            return Response(
                200,
                {"rate_limit": {"primary_window": {"used_percent": 12}}},
            )

    client = Client()
    account = AccountRecord("account-test", auth_path)
    broker = TokenBroker.__new__(TokenBroker)
    broker.client = client
    broker.settings = SimpleNamespace(refresh_window_seconds=900, usage_cache_seconds=30)

    auth, usage = asyncio.run(broker._account_snapshot(account))

    assert client.posts == 1
    assert client.gets == 2
    assert auth["tokens"]["access_token"] == new_access
    assert usage["rate_limit"]["primary_window"]["used_percent"] == 12
    stored = json.loads(auth_path.read_text(encoding="utf-8"))
    assert stored["tokens"]["refresh_token"] == "new-refresh"
    assert stored["last_refresh_reason"] == "official_unauthorized"
    assert account.official_refresh_required is False


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


def health_aware_broker(now: int, state: dict, health_accounts: dict) -> TokenBroker:
    """Build a real lease broker around deterministic account/health snapshots."""
    broker = TokenBroker.__new__(TokenBroker)
    broker.settings = SimpleNamespace(
        lease_seconds=28_800,
        lease_rotation_seconds=86_400,
    )
    broker.accounts = {"chatgpt010": "ten", "chatgpt024": "twenty-four"}
    broker.state_lock = asyncio.Lock()
    broker._load_state = lambda: state
    broker._save_state = lambda value: state.update(value)
    broker.model_health_store = SimpleNamespace(
        load=lambda: {"version": 1, "accounts": health_accounts}
    )

    async def snapshot(account):
        account_id = "acct-ten" if account == "ten" else "acct-twenty-four"
        access_token = jwt(
            {
                "exp": now + 3600,
                "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
            }
        )
        used = 50 if account == "ten" else 1
        return {"tokens": {"access_token": access_token}}, {
            "rate_limit": {"primary_window": {"used_percent": used, "reset_after_seconds": 3600}}
        }

    broker._account_snapshot = snapshot
    return broker


def test_new_lease_excludes_account_when_all_models_are_blocked(monkeypatch):
    now = 2_000_000_000
    broker = health_aware_broker(
        now,
        {"leases": {}},
        {
            "chatgpt010": {"models": {"gpt-test": {"status": "available", "available": True}}},
            "chatgpt024": {
                "models": {
                    "gpt-test": {
                        "status": "capacity",
                        "available": False,
                        "cooldown_until": now + 1800,
                    }
                }
            },
        },
    )
    monkeypatch.setattr("scripts.codex_plus_broker.time.time", lambda: now)

    result = asyncio.run(broker.lease(DeviceIdentity(token_id="device-a", label="employee-a"), "pc", None))

    assert result["account_alias"] == "chatgpt010"


def test_probe_unknown_does_not_exclude_an_account(monkeypatch):
    now = 2_000_000_000
    broker = health_aware_broker(
        now,
        {"leases": {}},
        {
            "chatgpt010": {"models": {"gpt-test": {"status": "probe_error", "available": None}}},
            "chatgpt024": {
                "models": {
                    "gpt-test": {
                        "status": "capacity",
                        "available": False,
                        "cooldown_until": now + 1800,
                    }
                }
            },
        },
    )
    monkeypatch.setattr("scripts.codex_plus_broker.time.time", lambda: now)

    result = asyncio.run(broker.lease(DeviceIdentity(token_id="device-a", label="employee-a"), "pc", None))

    assert result["account_alias"] == "chatgpt010"


def test_existing_affinity_is_dropped_when_account_becomes_blocked(monkeypatch):
    now = 2_000_000_000
    device_hash = hashlib.sha256(b"pc").hexdigest()
    state = {
        "leases": {
            "old-024-lease": {
                "account_alias": "chatgpt024",
                "device_token_id": "device-a",
                "client_device_hash": device_hash,
                "created_at": now,
                "expires_at": now + 100,
            }
        }
    }
    broker = health_aware_broker(
        now,
        state,
        {
            "chatgpt010": {"models": {"gpt-test": {"status": "available", "available": True}}},
            "chatgpt024": {
                "models": {
                    "gpt-test": {
                        "status": "capacity",
                        "available": False,
                        "cooldown_until": now + 1800,
                    }
                }
            },
        },
    )
    monkeypatch.setattr("scripts.codex_plus_broker.time.time", lambda: now)

    result = asyncio.run(
        broker.lease(DeviceIdentity(token_id="device-a", label="employee-a"), "pc", "old-024-lease")
    )

    assert result["lease_id"] != "old-024-lease"
    assert result["account_alias"] == "chatgpt010"
    assert "old-024-lease" not in state["leases"]


def test_blocked_affinity_cleanup_is_saved_before_replacement_failure(monkeypatch):
    now = 2_000_000_000
    device_hash = hashlib.sha256(b"pc").hexdigest()
    persisted = {
        "leases": {
            "old-024-lease": {
                "account_alias": "chatgpt024",
                "device_token_id": "device-a",
                "client_device_hash": device_hash,
                "created_at": now,
                "expires_at": now + 100,
            }
        }
    }
    broker = health_aware_broker(
        now,
        persisted,
        {
            "chatgpt010": {"models": {"gpt-test": {"status": "available", "available": True}}},
            "chatgpt024": {
                "models": {
                    "gpt-test": {
                        "status": "quota",
                        "available": False,
                    }
                }
            },
        },
    )
    broker._load_state = lambda: json.loads(json.dumps(persisted))
    broker._save_state = lambda value: persisted.update(value)

    async def unavailable_snapshot(_account):
        raise RuntimeError("replacement unavailable")

    broker._account_snapshot = unavailable_snapshot
    monkeypatch.setattr("scripts.codex_plus_broker.time.time", lambda: now)

    with pytest.raises(broker_module.HTTPException) as exc_info:
        asyncio.run(
            broker.lease(DeviceIdentity(token_id="device-a", label="employee-a"), "pc", "old-024-lease")
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "all Codex accounts are unavailable"
    assert "old-024-lease" not in persisted["leases"]


def test_blocked_affinity_cleanup_is_saved_before_missing_account_id_failure(monkeypatch):
    now = 2_000_000_000
    device_hash = hashlib.sha256(b"pc").hexdigest()
    persisted = {
        "leases": {
            "old-024-lease": {
                "account_alias": "chatgpt024",
                "device_token_id": "device-a",
                "client_device_hash": device_hash,
                "created_at": now,
                "expires_at": now + 100,
            }
        }
    }
    broker = health_aware_broker(
        now,
        persisted,
        {
            "chatgpt010": {"models": {"gpt-test": {"status": "available", "available": True}}},
            "chatgpt024": {
                "models": {
                    "gpt-test": {
                        "status": "quota",
                        "available": False,
                    }
                }
            },
        },
    )
    broker._load_state = lambda: json.loads(json.dumps(persisted))
    broker._save_state = lambda value: persisted.update(value)

    async def snapshot_without_account_id(_account):
        return {"tokens": {"access_token": jwt({"exp": now + 3600})}}, None

    broker._account_snapshot = snapshot_without_account_id
    monkeypatch.setattr("scripts.codex_plus_broker.time.time", lambda: now)

    with pytest.raises(broker_module.HTTPException) as exc_info:
        asyncio.run(
            broker.lease(DeviceIdentity(token_id="device-a", label="employee-a"), "pc", "old-024-lease")
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "selected account has no ChatGPT account id"
    assert "old-024-lease" not in persisted["leases"]


def test_blocked_affinity_cleanup_is_saved_when_every_account_is_blocked(monkeypatch):
    now = 2_000_000_000
    device_hash = hashlib.sha256(b"pc").hexdigest()
    persisted = {
        "leases": {
            "old-024-lease": {
                "account_alias": "chatgpt024",
                "device_token_id": "device-a",
                "client_device_hash": device_hash,
                "created_at": now,
                "expires_at": now + 100,
            }
        }
    }
    broker = health_aware_broker(
        now,
        persisted,
        {
            alias: {"models": {"gpt-test": {"status": "quota", "available": False}}}
            for alias in ("chatgpt010", "chatgpt024")
        },
    )
    broker._load_state = lambda: json.loads(json.dumps(persisted))
    broker._save_state = lambda value: persisted.update(value)
    monkeypatch.setattr("scripts.codex_plus_broker.time.time", lambda: now)

    with pytest.raises(broker_module.HTTPException) as exc_info:
        asyncio.run(
            broker.lease(DeviceIdentity(token_id="device-a", label="employee-a"), "pc", "old-024-lease")
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "all Codex accounts have blocked models"
    assert "old-024-lease" not in persisted["leases"]


def test_lease_returns_sanitized_503_when_every_account_is_explicitly_blocked(monkeypatch):
    now = 2_000_000_000
    broker = health_aware_broker(
        now,
        {"leases": {}},
        {
            alias: {"models": {"gpt-test": {"status": "quota", "available": False}}}
            for alias in ("chatgpt010", "chatgpt024")
        },
    )
    monkeypatch.setattr("scripts.codex_plus_broker.time.time", lambda: now)

    with pytest.raises(broker_module.HTTPException) as exc_info:
        asyncio.run(broker.lease(DeviceIdentity(token_id="device-a", label="employee-a"), "pc", None))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "all Codex accounts have blocked models"


def test_admin_account_status_exposes_only_sanitized_model_health(monkeypatch):
    now = 2_000_000_000
    broker = health_aware_broker(
        now,
        {"leases": {}},
        {
            "chatgpt010": {
                "models": {
                    "gpt-test": {
                        "status": "capacity",
                        "available": False,
                        "last_probe_at": now - 60,
                        "last_probe_error_at": now - 55,
                        "cooldown_until": now + 1800,
                        "http_status": 503,
                        "error_code": "server_overloaded",
                        "error_message": "x" * 600,
                        "access_token": "must-not-leak",
                        "refresh_token": "must-not-leak",
                        "authorization": "Bearer must-not-leak",
                        "request_body": "must-not-leak",
                        "probe_response_body": "must-not-leak",
                    }
                }
            }
        },
    )
    broker.accounts = {"chatgpt010": "ten"}
    monkeypatch.setattr("scripts.codex_plus_broker.time.time", lambda: now)
    monkeypatch.setattr(broker_module, "broker", broker)
    monkeypatch.setattr(
        broker_module,
        "settings",
        SimpleNamespace(admin_token="admin-token-that-is-longer-than-thirty-two-characters"),
    )

    response = TestClient(broker_module.app).get(
        "/v1/admin/accounts",
        headers={"Authorization": "Bearer admin-token-that-is-longer-than-thirty-two-characters"},
    )

    assert response.status_code == 200, response.text
    health = response.json()["accounts"][0]["model_health"]
    assert health == {
        "available": False,
        "models": [
            {
                "name": "gpt-test",
                "status": "capacity",
                "available": False,
                "last_probe_at": now - 60,
                "last_probe_error_at": now - 55,
                "cooldown_until": now + 1800,
                "http_status": 503,
                "error_code": "server_overloaded",
                "error_message": "x" * 500,
            }
        ],
    }
    rendered = response.text
    for secret_field in ("access_token", "refresh_token", "authorization", "request_body", "probe_response_body"):
        assert secret_field not in rendered


def test_lease_rotation_rebalances_after_24_hours(monkeypatch):
    now = 2_000_000_000
    broker = TokenBroker.__new__(TokenBroker)
    broker.settings = SimpleNamespace(
        lease_seconds=28_800,
        lease_rotation_seconds=86_400,
    )
    broker.accounts = {"account-01": "one", "account-02": "two"}
    broker.state_lock = asyncio.Lock()
    device_hash = __import__("hashlib").sha256(b"pc/user").hexdigest()
    state = {
        "leases": {
            "expired-affinity": {
                "account_alias": "account-01",
                "device_token_id": "device-a",
                "client_device_hash": device_hash,
                "created_at": now - 86_401,
                "expires_at": now + 100,
            },
            "other-active-user": {
                "account_alias": "account-01",
                "device_token_id": "device-b",
                "client_device_hash": "other",
                "created_at": now,
                "expires_at": now + 100,
            },
        }
    }
    saved = {}
    broker._load_state = lambda: state
    broker._save_state = lambda value: saved.update(value)

    async def snapshot(account):
        account_id = "acct-one" if account == "one" else "acct-two"
        access_token = jwt(
            {
                "exp": now + 3600,
                "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
            }
        )
        usage = {"rate_limit": {"primary_window": {"used_percent": 20, "reset_after_seconds": 3600}}}
        return {"tokens": {"access_token": access_token}}, usage

    broker._account_snapshot = snapshot
    monkeypatch.setattr("scripts.codex_plus_broker.time.time", lambda: now)
    result = asyncio.run(
        broker.lease(
            DeviceIdentity(token_id="device-a", label="employee-a"),
            "pc/user",
            "expired-affinity",
        )
    )
    assert result["lease_id"] != "expired-affinity"
    assert result["account_alias"] == "account-02"
    assert "expired-affinity" not in saved["leases"]
    assert saved["leases"][result["lease_id"]]["created_at"] == now


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
    assert "tokens.access_token" not in dashboard
    assert "refresh_token" not in dashboard
    assert "access_token_expires_at" in dashboard
    assert "account.email" in dashboard
    assert 'id="brokerVersion"' in dashboard
    assert 'id="onlyActive"' in dashboard
    assert "hasActivity" in dashboard


def test_quota_dashboard_renders_sanitized_model_health_through_safe_dom_nodes():
    dashboard = (Path(__file__).parents[1] / "scripts" / "codex_quota_dashboard.html").read_text(encoding="utf-8")

    assert "model_health" in dashboard
    assert "renderModelHealth" in dashboard
    for label in ("模型可用", "容量超限", "额度耗尽", "鉴权失败", "不支持", "探测异常", "探测未知"):
        assert label in dashboard
    assert "innerHTML" not in dashboard
    assert "el.textContent=esc(text)" in dashboard


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
