#!/usr/bin/env python3
"""Run a credential-safe live Codex model protocol validation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, TextIO

import httpx

try:
    from scripts.codex_plus_broker import (
        AccountRecord,
        DEFAULT_ACCOUNTS_DIR,
        DEFAULT_CODEX_MODELS_URL,
        DEFAULT_CODEX_RESPONSES_URL,
    )
except ModuleNotFoundError as exc:
    if exc.name != "scripts":
        raise
    from codex_plus_broker import (  # type: ignore[no-redef]
        AccountRecord,
        DEFAULT_ACCOUNTS_DIR,
        DEFAULT_CODEX_MODELS_URL,
        DEFAULT_CODEX_RESPONSES_URL,
    )


ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
STATUS_PATTERN = re.compile(r"^[a-z0-9_.-]{1,80}$")


def _summary(alias: str, model: str, result: dict[str, Any]) -> dict[str, str]:
    classification = result.get("status")
    if not isinstance(classification, str):
        classification = "probe_error"
    if classification == "available":
        status = "ok"
    else:
        error_code = result.get("error_code")
        if isinstance(error_code, str) and STATUS_PATTERN.fullmatch(error_code):
            status = error_code
        elif isinstance(result.get("http_status"), int):
            status = f"http_{result['http_status']}"
        else:
            status = "failed"
    return {
        "alias": alias,
        "model": model,
        "status": status,
        "classification": classification,
    }


async def _probe_account(
    account: AccountRecord, client: httpx.AsyncClient, timeout: int
) -> tuple[list[dict[str, str]], bool]:
    try:
        payload = await account.ensure_fresh(client, 900)
        models = await account.discover_models(client, payload, timeout)
    except Exception:
        return (
            [
                {
                    "alias": account.alias,
                    "model": "<discovery>",
                    "status": "failed",
                    "classification": "probe_error",
                }
            ],
            False,
        )

    summaries = []
    for model in models:
        result = await account.probe_model(client, payload, model, timeout)
        summaries.append(_summary(account.alias, model, result))
    return summaries, True


async def run_validation(
    accounts_dir: Path,
    *,
    healthy_alias: str,
    capacity_alias: str,
    timeout: int,
    client: httpx.AsyncClient,
    models_url: str = DEFAULT_CODEX_MODELS_URL,
    responses_url: str = DEFAULT_CODEX_RESPONSES_URL,
) -> tuple[bool, list[dict[str, str]]]:
    for alias in (healthy_alias, capacity_alias):
        if not ALIAS_PATTERN.fullmatch(alias):
            raise ValueError("account aliases may contain only letters, numbers, dot, underscore, and dash")

    summaries: list[dict[str, str]] = []
    discovery_ok = True
    for alias in (healthy_alias, capacity_alias):
        account = AccountRecord(
            alias,
            accounts_dir / alias / "auth.json",
            models_url=models_url,
            responses_url=responses_url,
        )
        account_summaries, account_discovery_ok = await _probe_account(
            account, client, timeout
        )
        summaries.extend(account_summaries)
        discovery_ok = discovery_ok and account_discovery_ok

    healthy_ok = any(
        item["alias"] == healthy_alias and item["classification"] == "available"
        for item in summaries
    )
    capacity_ok = any(
        item["alias"] == capacity_alias
        and item["status"] == "server_overloaded"
        and item["classification"] == "capacity"
        for item in summaries
    )
    return discovery_ok and healthy_ok and capacity_ok, summaries


def emit_summaries(
    summaries: list[dict[str, str]], stream: TextIO | None = None
) -> None:
    output = stream or sys.stdout
    for summary in summaries:
        output.write(json.dumps(summary, ensure_ascii=True, separators=(",", ":")) + "\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--accounts-dir",
        type=Path,
        default=Path(os.getenv("CWS_CODEX_ACCOUNTS_DIR", DEFAULT_ACCOUNTS_DIR)),
    )
    parser.add_argument("--healthy-alias", required=True)
    parser.add_argument("--capacity-alias", default="chatgpt024")
    parser.add_argument("--timeout", type=int, default=45, choices=range(5, 121))
    parser.add_argument(
        "--models-url",
        default=os.getenv("CWS_CODEX_MODELS_URL", DEFAULT_CODEX_MODELS_URL),
    )
    parser.add_argument(
        "--responses-url",
        default=os.getenv("CWS_CODEX_RESPONSES_URL", DEFAULT_CODEX_RESPONSES_URL),
    )
    return parser.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(timeout=float(args.timeout)) as client:
        valid, summaries = await run_validation(
            args.accounts_dir,
            healthy_alias=args.healthy_alias,
            capacity_alias=args.capacity_alias,
            timeout=args.timeout,
            client=client,
            models_url=args.models_url,
            responses_url=args.responses_url,
        )
    emit_summaries(summaries)
    return 0 if valid else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
