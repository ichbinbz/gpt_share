import asyncio
import base64
import json
import time
from pathlib import Path

import httpx

from scripts.codex_live_probe import emit_summaries, run_validation


def access_token(account_id: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {"exp": int(time.time()) + 3600, "chatgpt_account_id": account_id}
        ).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{payload}.signature"


def write_auth(accounts_dir: Path, alias: str, account_id: str) -> None:
    auth_path = accounts_dir / alias / "auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": access_token(account_id),
                    "refresh_token": "must-never-print",
                    "account_id": account_id,
                }
            }
        ),
        encoding="utf-8",
    )


def test_live_probe_emits_only_sanitized_fields_and_validates_expected_outcomes(
    tmp_path: Path, capsys
):
    accounts_dir = tmp_path / "accounts"
    write_auth(accounts_dir, "healthy", "account-healthy")
    write_auth(accounts_dir, "chatgpt024", "account-capacity")

    def handler(request: httpx.Request) -> httpx.Response:
        account_id = request.headers["ChatGPT-Account-Id"]
        if request.method == "GET":
            return httpx.Response(
                200,
                request=request,
                json={
                    "models": [
                        {"slug": "gpt-test", "supports_text": True, "available": True}
                    ]
                },
            )
        if account_id == "account-healthy":
            return httpx.Response(
                200,
                request=request,
                json={"output": [{"content": "SENSITIVE_RESPONSE_BODY"}]},
            )
        return httpx.Response(
            503,
            request=request,
            json={
                "error": {
                    "code": "server_overloaded",
                    "message": "SENSITIVE_CAPACITY_BODY",
                }
            },
        )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_validation(
                accounts_dir,
                healthy_alias="healthy",
                capacity_alias="chatgpt024",
                timeout=5,
                client=client,
            )

    valid, summaries = asyncio.run(exercise())
    emit_summaries(summaries)

    assert valid
    assert summaries == [
        {
            "alias": "healthy",
            "model": "gpt-test",
            "status": "ok",
            "classification": "available",
        },
        {
            "alias": "chatgpt024",
            "model": "gpt-test",
            "status": "server_overloaded",
            "classification": "capacity",
        },
    ]
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == summaries
    assert all(
        set(line) == {"alias", "model", "status", "classification"}
        for line in lines
    )
    rendered = json.dumps(lines)
    assert "must-never-print" not in rendered
    assert "SENSITIVE_RESPONSE_BODY" not in rendered
    assert "SENSITIVE_CAPACITY_BODY" not in rendered


def test_live_probe_fails_closed_with_sanitized_discovery_result(tmp_path: Path):
    accounts_dir = tmp_path / "accounts"
    write_auth(accounts_dir, "healthy", "account-healthy")
    write_auth(accounts_dir, "chatgpt024", "account-capacity")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            request=request,
            text="SENSITIVE_DISCOVERY_BODY",
        )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_validation(
                accounts_dir,
                healthy_alias="healthy",
                capacity_alias="chatgpt024",
                timeout=5,
                client=client,
            )

    valid, summaries = asyncio.run(exercise())

    assert not valid
    assert summaries == [
        {
            "alias": "healthy",
            "model": "<discovery>",
            "status": "failed",
            "classification": "probe_error",
        },
        {
            "alias": "chatgpt024",
            "model": "<discovery>",
            "status": "failed",
            "classification": "probe_error",
        },
    ]
    assert "SENSITIVE_DISCOVERY_BODY" not in json.dumps(summaries)
