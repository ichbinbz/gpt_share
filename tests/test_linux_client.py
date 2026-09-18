import importlib.util
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import time
from pathlib import Path

import pytest

from scripts.build_linux_client import ARCHIVE_ROOT, build


ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "linux-client"
SPEC = importlib.util.spec_from_file_location("cws_linux_client", CLIENT / "cws_codex.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_linux_bundle_contains_expected_entrypoints():
    assert (CLIENT / "install.sh").is_file()
    assert (CLIENT / "uninstall.sh").is_file()
    assert (CLIENT / "cws_codex.py").is_file()
    installer = (CLIENT / "install.sh").read_text(encoding="utf-8")
    assert "systemctl --user enable --now cws-codex-sync.timer" in installer
    assert "openai.chatgpt" in installer
    assert "sudo" in installer


def test_linux_launcher_reuses_vscode_profile_and_default_codex_history():
    source = (CLIENT / "cws_codex.py").read_text(encoding="utf-8")
    installer = (CLIENT / "install.sh").read_text(encoding="utf-8")
    assert 'environment["CODEX_HOME"]' in source
    assert 'CODEX_HOME="${HOME}/.codex"' in installer
    assert 'CLIENT_HOME="${HOME}/.cws-codex"' in installer
    assert '"client_home":' in source
    assert "--user-data-dir" not in source
    assert "--new-window" not in source
    assert "subprocess.Popen(" in source


def test_linux_client_checks_github_release_and_preserves_token_on_update():
    source = (CLIENT / "cws_codex.py").read_text(encoding="utf-8")
    installer = (CLIENT / "install.sh").read_text(encoding="utf-8")
    assert "api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest" in source
    assert "CWS-Codex-Linux-v{latest}.tar.gz" in source
    assert "confirm_graphical_update" in source
    assert "sha256:" in source
    assert '"--auto-update"' in source
    assert "--keep-existing-token" in source
    assert "--auto-update) AUTO_UPDATE=1" in installer
    assert "SETUP_ARGS+=(--keep-existing-token)" in installer


class _BytesResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size: int = -1) -> bytes:
        if _size < 0:
            result, self.payload = self.payload, b""
            return result
        result, self.payload = self.payload[:_size], self.payload[_size:]
        return result


def _broker_manifest(version: str = "0.1.7", payload: bytes = b"linux archive"):
    name = f"CWS-Codex-Linux-v{version}.tar.gz"
    return {
        "release_version": version,
        "published_at": "2026-09-18T00:00:00Z",
        "assets": [
            {
                "platform": "linux-tarball",
                "name": name,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "download_url": f"/v1/client/releases/download/{name}",
            }
        ],
    }


def _github_release(version: str = "0.1.7", payload: bytes = b"linux archive"):
    name = f"CWS-Codex-Linux-v{version}.tar.gz"
    return {
        "tag_name": f"v{version}",
        "assets": [
            {
                "name": name,
                "size": len(payload),
                "digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
                "browser_download_url": (
                    f"https://github.com/ichbinbz/gpt_share/releases/download/v{version}/{name}"
                ),
            }
        ],
    }


def _linux_archive(version: str = "0.1.7") -> bytes:
    output = io.BytesIO()
    root = f"CWS-Codex-Linux-v{version}"
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        installer = tarfile.TarInfo(f"{root}/install.sh")
        installer.mode = 0o755
        body = b"#!/bin/sh\nexit 0\n"
        installer.size = len(body)
        archive.addfile(installer, io.BytesIO(body))
    return output.getvalue()


def test_linux_broker_release_candidate_uses_bearer_and_proxy(monkeypatch):
    observed = []

    def open_url(url, headers, proxy_url, timeout, *, block_redirects=False):
        observed.append((url, headers, proxy_url, timeout, block_redirects))
        return _BytesResponse(json.dumps(_broker_manifest()).encode("utf-8"))

    monkeypatch.setattr(MODULE, "open_url", open_url)
    candidate = MODULE.broker_release_candidate(
        {"broker_url": "https://broker.example.test", "proxy_url": "http://proxy:8080"},
        "cwsdt_test-token-long-enough-123456",
    )
    assert candidate["source"] == "Broker"
    assert candidate["download_url"] == (
        "https://broker.example.test/v1/client/releases/download/CWS-Codex-Linux-v0.1.7.tar.gz"
    )
    assert observed == [
        (
            "https://broker.example.test/v1/client/releases/latest",
            {
                "Accept": "application/json",
                "Authorization": "Bearer cwsdt_test-token-long-enough-123456",
                "User-Agent": "CWS-Codex-Linux/0.1.6",
            },
            "http://proxy:8080",
            30,
            True,
        )
    ]


