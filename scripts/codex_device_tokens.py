#!/usr/bin/env python3
"""Create, list, rotate, and revoke per-device CWS Codex broker tokens."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REGISTRY = Path("/var/lib/cws-codex/device-tokens.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_registry(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "devices": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("devices"), list):
        raise ValueError(f"invalid device token registry: {path}")
    return payload


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def matching_devices(registry: dict[str, Any], label: str) -> list[dict[str, Any]]:
    return [
        device
        for device in registry["devices"]
        if isinstance(device, dict) and device.get("label") == label
    ]


def find_device(registry: dict[str, Any], label: str) -> dict[str, Any] | None:
    devices = matching_devices(registry, label)
    return devices[-1] if devices else None


def revoke_active_devices(registry: dict[str, Any], label: str) -> int:
    revoked_at = now_iso()
    count = 0
    for device in matching_devices(registry, label):
        if device.get("enabled"):
            device["enabled"] = False
            device["revoked_at"] = revoked_at
            count += 1
    return count


def create_token(registry: dict[str, Any], label: str, rotate: bool = False) -> str:
    existing = matching_devices(registry, label)
    if existing and not rotate:
        raise ValueError(f"device label already exists: {label}; use rotate instead")
    if existing:
        revoke_active_devices(registry, label)
    token = "cwsdt_" + secrets.token_urlsafe(32)
    registry["devices"].append(
        {
            "id": str(uuid.uuid4()),
            "label": label,
            "token_hash": token_hash(token),
            "enabled": True,
            "created_at": now_iso(),
        }
    )
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "rotate", "revoke"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("label")
    subparsers.add_parser("list")
    args = parser.parse_args()

    lock_path = args.registry.with_suffix(args.registry.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry = load_registry(args.registry)
        if args.command == "list":
            for device in registry["devices"]:
                print(
                    json.dumps(
                        {
                            "id": device.get("id"),
                            "label": device.get("label"),
                            "enabled": bool(device.get("enabled")),
                            "created_at": device.get("created_at"),
                            "revoked_at": device.get("revoked_at"),
                        },
                        ensure_ascii=False,
                    )
                )
            return 0

        if args.command in ("create", "rotate"):
            token = create_token(registry, args.label, rotate=args.command == "rotate")
            atomic_write(args.registry, registry)
            print("Device token (shown once; give it only to this employee device):")
            print(token)
            return 0

        if revoke_active_devices(registry, args.label) == 0:
            raise SystemExit(f"active device label not found: {args.label}")
        atomic_write(args.registry, registry)
        print(f"Revoked device token: {args.label}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
