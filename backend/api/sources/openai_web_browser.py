import asyncio
import re
import time
import uuid
from typing import Any, AsyncIterator

from api.exceptions import InvalidParamsException, OpenaiWebException
from utils.logger import get_logger


logger = get_logger(__name__)

_CONVERSATION_URL_RE = re.compile(
    r"/c/(?P<conversation_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:[/?#]|$)"
)


def conversation_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = _CONVERSATION_URL_RE.search(url)
    return match.group("conversation_id") if match else None


def make_text_stream_event(
    *,
    conversation_id: str,
    message_id: str,
    parent_message_id: str,
    text: str,
    model_slug: str,
    finished: bool,
) -> dict[str, Any]:
    """Build the subset of a ChatGPT Web stream event consumed by CWS."""
    return {
        "conversation_id": conversation_id,
        "message": {
            "id": message_id,
            "author": {"role": "assistant"},
            "create_time": time.time(),
            "content": {"content_type": "text", "parts": [text]},
            "metadata": {
                "model_slug": model_slug,
                "message_type": "next",
            },
            "status": "finished_successfully" if finished else "in_progress",
            "end_turn": finished,
            "weight": 1.0,
            "recipient": "all",
        },
        "parent": parent_message_id,
        "children": [],
    }


class ChatGPTBrowserBridge:
    """Drive a real, user-authenticated ChatGPT Chromium tab over CDP.

    The bridge intentionally does not read browser cookies or implement Sentinel,
    Turnstile, Arkose, or proof-token solvers. ChatGPT's own page performs those
    steps. CWS only queues requests and operates the visible composer.
    """

    _ASSISTANT_SELECTOR = '[data-message-author-role="assistant"]'
    _LOGIN_SELECTOR = '[data-testid="login-button"]'
    _COMPOSER_SELECTORS = (
        "#prompt-textarea",
        '[data-testid="prompt-textarea"]',
        'textarea[aria-label*="ChatGPT"]',
    )
    _SEND_SELECTORS = (
        '[data-testid="send-button"]',
        'button[aria-label*="Send"]',
        'button[aria-label*="发送"]',
    )
    _STOP_SELECTORS = (
        '[data-testid="stop-button"]',
        'button[aria-label*="Stop"]',
        'button[aria-label*="停止"]',
    )

    def __init__(self, settings):
        self.settings = settings
        self._playwright = None
        self._browser = None
        self._page = None
        self._connect_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._stale = False

    @property
    def connected(self) -> bool:
        return self._page is not None and not self._stale

    def mark_stale(self) -> None:
        self._stale = True

    async def startup(self) -> None:
        await self._ensure_page()

    async def shutdown(self) -> None:
        # Do not close the externally managed browser or its persistent profile.
        self._page = None
        self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _ensure_page(self):
        if self.connected:
            try:
                await self._page.title()
                return self._page
            except Exception:
                self._stale = True

        async with self._connect_lock:
            if self.connected:
                return self._page
            try:
                from playwright.async_api import async_playwright

                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    self.settings.browser_cdp_url,
                    timeout=self.settings.common_timeout * 1000,
                )
                if not self._browser.contexts:
                    raise RuntimeError("CDP browser has no persistent context")
                context = self._browser.contexts[0]
                chatgpt_origin = self.settings.browser_chatgpt_url.rstrip("/")
                self._page = next(
                    (page for page in context.pages if page.url.startswith(chatgpt_origin)),
                    None,
                )
                if self._page is None:
                    self._page = await context.new_page()
                    await self._page.goto(
                        f"{chatgpt_origin}/",
                        wait_until="domcontentloaded",
                        timeout=self.settings.common_timeout * 1000,
                    )
                self._stale = False
                return self._page
            except OpenaiWebException:
                raise
            except Exception as exc:
                self._stale = True
                raise OpenaiWebException(
                    message=(
                        f"Cannot connect to the ChatGPT browser at "
                        f"{self.settings.browser_cdp_url}: {exc}"
                    ),
                    code=503,
                ) from exc

    async def _is_logged_in(self, page) -> bool:
        composer = await self._first_visible(page, self._COMPOSER_SELECTORS)
        if composer is not None:
            return True
        login_button = page.locator(self._LOGIN_SELECTOR).first
        if await login_button.count() and await login_button.is_visible():
            return False
        # Some logged-out or challenged page states omit the login button. A
        # same-origin status check avoids treating "button absent" as auth.
        return await page.evaluate(
            """
            async () => {
              try {
                const response = await fetch('/backend-api/me', {
                  method: 'GET',
                  credentials: 'include',
                });
                return response.ok;
              } catch (_) {
                return false;
              }
            }
            """
        )

    async def require_login(self) -> None:
        page = await self._ensure_page()
        if not await self._is_logged_in(page):
            raise OpenaiWebException(
                message=(
                    "The connected Chromium profile is not signed in to ChatGPT. "
                    "Open the profile, sign in, and retry."
                ),
                code=401,
            )

    async def health(self) -> dict[str, Any]:
        page = await self._ensure_page()
        return {
            "connected": True,
            "logged_in": await self._is_logged_in(page),
            "url": page.url,
        }

    async def quota_hint(self) -> str | None:
        """Read the quota/reset hint currently exposed by ChatGPT's model menu.

        ChatGPT does not always expose a numeric remaining count. The returned
        text is deliberately kept as a remote hint instead of being parsed into
        a fabricated authoritative balance.
        """
        async with self._operation_lock:
            page = await self._ensure_page()
            await self.require_login()
            button = page.locator('[data-testid="model-switcher-dropdown-button"]').first
            if not await button.count() or not await button.is_visible():
                return None
            await button.click()
            try:
                await asyncio.sleep(0.2)
                selectors = (
                    '[role="menu"]:visible',
                    '[role="listbox"]:visible',
                    '[data-radix-menu-content]:visible',
                )
                chunks = []
                for selector in selectors:
                    locators = page.locator(selector)
                    for index in range(await locators.count()):
                        text = (await locators.nth(index).inner_text()).strip()
                        if text and text not in chunks:
                            chunks.append(text)
                if not chunks:
                    return None
                return "\n\n".join(chunks)[:4000]
            finally:
                await page.keyboard.press("Escape")

    async def _goto(self, path: str) -> Any:
        page = await self._ensure_page()
        target = f"{self.settings.browser_chatgpt_url.rstrip('/')}{path}"
        if page.url.rstrip("/") != target.rstrip("/"):
            await page.goto(
                target,
                wait_until="domcontentloaded",
                timeout=self.settings.common_timeout * 1000,
            )
        await self.require_login()
        return page

    @staticmethod
    async def _first_visible(page, selectors: tuple[str, ...]):
        for selector in selectors:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                return locator
        return None

    @staticmethod
    async def _message_id_from_locator(locator) -> str | None:
        return await locator.evaluate(
            """
            element => {
              const turn = element.closest('[data-testid^="conversation-turn-"]');
              const withId = element.closest('[data-message-id]') ||
                turn?.querySelector('[data-message-id]');
              return withId?.getAttribute('data-message-id') || null;
            }
            """
        )

    async def _select_model(self, page, model_name: str) -> None:
        label = self.settings.browser_model_labels.get(model_name, "")
        if not label:
            return
        button = page.locator('[data-testid="model-switcher-dropdown-button"]').first
        if not await button.count():
            raise OpenaiWebException(
                message="ChatGPT model switcher was not found; clear the model label or update the selector.",
                code=503,
            )
        await button.click()
        option = page.get_by_text(label, exact=True).last
        try:
            await option.wait_for(state="visible", timeout=self.settings.common_timeout * 1000)
            await option.click()
        except Exception as exc:
            raise OpenaiWebException(
                message=f"ChatGPT model option '{label}' is not available in the connected account.",
                code=400,
            ) from exc

    async def _current_conversation_id(self, page) -> str | None:
        conversation_id = conversation_id_from_url(page.url)
        if conversation_id:
            return conversation_id
        try:
            await page.wait_for_url(
                re.compile(r".*/c/[0-9a-fA-F-]+(?:[/?#].*)?$"),
                timeout=self.settings.common_timeout * 1000,
            )
        except Exception:
            return conversation_id_from_url(page.url)
        return conversation_id_from_url(page.url)

    async def complete(
        self,
        *,
        model_name: str,
        model_slug: str,
        text_content: str,
        conversation_id: str | None,
        parent_message_id: str | None,
    ) -> AsyncIterator[dict[str, Any]]:
        async with self._operation_lock:
            async for event in self._complete_locked(
                model_name=model_name,
                model_slug=model_slug,
                text_content=text_content,
                conversation_id=conversation_id,
                parent_message_id=parent_message_id,
            ):
                yield event

    async def _complete_locked(
        self,
        *,
        model_name: str,
        model_slug: str,
        text_content: str,
        conversation_id: str | None,
        parent_message_id: str | None,
    ) -> AsyncIterator[dict[str, Any]]:
        if text_content == ":continue":
            raise InvalidParamsException("Browser transport does not support :continue yet")

        path = f"/c/{conversation_id}" if conversation_id else "/"
        page = await self._goto(path)
        await self._select_model(page, model_name)

        assistant_messages = page.locator(self._ASSISTANT_SELECTOR)
        previous_count = await assistant_messages.count()
        composer = await self._first_visible(page, self._COMPOSER_SELECTORS)
        if composer is None:
            raise OpenaiWebException(
                message="ChatGPT composer was not found. The page layout may have changed.",
                code=503,
            )
        await composer.fill(text_content)
        send_button = await self._first_visible(page, self._SEND_SELECTORS)
        if send_button is not None:
            await send_button.click()
        else:
            await composer.press("Enter")

        deadline = asyncio.get_running_loop().time() + self.settings.ask_timeout
        stable_since = None
        last_text = ""
        message_id = None
        resolved_conversation_id = str(conversation_id) if conversation_id else None
        parent_id = parent_message_id or str(uuid.uuid4())

        user_messages = page.locator('[data-message-author-role="user"]')
        if await user_messages.count():
            parent_id = (
                await self._message_id_from_locator(user_messages.last)
            ) or parent_id

        while asyncio.get_running_loop().time() < deadline:
            count = await assistant_messages.count()
            if count > previous_count:
                latest = assistant_messages.nth(count - 1)
                text = (await latest.inner_text()).strip()
                if text and text != last_text:
                    last_text = text
                    stable_since = asyncio.get_running_loop().time()
                    if message_id is None:
                        message_id = (
                            await self._message_id_from_locator(latest)
                        ) or str(uuid.uuid4())
                    if resolved_conversation_id is None:
                        resolved_conversation_id = await self._current_conversation_id(page)
                    if resolved_conversation_id:
                        yield make_text_stream_event(
                            conversation_id=resolved_conversation_id,
                            message_id=message_id,
                            parent_message_id=parent_id,
                            text=last_text,
                            model_slug=model_slug,
                            finished=False,
                        )

            stop_button = await self._first_visible(page, self._STOP_SELECTORS)
            if last_text and stop_button is None:
                if stable_since is None:
                    stable_since = asyncio.get_running_loop().time()
                if asyncio.get_running_loop().time() - stable_since >= self.settings.browser_stable_seconds:
                    if resolved_conversation_id is None:
                        resolved_conversation_id = await self._current_conversation_id(page)
                    if not resolved_conversation_id:
                        raise OpenaiWebException(
                            message="ChatGPT replied but no conversation id appeared in the page URL.",
                            code=503,
                        )
                    yield make_text_stream_event(
                        conversation_id=resolved_conversation_id,
                        message_id=message_id or str(uuid.uuid4()),
                        parent_message_id=parent_id,
                        text=last_text,
                        model_slug=model_slug,
                        finished=True,
                    )
                    return

            await asyncio.sleep(self.settings.browser_poll_interval_ms / 1000)

        raise OpenaiWebException(
            message="Timed out while waiting for the ChatGPT browser response.",
            code=504,
        )

    async def same_origin_fetch(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> Any:
        page = await self._ensure_page()
        chatgpt_origin = self.settings.browser_chatgpt_url.rstrip("/")
        if not page.url.startswith(chatgpt_origin):
            page = await self._goto("/")
        await self.require_login()
        result = await page.evaluate(
            """
            async ({path, method, body}) => {
              const response = await fetch(path, {
                method,
                credentials: 'include',
                headers: body ? {'Content-Type': 'application/json'} : undefined,
                body: body ? JSON.stringify(body) : undefined,
              });
              const text = await response.text();
              return {status: response.status, text};
            }
            """,
            {"path": path, "method": method, "body": body},
        )
        if result["status"] < 200 or result["status"] >= 300:
            raise OpenaiWebException(message=result["text"], code=result["status"])
        if not result["text"]:
            return None
        try:
            import json

            return json.loads(result["text"])
        except ValueError:
            return result["text"]
