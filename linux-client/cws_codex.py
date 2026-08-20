#!/usr/bin/env python3
"""CWS Codex Ubuntu/Linux employee client."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


VERSION = "0.1.4"
GITHUB_REPOSITORY = "ichbinbz/gpt_share"
INSTALL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = INSTALL_DIR / "config.json"
TOKEN_PATH = INSTALL_DIR / "device-token"
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
USER_INPUT_FIELDS = (
    "user_message_count",
    "user_text_characters",
    "user_text_tokens_estimated",
)
USAGE_FIELDS = TOKEN_FIELDS + USER_INPUT_FIELDS
CHINA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def write_json_atomic(path: Path, value: Any) -> None:
    private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


def read_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"配置文件不存在，请重新安装：{path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"配置文件格式错误：{path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"配置文件格式错误：{path}")
    return value


def read_device_token(path: Path = TOKEN_PATH) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError("设备令牌不存在，请重新运行 install.sh") from exc
    if not token.startswith("cwsdt_") or len(token) < 30:
        raise RuntimeError("设备令牌格式错误，应为 cwsdt_ 开头的本机专用令牌")
    return token


def expanded_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def client_home_from_config(config: dict[str, Any]) -> Path:
    return expanded_path(str(config.get("client_home") or "~/.cws-codex"))


def session_id_for_name(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def initialize_auth_backup(codex_home: Path, client_home: Path) -> None:
    private_directory(client_home)
    private_directory(codex_home)
    state_path = client_home / "original-auth-state.json"
    if state_path.is_file():
        return
    auth_path = codex_home / "auth.json"
    backup_path = client_home / "original-auth.json"
    had_auth = auth_path.is_file()
    if had_auth:
        shutil.copyfile(auth_path, backup_path)
        backup_path.chmod(0o600)
    write_json_atomic(state_path, {"version": 1, "had_auth": had_auth})


def initialize_usage_baseline(codex_home: Path, client_home: Path) -> None:
    baseline_path = client_home / "usage-baseline.json"
    if baseline_path.is_file():
        return
    sessions_root = codex_home / "sessions"
    excluded = sorted(
        {
            session_id_for_name(path.name)
            for path in sessions_root.rglob("*.jsonl")
        }
        if sessions_root.is_dir()
        else set()
    )
    write_json_atomic(
        baseline_path,
        {"version": 1, "excluded_session_ids": excluded},
    )


def mark_managed_auth(auth_path: Path, client_home: Path) -> None:
    digest = hashlib.sha256(auth_path.read_bytes()).hexdigest()
    marker = client_home / "managed-auth.sha256"
    marker.write_text(digest + "\n", encoding="utf-8")
    marker.chmod(0o600)


def restore_original_auth(config_path: Path = CONFIG_PATH) -> bool:
    config = read_config(config_path)
    codex_home = expanded_path(str(config["codex_home"]))
    client_home = client_home_from_config(config)
    state_path = client_home / "original-auth-state.json"
    if not state_path.is_file():
        return False
    auth_path = codex_home / "auth.json"
    marker_path = client_home / "managed-auth.sha256"
    if auth_path.is_file() and marker_path.is_file():
        expected = marker_path.read_text(encoding="utf-8").strip()
        current = hashlib.sha256(auth_path.read_bytes()).hexdigest()
        if expected and current != expected:
            print(
                "警告：当前 Codex 登录凭据已被其他程序修改，为避免覆盖，未恢复安装前凭据。",
                file=sys.stderr,
            )
            return False
    state = json.loads(state_path.read_text(encoding="utf-8"))
    backup_path = client_home / "original-auth.json"
    if bool(state.get("had_auth")):
        if not backup_path.is_file():
            raise RuntimeError("原 Codex 登录凭据备份不存在，无法恢复")
        shutil.copyfile(backup_path, auth_path)
        auth_path.chmod(0o600)
    else:
        auth_path.unlink(missing_ok=True)
    for path in (
        marker_path,
        backup_path,
        state_path,
        client_home / "usage-baseline.json",
    ):
        path.unlink(missing_ok=True)
    return True


def local_ipv4() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        value = sock.getsockname()[0]
        if not value.startswith(("127.", "169.254.")):
            return value
    except OSError:
        return None
    finally:
        sock.close()
    return None


def request_json(
    url: str,
    *,
    proxy_url: str = "",
    device_token: str | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    proxy_handler = (
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        if proxy_url
        else urllib.request.ProxyHandler({})
    )
    opener = urllib.request.build_opener(proxy_handler)
    headers = {"Accept": "application/json"}
    data = None
    if device_token:
        headers["Authorization"] = f"Bearer {device_token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Broker 返回 HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 Broker：{exc.reason}") from exc
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Broker 返回了无效 JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Broker 返回格式无效")
    return value


def version_tuple(value: str) -> tuple[int, int, int]:
    parts = value.strip().lstrip("v").split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise RuntimeError(f"GitHub Release 版本号无效：{value}")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def confirm_graphical_update(version: str) -> bool:
    message = (
        f"检测到 CWS Codex v{version}。是否立即从 GitHub 下载并自动更新？\n\n"
        "更新会保留设备令牌、VS Code 配置和历史对话。"
    )
    zenity = shutil.which("zenity")
    if zenity:
        return subprocess.run(
            [zenity, "--question", "--title=CWS Codex 客户端更新", f"--text={message}"],
            check=False,
        ).returncode == 0
    if sys.stdin.isatty():
        return input(f"{message}\n立即更新？[Y/n]：").strip().lower() in {"", "y", "yes"}
    return False


def check_for_update(config_path: Path = CONFIG_PATH, *, force: bool = False) -> bool:
    config = read_config(config_path)
    client_home = client_home_from_config(config)
    state_path = client_home / "update-state.json"
    now = datetime.now(timezone.utc)
    if not force and state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            checked = datetime.fromisoformat(str(state["last_checked_at"]).replace("Z", "+00:00"))
            if (now - checked).total_seconds() < 86400:
                return False
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass

    release = request_json(
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest",
        proxy_url=str(config.get("proxy_url") or ""),
    )
    latest = str(release.get("tag_name") or "").lstrip("v")
    latest_tuple = version_tuple(latest)
    write_json_atomic(
        state_path,
        {
            "version": 1,
            "last_checked_at": now.isoformat(),
            "latest_version": latest,
        },
    )
    if latest_tuple <= version_tuple(VERSION):
        return False

    asset_name = f"CWS-Codex-Linux-v{latest}.tar.gz"
    asset = next(
        (
            item
            for item in release.get("assets", [])
            if isinstance(item, dict) and item.get("name") == asset_name
        ),
        None,
    )
    if not asset or not asset.get("browser_download_url"):
        raise RuntimeError(f"GitHub Release 缺少 Linux 安装包：{asset_name}")
    url = str(asset["browser_download_url"])
    expected_prefix = f"https://github.com/{GITHUB_REPOSITORY}/releases/download/"
    if not url.startswith(expected_prefix):
        raise RuntimeError("GitHub Release 下载地址未通过安全检查")
    if not confirm_graphical_update(latest):
        return False

    proxy_url = str(config.get("proxy_url") or "")
    proxy_handler = (
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        if proxy_url
        else urllib.request.ProxyHandler({})
    )
    opener = urllib.request.build_opener(proxy_handler)
    with tempfile.TemporaryDirectory(prefix="cws-codex-update-") as temporary:
        archive_path = Path(temporary) / asset_name
        request = urllib.request.Request(url, headers={"User-Agent": f"CWS-Codex-Linux/{VERSION}"})
        with opener.open(request, timeout=180) as response, archive_path.open("wb") as output:
            shutil.copyfileobj(response, output)
        digest = str(asset.get("digest") or "")
        if digest.startswith("sha256:"):
            actual = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            if actual != digest.removeprefix("sha256:").lower():
                raise RuntimeError("下载文件 SHA-256 校验失败")
        extract_root = Path(temporary) / "extracted"
        extract_root.mkdir()
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                target = (extract_root / member.name).resolve()
                if extract_root.resolve() not in target.parents and target != extract_root.resolve():
                    raise RuntimeError("Linux 更新包包含不安全路径")
            archive.extractall(extract_root)
        installer = next(extract_root.glob("CWS-Codex-Linux-v*/install.sh"), None)
        if not installer:
            raise RuntimeError("Linux 更新包缺少 install.sh")
        command = [
            str(installer),
            "--auto-update",
            "--skip-extension",
            "--broker-url",
            str(config["broker_url"]),
            "--codex-home",
            str(config["codex_home"]),
            "--client-home",
            str(config.get("client_home") or "~/.cws-codex"),
        ]
        command.extend(["--proxy-url", proxy_url] if proxy_url else ["--direct"])
        subprocess.run(command, check=True)
    return True


def find_vscode(configured: str = "") -> str | None:
    candidates = [configured, shutil.which("code"), shutil.which("code-insiders")]
    candidates.extend(("/usr/bin/code", "/snap/bin/code", "/usr/local/bin/code"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            # Do not resolve symlinks: /snap/bin/code points at the generic snap launcher,
            # which relies on argv[0] remaining "code".
            return str(Path(candidate).expanduser().absolute())
    return None


def make_auth_payload(lease: dict[str, Any]) -> dict[str, Any]:
    required = ("access_token", "account_id", "lease_id")
    if any(not lease.get(field) for field in required):
        raise RuntimeError("Broker 响应缺少官方 ChatGPT 登录字段")
    return {
        "auth_mode": "chatgptAuthTokens",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": str(lease["access_token"]),
            "access_token": str(lease["access_token"]),
            "refresh_token": "",
            "account_id": str(lease["account_id"]),
        },
        "last_refresh": datetime.now(timezone.utc).isoformat(),
    }


def synchronize(config_path: Path = CONFIG_PATH, *, new_lease: bool = False, quiet: bool = False) -> dict[str, Any]:
    config = read_config(config_path)
    token = read_device_token(config_path.parent / "device-token")
    codex_home = expanded_path(str(config["codex_home"]))
    client_home = client_home_from_config(config)
    initialize_auth_backup(codex_home, client_home)
    initialize_usage_baseline(codex_home, client_home)
    lease_path = client_home / "cws-lease.json"

    lease_id = None
    if not new_lease and lease_path.is_file():
        try:
            lease_id = json.loads(lease_path.read_text(encoding="utf-8")).get("lease_id")
        except (OSError, json.JSONDecodeError, AttributeError):
            lease_id = None
    body: dict[str, Any] = {"device_id": f"{socket.gethostname()}/{getpass.getuser()}"}
    client_ip = local_ipv4()
    if client_ip:
        body["client_ip"] = client_ip
    if lease_id:
        body["lease_id"] = str(lease_id)

    lease = request_json(
        str(config["broker_url"]).rstrip("/") + "/v1/lease",
        proxy_url=str(config.get("proxy_url") or ""),
        device_token=token,
        body=body,
    )
    auth_payload = make_auth_payload(lease)
    lease_payload = {
        "lease_id": str(lease["lease_id"]),
        "lease_expires_at": lease.get("lease_expires_at"),
        "account_alias": str(lease.get("account_alias") or ""),
        "access_token_expires_at": lease.get("access_token_expires_at"),
    }
    auth_path = codex_home / "auth.json"
    write_json_atomic(auth_path, auth_payload)
    write_json_atomic(lease_path, lease_payload)
    mark_managed_auth(auth_path, client_home)
    if not quiet:
        print("CWS Codex 凭据同步成功。")
        print(f"账号：{lease.get('account_alias', '')}；套餐：{lease.get('plan_type', '')}")
    return lease


def safe_token_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def usage_date(value: Any) -> str:
    try:
        timestamp = str(value or "").replace("Z", "+00:00")
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(CHINA_TIMEZONE).date().isoformat()
    except (TypeError, ValueError):
        return datetime.now(CHINA_TIMEZONE).date().isoformat()


def estimate_user_text_tokens(text: str) -> int:
    """Estimate plain user text locally without transmitting the message content."""
    tokens = 0
    ascii_run = 0
    for character in text:
        if character.isascii() and (character.isalnum() or character == "_"):
            ascii_run += 1
            continue
        if ascii_run:
            tokens += (ascii_run + 3) // 4
            ascii_run = 0
        if not character.isspace():
            tokens += 1
    if ascii_run:
        tokens += (ascii_run + 3) // 4
    return tokens


def empty_daily_usage(date: str) -> dict[str, Any]:
    return {"date": date, **{field: 0 for field in USAGE_FIELDS}}


def collect_session_usage(
    sessions_root: Path,
    excluded_session_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    if not sessions_root.is_dir():
        return sessions
    for path in sorted(sessions_root.rglob("*.jsonl")):
        session_id = session_id_for_name(path.name)
        if session_id in (excluded_session_ids or set()):
            continue
        latest: dict[str, Any] | None = None
        previous = {field: 0 for field in TOKEN_FIELDS}
        daily: dict[str, dict[str, Any]] = {}
        user_totals = {field: 0 for field in USER_INPUT_FIELDS}
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"token_count"' not in line and '"user_message"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                        payload = event.get("payload", {})
                        info = payload.get("info") or {}
                        total = info.get("total_token_usage")
                        if payload.get("type") == "token_count" and isinstance(total, dict):
                            latest = total
                            date = usage_date(event.get("timestamp"))
                            bucket = daily.setdefault(date, empty_daily_usage(date))
                            for field in TOKEN_FIELDS:
                                current = safe_token_count(total.get(field))
                                before = previous[field]
                                bucket[field] += current - before if current >= before else current
                                previous[field] = current
                        elif payload.get("type") == "user_message":
                            message = payload.get("message")
                            text = message if isinstance(message, str) else ""
                            characters = len(text)
                            estimated = estimate_user_text_tokens(text)
                            user_totals["user_message_count"] += 1
                            user_totals["user_text_characters"] += characters
                            user_totals["user_text_tokens_estimated"] += estimated
                            date = usage_date(event.get("timestamp"))
                            bucket = daily.setdefault(date, empty_daily_usage(date))
                            bucket["user_message_count"] += 1
                            bucket["user_text_characters"] += characters
                            bucket["user_text_tokens_estimated"] += estimated
                    except (json.JSONDecodeError, AttributeError):
                        continue
        except OSError:
            continue
        if latest is None and not user_totals["user_message_count"]:
            continue
        latest = latest or {}
        record: dict[str, Any] = {
            "session_id": session_id
        }
        record.update({field: safe_token_count(latest.get(field)) for field in TOKEN_FIELDS})
        record.update(user_totals)
        record["daily_usage"] = [daily[date] for date in sorted(daily)]
        sessions.append(record)
    return sessions


def report_usage(config_path: Path = CONFIG_PATH, *, quiet: bool = False) -> int:
    config = read_config(config_path)
    codex_home = expanded_path(str(config["codex_home"]))
    client_home = client_home_from_config(config)
    baseline_path = client_home / "usage-baseline.json"
    excluded: set[str] = set()
    if baseline_path.is_file():
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            excluded = {str(value) for value in baseline.get("excluded_session_ids", [])}
        except (json.JSONDecodeError, AttributeError) as exc:
            raise RuntimeError(f"CWS Codex usage baseline is invalid: {baseline_path}") from exc
    sessions = collect_session_usage(codex_home / "sessions", excluded)
    if not sessions:
        if not quiet:
            print("没有可上报的 Codex Token 累计记录。")
        return 0
    token = read_device_token(config_path.parent / "device-token")
    reported = 0
    for offset in range(0, len(sessions), 200):
        body: dict[str, Any] = {"sessions": sessions[offset : offset + 200]}
        client_ip = local_ipv4()
        if client_ip:
            body["client_ip"] = client_ip
        result = request_json(
            str(config["broker_url"]).rstrip("/") + "/v1/usage/report",
            proxy_url=str(config.get("proxy_url") or ""),
            device_token=token,
            body=body,
        )
        reported += int(result.get("accepted_sessions") or 0)
    if not quiet:
        print(f"已上报 {reported} 个 Codex 会话的累计 Token 记录。")
    return reported


def append_log(name: str, message: str) -> None:
    path = INSTALL_DIR / name
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} {message}\n")
    path.chmod(0o600)


def launch(config_path: Path = CONFIG_PATH) -> int:
    try:
        if check_for_update(config_path):
            os.execv(
                sys.executable,
                [sys.executable, str(INSTALL_DIR / "cws_codex.py"), "launch", "--config", str(config_path)],
            )
    except Exception as exc:
        append_log("launch.log", f"Client update check failed: {exc}")
    config = read_config(config_path)
    vscode = find_vscode(str(config.get("vscode_path") or ""))
    if not vscode:
        raise RuntimeError("未找到 Visual Studio Code，请先安装 VS Code")
    try:
        synchronize(config_path)
        append_log("launch.log", "Credential synchronization succeeded.")
    except Exception as exc:  # launch remains available for diagnostics and offline use
        append_log("launch.log", f"Credential synchronization failed: {exc}")
        print(f"警告：凭据同步失败，仍将启动 VS Code：{exc}", file=sys.stderr)

    if shutil.which("pgrep"):
        running = subprocess.run(
            ["pgrep", "-x", "code"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        if running:
            print("警告：VS Code 已在运行。首次切换公司凭据时请先完全退出 VS Code。", file=sys.stderr)

    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(expanded_path(str(config["codex_home"])))
    subprocess.Popen(
        [vscode],
        env=environment,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    append_log("launch.log", f"VS Code launch requested: {vscode}")
    return 0


def configure_proxy(config_path: Path, proxy: str | None, direct: bool) -> int:
    config = read_config(config_path)
    if direct:
        selected = ""
    elif proxy is not None:
        selected = proxy.strip()
    else:
        print("请选择员工端代理：")
        print("  1. 公司内网代理 192.168.2.38:7897（默认）")
        print("  2. 本机代理 127.0.0.1:7897")
        print("  3. 本机 Shadowrocket 127.0.0.1:1082")
        print("  4. 自定义 HTTP/Mixed 代理")
        print("  5. 直连（不使用代理）")
        choice = input("输入 1-5：").strip()
        choices = {
            "1": "http://192.168.2.38:7897",
            "2": "http://127.0.0.1:7897",
            "3": "http://127.0.0.1:1082",
            "5": "",
        }
        selected = input("输入完整代理地址：").strip() if choice == "4" else choices.get(choice, "")
        if choice not in {"1", "2", "3", "4", "5"}:
            raise RuntimeError("无效选择")
    config["proxy_url"] = selected
    write_json_atomic(config_path, config)
    print(f"代理配置已更新：{selected or '直连'}")
    return 0


def setup(
    config_path: Path,
    broker_url: str,
    proxy_url: str,
    codex_home: str,
    client_home: str,
    vscode_path: str,
    keep_existing_token: bool = False,
) -> int:
    private_directory(config_path.parent)
    token_path = config_path.parent / "device-token"
    keep_existing = keep_existing_token and token_path.is_file()
    if token_path.is_file():
        if not keep_existing_token:
            answer = input("检测到已保存的设备令牌，是否保留？[Y/n]：").strip().lower()
            keep_existing = not answer or answer.startswith("y")
    if not keep_existing:
        token = os.environ.get("CWS_CODEX_DEVICE_TOKEN") or getpass.getpass(
            "请输入管理员分配给本机的设备令牌："
        )
        if not token.startswith("cwsdt_") or len(token) < 30:
            raise RuntimeError("设备令牌格式错误")
        token_path.write_text(token.strip() + "\n", encoding="utf-8")
        token_path.chmod(0o600)
    config = {
        "broker_url": broker_url.rstrip("/"),
        "proxy_url": proxy_url,
        "codex_home": str(expanded_path(codex_home)),
        "client_home": str(expanded_path(client_home)),
        "vscode_path": vscode_path,
        "client_version": VERSION,
    }
    write_json_atomic(config_path, config)
    initialize_auth_backup(expanded_path(codex_home), expanded_path(client_home))
    initialize_usage_baseline(expanded_path(codex_home), expanded_path(client_home))
    return 0


def diagnose(config_path: Path) -> int:
    print("CWS Codex Ubuntu/Linux 员工端诊断")
    print(f"安装目录：{config_path.parent}")
    failures = 0
    try:
        config = read_config(config_path)
        print("[正常] 已读取配置文件")
        print(f"Broker：{config.get('broker_url', '')}")
        print(f"代理：{config.get('proxy_url') or '直连'}")
        print(f"CODEX_HOME：{config.get('codex_home', '')}")
        print(f"CWS 数据目录：{client_home_from_config(config)}")
    except Exception as exc:
        print(f"[失败] 配置文件：{exc}")
        return 1
    vscode = find_vscode(str(config.get("vscode_path") or ""))
    if vscode:
        print(f"[正常] VS Code：{vscode}")
    else:
        failures += 1
        print("[失败] 未找到 VS Code")
    try:
        read_device_token(config_path.parent / "device-token")
        mode = (config_path.parent / "device-token").stat().st_mode & 0o777
        if mode != 0o600:
            failures += 1
            print(f"[失败] 设备令牌权限应为 600，当前为 {mode:o}")
        else:
            print("[正常] 设备令牌存在且权限为 600")
    except Exception as exc:
        failures += 1
        print(f"[失败] 设备令牌：{exc}")
    try:
        health = request_json(
            str(config["broker_url"]).rstrip("/") + "/healthz",
            proxy_url=str(config.get("proxy_url") or ""),
            timeout=15,
        )
        print(f"[正常] Broker 可访问，账号数：{health.get('accounts', '')}")
    except Exception as exc:
        failures += 1
        print(f"[失败] Broker/代理连接：{exc}")
    try:
        synchronize(config_path, quiet=True)
        print("[正常] 设备令牌鉴权和凭据同步成功")
    except Exception as exc:
        failures += 1
        print(f"[失败] 凭据同步：{exc}")
    return 1 if failures else 0


def background(config_path: Path) -> int:
    synchronize(config_path, quiet=True)
    report_usage(config_path, quiet=True)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--version", action="version", version=VERSION)
    subparsers = result.add_subparsers(dest="command", required=True)
    for name in ("sync", "report", "launch", "diagnose", "background", "restore-auth"):
        command = subparsers.add_parser(name)
        command.add_argument("--config", type=Path, default=CONFIG_PATH)
        if name == "sync":
            command.add_argument("--new-lease", action="store_true")
    configure = subparsers.add_parser("configure-proxy")
    configure.add_argument("--config", type=Path, default=CONFIG_PATH)
    selection = configure.add_mutually_exclusive_group()
    selection.add_argument("--proxy")
    selection.add_argument("--direct", action="store_true")
    setup_parser = subparsers.add_parser("setup")
    setup_parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    setup_parser.add_argument("--broker-url", required=True)
    setup_parser.add_argument("--proxy-url", default="")
    setup_parser.add_argument("--codex-home", required=True)
    setup_parser.add_argument("--client-home", required=True)
    setup_parser.add_argument("--vscode-path", required=True)
    setup_parser.add_argument("--keep-existing-token", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "sync":
            synchronize(args.config, new_lease=args.new_lease)
            return 0
        if args.command == "report":
            report_usage(args.config)
            return 0
        if args.command == "launch":
            return launch(args.config)
        if args.command == "diagnose":
            return diagnose(args.config)
        if args.command == "background":
            return background(args.config)
        if args.command == "restore-auth":
            if restore_original_auth(args.config):
                print("已恢复安装前的 Codex 登录凭据。")
            return 0
        if args.command == "configure-proxy":
            return configure_proxy(args.config, args.proxy, args.direct)
        if args.command == "setup":
            return setup(
                args.config,
                args.broker_url,
                args.proxy_url,
                args.codex_home,
                args.client_home,
                args.vscode_path,
                args.keep_existing_token,
            )
    except (RuntimeError, OSError, KeyError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