def test_linux_broker_release_success_skips_github(monkeypatch):
    calls = []

    def open_url(url, headers, proxy_url, timeout, *, block_redirects=False):
        calls.append(url)
        if "api.github.com" in url:
            pytest.fail("GitHub must not be called after Broker success")
        return _BytesResponse(json.dumps(_broker_manifest()).encode("utf-8"))

    monkeypatch.setattr(MODULE, "open_url", open_url)
    candidate = MODULE.select_release_candidate({"broker_url": "https://broker.example.test"}, "device-token")
    assert candidate["source"] == "Broker"
    assert calls == ["https://broker.example.test/v1/client/releases/latest"]


def test_linux_update_fallback_uses_github_without_authorization(monkeypatch):
    observed = []

    def open_url(url, headers, proxy_url, timeout, *, block_redirects=False):
        observed.append((url, headers, proxy_url, timeout, block_redirects))
        if "broker" in url:
            raise RuntimeError("broker unavailable")
        return _BytesResponse(json.dumps(_github_release()).encode("utf-8"))

    monkeypatch.setattr(MODULE, "open_url", open_url)
    candidate = MODULE.select_release_candidate(
        {"broker_url": "https://broker.example.test", "proxy_url": "http://proxy:8080"},
        "cwsdt_test-token-long-enough-123456",
    )
    assert candidate["source"] == "GitHub"
    assert "Authorization" not in observed[1][1]
    assert observed[1][2] == "http://proxy:8080"


def test_linux_update_fallback_reports_both_failures_without_leaking_device_token(monkeypatch):
    token = "cwsdt_secret-token-long-enough-123456"

    def open_url(url, headers, proxy_url, timeout, *, block_redirects=False):
        if "broker" in url:
            raise RuntimeError(f"Bearer {token}")
        raise RuntimeError("offline")

    monkeypatch.setattr(MODULE, "open_url", open_url)
    with pytest.raises(RuntimeError, match="Broker 更新检查失败.*GitHub 更新检查失败") as error:
        MODULE.select_release_candidate({}, token)
    assert token not in str(error.value)


@pytest.mark.parametrize(
    "download_url",
    [
        "https://other.example.test/v1/client/releases/download/CWS-Codex-Linux-v0.1.7.tar.gz",
        "https://broker.example.test:444/v1/client/releases/download/CWS-Codex-Linux-v0.1.7.tar.gz",
        "https://broker.example.test:0/v1/client/releases/download/CWS-Codex-Linux-v0.1.7.tar.gz",
        "https://user@broker.example.test/v1/client/releases/download/CWS-Codex-Linux-v0.1.7.tar.gz",
    ],
)
def test_linux_broker_candidate_rejects_cross_origin_or_userinfo_download(monkeypatch, download_url):
    manifest = _broker_manifest()
    manifest["assets"][0]["download_url"] = download_url
    monkeypatch.setattr(
        MODULE,
        "open_url",
        lambda *_args, **_kwargs: _BytesResponse(json.dumps(manifest).encode("utf-8")),
    )
    with pytest.raises(RuntimeError, match="同源|无效"):
        MODULE.broker_release_candidate({"broker_url": "https://broker.example.test"}, "device-token")


def test_linux_update_hash_mismatch_skips_install_and_cleans_temp_directory(tmp_path, monkeypatch):
    archive_payload = b"tampered archive"
    candidate = {
        "source": "Broker",
        "version": "0.1.7",
        "name": "CWS-Codex-Linux-v0.1.7.tar.gz",
        "size": len(archive_payload),
        "sha256": "0" * 64,
        "download_url": "https://broker.example.test/v1/client/releases/download/CWS-Codex-Linux-v0.1.7.tar.gz",
        "headers": {"Authorization": "Bearer device-token", "User-Agent": "CWS-Codex-Linux/0.1.6"},
    }
    created = []
    real_mkdtemp = tempfile.mkdtemp

    class TrackingTemporaryDirectory:
        def __init__(self, **_kwargs):
            self.name = real_mkdtemp(dir=tmp_path)
            created.append(Path(self.name))

        def __enter__(self):
            return self.name

        def __exit__(self, *_args):
            shutil.rmtree(self.name)

    monkeypatch.setattr(MODULE.tempfile, "TemporaryDirectory", TrackingTemporaryDirectory)
    monkeypatch.setattr(MODULE, "open_url", lambda *_args, **_kwargs: _BytesResponse(archive_payload))
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("installer must not run"))
    with pytest.raises(RuntimeError, match="SHA-256"):
        MODULE.install_release_candidate(candidate, {"proxy_url": "http://proxy:8080"})
    assert created and not created[0].exists()


