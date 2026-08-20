import importlib.util
import json
import os
import tarfile
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


def test_linux_auth_payload_matches_official_codex_format():
    payload = MODULE.make_auth_payload(
        {"access_token": "access", "account_id": "account", "lease_id": "lease"}
    )
    assert payload["auth_mode"] == "chatgptAuthTokens"
    assert payload["OPENAI_API_KEY"] is None
    assert payload["tokens"]["access_token"] == "access"
    assert payload["tokens"]["account_id"] == "account"
    assert payload["tokens"]["refresh_token"] == ""


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
