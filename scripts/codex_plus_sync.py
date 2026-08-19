#!/usr/bin/env python3
"""Synchronize a leased official ChatGPT token into an isolated CODEX_HOME."""

from __future__ import annotations

import argparse
import json
import os
import platform
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_BROKER_URL = "http://codex.cws.internal:8765"
DEFAULT_PROXY_URL = "http://192.168.2.38:7897"


def atomic_write_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, mode)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def codex_auth_payload(access_token: str, account_id: str) -> dict[str, Any]:
    """Return Codex's official externally-managed ChatGPT auth representation."""
    return {
        "auth_mode": "chatgptAuthTokens",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": access_token,
            "access_token": access_token,
            "refresh_token": "",
            "account_id": account_id,
        },
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def request_lease(
    broker_url: str,
    device_token: str,
    device_id: str,
    lease_id: str | None,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"device_id": device_id}
    if lease_id:
        body["lease_id"] = lease_id
    request = urllib.request.Request(
        broker_url.rstrip("/") + "/v1/lease",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {device_token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        if proxy_url:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
            response_context = opener.open(request, timeout=30)
        else:
            response_context = urllib.request.urlopen(request, timeout=30)
        with response_context as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"broker returned HTTP {exc.code}: {detail}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("broker returned a non-object")
    return payload


def load_lease_id(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    value = payload.get("lease_id") if isinstance(payload, dict) else None
    return value if isinstance(value, str) and value else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--broker-url",
        default=os.getenv("CWS_CODEX_BROKER_URL", DEFAULT_BROKER_URL),
    )
    parser.add_argument("--device-token", default=os.getenv("CWS_CODEX_DEVICE_TOKEN"))
    parser.add_argument(
        "--proxy-url",
        default=os.getenv("CWS_CODEX_PROXY_URL", DEFAULT_PROXY_URL),
        help=f"HTTP or mixed proxy (default: {DEFAULT_PROXY_URL})",
    )
    parser.add_argument("--direct", action="store_true", help="Do not use an HTTP proxy")
    parser.add_argument("--device-id", default=platform.node() or "unknown-device")
    parser.add_argument("--codex-home", type=Path, default=Path(os.getenv("CWS_CODEX_HOME", "~/.cws-codex")).expanduser())
    parser.add_argument("--new-lease", action="store_true", help="Discard session affinity and choose the best account again")
    parser.add_argument("--watch", action="store_true", help="Keep the official access token synchronized")
    parser.add_argument("--interval", type=int, default=300, help="Watch interval in seconds (default: 300)")
    args = parser.parse_args()

    if not args.broker_url:
        parser.error("--broker-url or CWS_CODEX_BROKER_URL is required")
    if not args.device_token:
        parser.error("--device-token or CWS_CODEX_DEVICE_TOKEN is required")

    if args.interval < 30:
        parser.error("--interval must be at least 30 seconds")

    lease_state = args.codex_home / "cws-lease.json"
    first_sync = True
    while True:
        lease_id = None if first_sync and args.new_lease else load_lease_id(lease_state)
        lease = request_lease(
            args.broker_url,
            args.device_token,
            args.device_id,
            lease_id,
            None if args.direct else args.proxy_url,
        )
        access_token = lease.get("access_token")
        account_id = lease.get("account_id")
        if not isinstance(access_token, str) or not access_token or not isinstance(account_id, str) or not account_id:
            raise RuntimeError("broker response is missing official ChatGPT authentication fields")

        atomic_write_json(args.codex_home / "auth.json", codex_auth_payload(access_token, account_id))
        atomic_write_json(
            lease_state,
            {
                "lease_id": lease.get("lease_id"),
                "lease_expires_at": lease.get("lease_expires_at"),
                "account_alias": lease.get("account_alias"),
                "access_token_expires_at": lease.get("access_token_expires_at"),
            },
        )
        print(f"Codex ChatGPT lease synchronized to {args.codex_home}")
        print(f"Account alias: {lease.get('account_alias', 'unknown')}; plan: {lease.get('plan_type', 'unknown')}")
        if not args.watch:
            print(f"Launch VS Code with CODEX_HOME={args.codex_home}")
            break
        first_sync = False
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
