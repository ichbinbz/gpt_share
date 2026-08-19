import asyncio
import json
from types import SimpleNamespace

from api.sources.openai_web_browser import (
    ChatGPTBrowserBridge,
    conversation_id_from_url,
    make_text_stream_event,
)


CONVERSATION_ID = "123e4567-e89b-42d3-a456-426614174000"


def make_settings():
    return SimpleNamespace(
        browser_cdp_url="http://127.0.0.1:9222",
        browser_chatgpt_url="https://chatgpt.com",
        browser_model_labels={"chatgpt_auto": ""},
        browser_poll_interval_ms=100,
        browser_stable_seconds=0.5,
        common_timeout=2,
        ask_timeout=2,
    )


def test_conversation_id_from_url():
    assert conversation_id_from_url(f"https://chatgpt.com/c/{CONVERSATION_ID}") == CONVERSATION_ID
    assert conversation_id_from_url(f"https://chatgpt.com/c/{CONVERSATION_ID}?model=auto") == CONVERSATION_ID
    assert conversation_id_from_url("https://chatgpt.com/") is None
    assert conversation_id_from_url("https://example.com/c/not-a-uuid") is None


def test_make_text_stream_event_matches_cws_shape():
    event = make_text_stream_event(
        conversation_id=CONVERSATION_ID,
        message_id="223e4567-e89b-42d3-a456-426614174000",
        parent_message_id="323e4567-e89b-42d3-a456-426614174000",
        text="hello",
        model_slug="auto",
        finished=True,
    )
    assert event["conversation_id"] == CONVERSATION_ID
    assert event["message"]["author"]["role"] == "assistant"
    assert event["message"]["content"] == {"content_type": "text", "parts": ["hello"]}
    assert event["message"]["metadata"]["model_slug"] == "auto"
    assert event["message"]["end_turn"] is True


class FakeLocator:
    def __init__(self, count):
        self._count = count

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._count > 0

    @property
    def first(self):
        return self


class FakePage:
    def __init__(self, *, logged_in=True, response=None):
        self.url = "https://chatgpt.com/"
        self._logged_in = logged_in
        self._response = response or {"status": 200, "text": "{}"}

    async def title(self):
        return "ChatGPT"

    def locator(self, selector):
        if selector == ChatGPTBrowserBridge._LOGIN_SELECTOR:
            return FakeLocator(0 if self._logged_in else 1)
        if selector in ChatGPTBrowserBridge._COMPOSER_SELECTORS:
            return FakeLocator(1 if self._logged_in else 0)
        raise AssertionError(f"Unexpected selector {selector}")

    async def evaluate(self, _script, payload=None):
        if payload is None:
            return self._logged_in
        assert payload["path"].startswith("/backend-api/")
        return self._response


def test_health_reports_login_state_without_reading_cookies():
    bridge = ChatGPTBrowserBridge(make_settings())
    bridge._page = FakePage(logged_in=True)
    assert asyncio.run(bridge.health()) == {
        "connected": True,
        "logged_in": True,
        "url": "https://chatgpt.com/",
    }


def test_health_does_not_treat_missing_login_button_as_authenticated():
    bridge = ChatGPTBrowserBridge(make_settings())
    bridge._page = FakePage(logged_in=False)
    assert asyncio.run(bridge.health())["logged_in"] is False


def test_same_origin_fetch_decodes_json():
    bridge = ChatGPTBrowserBridge(make_settings())
    bridge._page = FakePage(
        logged_in=True,
        response={"status": 200, "text": json.dumps({"items": [1, 2]})},
    )
    result = asyncio.run(bridge.same_origin_fetch("/backend-api/conversations"))
    assert result == {"items": [1, 2]}