def test_linux_broker_download_uses_proxy_bearer_and_blocks_redirects(monkeypatch):
    archive_payload = _linux_archive()
    candidate = {
        "source": "Broker",
        "version": "0.1.7",
        "name": "CWS-Codex-Linux-v0.1.7.tar.gz",
        "size": len(archive_payload),
        "sha256": hashlib.sha256(archive_payload).hexdigest(),
        "download_url": "https://broker.example.test/v1/client/releases/download/CWS-Codex-Linux-v0.1.7.tar.gz",
        "headers": {"Authorization": "Bearer device-token", "User-Agent": "CWS-Codex-Linux/0.1.6"},
    }
    observed = []

    def open_url(url, headers, proxy_url, timeout, *, block_redirects=False):
        observed.append((url, headers, proxy_url, timeout, block_redirects))
        return _BytesResponse(archive_payload)

    monkeypatch.setattr(MODULE, "open_url", open_url)
    monkeypatch.setattr(MODULE.subprocess, "run", lambda command, **_kwargs: observed.append(command))
    assert MODULE.install_release_candidate(
        candidate,
        {"broker_url": "https://broker.example.test", "proxy_url": "http://proxy:8080", "codex_home": "/tmp/codex"},
    )
    assert observed[0] == (
        candidate["download_url"],
        candidate["headers"],
        "http://proxy:8080",
        180,
        True,
    )


def test_linux_github_candidate_rejects_noncanonical_asset_url(monkeypatch):
    release = _github_release()
    release["assets"][0]["browser_download_url"] += "?token=unexpected"
    monkeypatch.setattr(
        MODULE,
        "open_url",
        lambda *_args, **_kwargs: _BytesResponse(json.dumps(release).encode("utf-8")),
    )
    with pytest.raises(RuntimeError, match="安全检查"):
        MODULE.github_release_candidate({})


def test_linux_auth_payload_matches_official_codex_format():
    payload = MODULE.make_auth_payload(
        {"access_token": "access", "account_id": "account", "lease_id": "lease"}
    )
    assert payload["auth_mode"] == "chatgptAuthTokens"
    assert payload["OPENAI_API_KEY"] is None
    assert payload["tokens"]["access_token"] == "access"
    assert payload["tokens"]["account_id"] == "account"
    assert payload["tokens"]["refresh_token"] == ""


def test_linux_client_requests_new_lease_after_24_hours(tmp_path, monkeypatch):
    install_dir = tmp_path / "install"
    codex_home = tmp_path / ".codex"
    client_home = tmp_path / ".cws-codex"
    install_dir.mkdir()
    codex_home.mkdir()
    client_home.mkdir()
    config_path = install_dir / "config.json"
    MODULE.write_json_atomic(
        config_path,
        {
            "broker_url": "http://broker",
            "proxy_url": "",
            "codex_home": str(codex_home),
            "client_home": str(client_home),
            "lease_rotation_seconds": 86400,
        },
    )
    (install_dir / "device-token").write_text("cwsdt_test-token-long-enough-123456", encoding="utf-8")
    MODULE.write_json_atomic(
        client_home / "cws-lease.json",
        {"lease_id": "old-lease", "lease_started_at": int(time.time()) - 86401},
    )
    requested = {}

    def request_json(_url, **kwargs):
        requested.update(kwargs["body"])
        return {
            "lease_id": "new-lease",
            "access_token": "access",
            "account_id": "account",
            "account_alias": "account-02",
        }

    monkeypatch.setattr(MODULE, "request_json", request_json)
    monkeypatch.setattr(MODULE, "initialize_auth_backup", lambda *_args: None)
    monkeypatch.setattr(MODULE, "initialize_usage_baseline", lambda *_args: None)
    monkeypatch.setattr(MODULE, "mark_managed_auth", lambda *_args: None)
    monkeypatch.setattr(MODULE, "local_ipv4", lambda: None)
    MODULE.synchronize(config_path, quiet=True)
    assert "lease_id" not in requested
    lease = json.loads((client_home / "cws-lease.json").read_text(encoding="utf-8"))
    assert lease["lease_id"] == "new-lease"
    assert lease["lease_started_at"] >= int(time.time()) - 5


