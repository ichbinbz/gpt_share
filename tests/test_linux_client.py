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


def test_linux_launcher_reuses_vscode_profile_and_switches_only_codex_home():
    source = (CLIENT / "cws_codex.py").read_text(encoding="utf-8")
    assert 'environment["CODEX_HOME"]' in source
    assert "--user-data-dir" not in source
    assert "--new-window" not in source
    assert "subprocess.Popen(" in source


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
