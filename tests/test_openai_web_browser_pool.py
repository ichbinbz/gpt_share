import asyncio

import pytest

from api.conf.config import ConfigModel
from api.exceptions import OpenaiWebException
from api.sources.openai_web_browser_pool import ChatGPTBrowserPool


def make_pool(*, quota_limit=10):
    settings = ConfigModel.model_validate({
        "openai_web": {
            "transport": "browser",
            "max_completion_concurrency": 2,
            "browser_accounts": [
                {
                    "id": "plus-a",
                    "name": "A",
                    "cdp_url": "http://127.0.0.1:9222",
                    "quota_limits": {"chatgpt_auto": quota_limit},
                    "quota_window_seconds": {"chatgpt_auto": 3600},
                },
                {
                    "id": "plus-b",
                    "name": "B",
                    "cdp_url": "http://127.0.0.1:9223",
                    "quota_limits": {"chatgpt_auto": quota_limit},
                    "quota_window_seconds": {"chatgpt_auto": 3600},
                },
            ],
        }
    }).openai_web
    return ChatGPTBrowserPool(settings)


def test_pool_balances_successful_requests_and_preserves_affinity():
    async def run():
        pool = make_pool()
        async with pool.lease(preferred_account_id=None, model_name="chatgpt_auto") as first:
            first_id = first.account_id
        async with pool.lease(preferred_account_id=None, model_name="chatgpt_auto") as second:
            second_id = second.account_id
        async with pool.lease(preferred_account_id=first_id, model_name="chatgpt_auto") as affine:
            affine_id = affine.account_id
        return first_id, second_id, affine_id

    first_id, second_id, affine_id = asyncio.run(run())
    assert first_id != second_id
    assert affine_id == first_id


def test_pool_rejects_new_requests_when_configured_quota_is_exhausted():
    async def run():
        pool = make_pool(quota_limit=1)
        async with pool.lease(preferred_account_id=None, model_name="chatgpt_auto"):
            pass
        async with pool.lease(preferred_account_id=None, model_name="chatgpt_auto"):
            pass
        with pytest.raises(OpenaiWebException) as exc_info:
            async with pool.lease(preferred_account_id=None, model_name="chatgpt_auto"):
                pass
        return exc_info.value.code

    assert asyncio.run(run()) == 429


def test_pool_status_labels_remaining_as_local_estimate():
    async def run():
        pool = make_pool(quota_limit=3)
        async with pool.lease(preferred_account_id="plus-a", model_name="chatgpt_auto"):
            pass
        return await pool.status()

    status = asyncio.run(run())
    plus_a = next(account for account in status["accounts"] if account["id"] == "plus-a")
    quota = plus_a["quota"]["chatgpt_auto"]
    assert quota["remaining_estimate"] == 2
    assert quota["source"] == "local_cws_estimate"


def test_affined_conversation_does_not_move_after_quota_exhaustion():
    async def run():
        pool = make_pool(quota_limit=1)
        async with pool.lease(preferred_account_id="plus-a", model_name="chatgpt_auto"):
            pass

        with pytest.raises(OpenaiWebException, match="cannot move to another account") as exc_info:
            async with pool.lease(preferred_account_id="plus-a", model_name="chatgpt_auto"):
                pass
        assert exc_info.value.code == 429

    asyncio.run(run())