def test_linux_usage_reader_uses_latest_counter_and_hashes_session_name(tmp_path):
    session = tmp_path / "sessions" / "2026" / "session-1.jsonl"
    session.parent.mkdir(parents=True)
    events = [
        {"payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 10}}}},
        {"payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 25, "input_tokens": 7}}}},
    ]
    session.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    records = MODULE.collect_session_usage(tmp_path / "sessions")
    assert len(records) == 1
    assert records[0]["total_tokens"] == 25
    assert records[0]["input_tokens"] == 7
    assert len(records[0]["session_id"]) == 64


def test_linux_usage_reader_counts_user_text_locally_and_builds_daily_buckets(tmp_path):
    session = tmp_path / "sessions" / "2026" / "session-user.jsonl"
    session.parent.mkdir(parents=True)
    events = [
        {
            "timestamp": "2026-08-20T01:00:00Z",
            "payload": {"type": "user_message", "message": "hello世界"},
        },
        {
            "timestamp": "2026-08-20T01:00:01Z",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}},
            },
        },
        {
            "timestamp": "2026-08-20T01:00:02Z",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 25, "output_tokens": 3, "total_tokens": 28}},
            },
        },
    ]
    session.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    record = MODULE.collect_session_usage(tmp_path / "sessions")[0]
    assert record["user_message_count"] == 1
    assert record["user_text_characters"] == 7
    assert record["user_text_tokens_estimated"] == 4
    assert record["daily_usage"] == [
        {
            "date": "2026-08-20",
            "input_tokens": 25,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 3,
            "reasoning_output_tokens": 0,
            "total_tokens": 28,
            "user_message_count": 1,
            "user_text_characters": 7,
            "user_text_tokens_estimated": 4,
        }
    ]


def test_linux_auth_backup_restore_and_usage_baseline(tmp_path):
    codex_home = tmp_path / ".codex"
    client_home = tmp_path / ".cws-codex"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    old_session = sessions / "old.jsonl"
    old_session.write_text("{}\n", encoding="utf-8")
    original_auth = {"original": True}
    MODULE.write_json_atomic(codex_home / "auth.json", original_auth)

    MODULE.initialize_auth_backup(codex_home, client_home)
    MODULE.initialize_usage_baseline(codex_home, client_home)
    MODULE.write_json_atomic(codex_home / "auth.json", {"company": True})
    MODULE.mark_managed_auth(codex_home / "auth.json", client_home)

    config_path = tmp_path / "config.json"
    MODULE.write_json_atomic(
        config_path,
        {"codex_home": str(codex_home), "client_home": str(client_home)},
    )
    baseline = json.loads((client_home / "usage-baseline.json").read_text(encoding="utf-8"))
    assert MODULE.session_id_for_name(old_session.name) in baseline["excluded_session_ids"]
    assert MODULE.restore_original_auth(config_path)
    assert json.loads((codex_home / "auth.json").read_text(encoding="utf-8")) == original_auth
    assert not (client_home / "original-auth-state.json").exists()
    assert not (client_home / "usage-baseline.json").exists()


def test_linux_restore_does_not_overwrite_auth_changed_after_sync(tmp_path):
    codex_home = tmp_path / ".codex"
    client_home = tmp_path / ".cws-codex"
    MODULE.write_json_atomic(codex_home / "auth.json", {"original": True})
    MODULE.initialize_auth_backup(codex_home, client_home)
    MODULE.write_json_atomic(codex_home / "auth.json", {"company": True})
    MODULE.mark_managed_auth(codex_home / "auth.json", client_home)
    MODULE.write_json_atomic(codex_home / "auth.json", {"new_login": True})
    config_path = tmp_path / "config.json"
    MODULE.write_json_atomic(
        config_path,
        {"codex_home": str(codex_home), "client_home": str(client_home)},
    )
    assert not MODULE.restore_original_auth(config_path)
    assert json.loads((codex_home / "auth.json").read_text(encoding="utf-8")) == {"new_login": True}


@pytest.mark.skipif(os.name == "nt", reason="Windows does not implement POSIX chmod bits")
def test_linux_private_files_are_written_with_user_only_permissions(tmp_path):
    path = tmp_path / "private" / "config.json"
    MODULE.write_json_atomic(path, {"ok": True})
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_linux_release_archive_has_expected_modes(tmp_path):
    output = tmp_path / "client.tar.gz"
    build(ROOT, output)
    with tarfile.open(output, "r:gz") as archive:
        entries = {item.name: item for item in archive.getmembers()}
        assert entries[f"{ARCHIVE_ROOT}/install.sh"].mode == 0o755
        assert entries[f"{ARCHIVE_ROOT}/cws_codex.py"].mode == 0o755
        assert entries[f"{ARCHIVE_ROOT}/README-Linux.txt"].mode == 0o644
        assert b"\r\n" not in archive.extractfile(f"{ARCHIVE_ROOT}/install.sh").read()
