import asyncio
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from api.exceptions import InvalidParamsException, OpenaiWebException
from api.sources.openai_web_browser import ChatGPTBrowserBridge


@dataclass
class BrowserAccountRuntime:
    account_id: str
    name: str
    weight: float
    enabled: bool
    error_cooldown_seconds: int
    quota_limits: dict[str, int]
    quota_window_seconds: dict[str, int]
    bridge: ChatGPTBrowserBridge
    busy: bool = False
    connected: bool = False
    logged_in: bool = False
    successful_requests: int = 0
    failed_requests: int = 0
    last_selected_at: float = 0
    last_success_at: float | None = None
    last_error: str | None = None
    cooldown_until: float = 0
    remote_quota_hint: str | None = None
    remote_quota_checked_at: float | None = None
    usage: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))

    def trim_usage(self, model_name: str, now: float) -> None:
        window = self.quota_window_seconds.get(model_name)
        if not window:
            return
        timestamps = self.usage[model_name]
        while timestamps and timestamps[0] <= now - window:
            timestamps.popleft()

    def remaining(self, model_name: str, now: float) -> int | None:
        limit = self.quota_limits.get(model_name)
        window = self.quota_window_seconds.get(model_name)
        if limit is None or window is None:
            return None
        self.trim_usage(model_name, now)
        return max(0, limit - len(self.usage[model_name]))

    def quota_status(self, now: float) -> dict[str, Any]:
        models = sorted(set(self.quota_limits) | set(self.quota_window_seconds))
        result = {}
        for model in models:
            limit = self.quota_limits.get(model)
            window = self.quota_window_seconds.get(model)
            remaining = self.remaining(model, now)
            timestamps = self.usage[model]
            reset_at = None
            if timestamps and window:
                reset_at = timestamps[0] + window
            result[model] = {
                "limit": limit,
                "window_seconds": window,
                "used_by_cws": len(timestamps),
                "remaining_estimate": remaining,
                "reset_at": reset_at,
                "source": "local_cws_estimate" if remaining is not None else "unknown",
            }
        return result


class ChatGPTBrowserPool:
    """Account-affine, weighted least-usage scheduler for ChatGPT browser profiles."""

    def __init__(self, settings):
        self.settings = settings
        self._condition = asyncio.Condition()
        self._accounts: dict[str, BrowserAccountRuntime] = {}
        account_settings = list(settings.browser_accounts)
        if not account_settings:
            account_settings = [
                type("DefaultBrowserAccount", (), {
                    "id": "default",
                    "name": "Default ChatGPT account",
                    "enabled": True,
                    "cdp_url": settings.browser_cdp_url,
                    "chatgpt_url": settings.browser_chatgpt_url,
                    "weight": 1.0,
                    "model_labels": {},
                    "quota_limits": {},
                    "quota_window_seconds": {},
                    "error_cooldown_seconds": 60,
                })()
            ]

        for account in account_settings:
            bridge_settings = settings.model_copy(deep=True)
            bridge_settings.browser_cdp_url = account.cdp_url
            bridge_settings.browser_chatgpt_url = account.chatgpt_url or settings.browser_chatgpt_url
            if account.model_labels:
                bridge_settings.browser_model_labels.update(account.model_labels)
            runtime = BrowserAccountRuntime(
                account_id=account.id,
                name=account.name,
                weight=account.weight,
                enabled=account.enabled,
                error_cooldown_seconds=account.error_cooldown_seconds,
                quota_limits={str(key): value for key, value in account.quota_limits.items()},
                quota_window_seconds={str(key): value for key, value in account.quota_window_seconds.items()},
                bridge=ChatGPTBrowserBridge(bridge_settings),
            )
            self._accounts[runtime.account_id] = runtime

    @property
    def default_account_id(self) -> str:
        for account in self._accounts.values():
            if account.enabled:
                return account.account_id
        raise OpenaiWebException("No enabled ChatGPT browser account", code=503)

    @property
    def default_bridge(self) -> ChatGPTBrowserBridge:
        return self._accounts[self.default_account_id].bridge

    def account(self, account_id: str | None) -> BrowserAccountRuntime:
        resolved_id = account_id or self.default_account_id
        account = self._accounts.get(resolved_id)
        if account is None:
            raise InvalidParamsException(f"Unknown ChatGPT browser account: {resolved_id}")
        return account

    def all_accounts(self) -> list[BrowserAccountRuntime]:
        return list(self._accounts.values())

    async def startup(self) -> None:
        await asyncio.gather(
            *(self._refresh_health(account) for account in self._accounts.values() if account.enabled),
            return_exceptions=True,
        )

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(account.bridge.shutdown() for account in self._accounts.values()),
            return_exceptions=True,
        )

    def mark_stale(self) -> None:
        for account in self._accounts.values():
            account.bridge.mark_stale()
            account.connected = False

    async def _refresh_health(self, account: BrowserAccountRuntime) -> dict[str, Any]:
        try:
            health = await account.bridge.health()
            account.connected = bool(health["connected"])
            account.logged_in = bool(health["logged_in"])
            if account.logged_in:
                account.last_error = None
                account.cooldown_until = 0
            return health
        except Exception as exc:
            account.connected = False
            account.logged_in = False
            account.last_error = str(exc)
            return {"connected": False, "logged_in": False}

    def _selection_score(self, account: BrowserAccountRuntime, model_name: str, now: float) -> tuple:
        remaining = account.remaining(model_name, now)
        exhausted = remaining == 0
        used = len(account.usage[model_name])
        return (
            not (account.connected and account.logged_in),
            exhausted,
            used / account.weight,
            account.successful_requests / account.weight,
            account.last_selected_at,
            account.account_id,
        )

    async def _acquire(self, preferred_account_id: str | None, model_name: str) -> BrowserAccountRuntime:
        async with self._condition:
            while True:
                now = time.time()
                if preferred_account_id:
                    account = self.account(preferred_account_id)
                    if not account.enabled:
                        raise OpenaiWebException(
                            f"ChatGPT browser account '{account.account_id}' is disabled", code=503
                        )
                    if account.remaining(model_name, now) == 0:
                        raise OpenaiWebException(
                            f"ChatGPT browser account '{account.account_id}' has exhausted the configured "
                            f"{model_name} quota; this conversation cannot move to another account",
                            code=429,
                        )
                    if account.cooldown_until > now:
                        raise OpenaiWebException(
                            f"ChatGPT browser account '{account.account_id}' is cooling down", code=429
                        )
                    candidates = [] if account.busy else [account]
                else:
                    candidates = [
                        account for account in self._accounts.values()
                        if account.enabled and not account.busy and account.cooldown_until <= now
                        and account.remaining(model_name, now) != 0
                    ]

                if candidates:
                    selected = min(
                        candidates,
                        key=lambda candidate: self._selection_score(candidate, model_name, now),
                    )
                    selected.busy = True
                    selected.last_selected_at = now
                    return selected

                if preferred_account_id:
                    await self._condition.wait()
                    continue

                enabled = [account for account in self._accounts.values() if account.enabled]
                if not enabled:
                    raise OpenaiWebException("No enabled ChatGPT browser account", code=503)
                exhausted = all(account.remaining(model_name, now) == 0 for account in enabled)
                if exhausted:
                    raise OpenaiWebException(
                        f"All ChatGPT browser accounts have exhausted the configured {model_name} quota",
                        code=429,
                    )
                cooldowns = [account.cooldown_until for account in enabled if account.cooldown_until > now]
                if cooldowns and all(account.busy is False for account in enabled):
                    timeout = max(0.1, min(cooldowns) - now)
                    try:
                        await asyncio.wait_for(self._condition.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await self._condition.wait()

    async def _release(
        self,
        account: BrowserAccountRuntime,
        model_name: str,
        error: BaseException | None,
    ) -> None:
        now = time.time()
        async with self._condition:
            account.busy = False
            if error is None:
                account.successful_requests += 1
                account.last_success_at = now
                account.last_error = None
                account.usage[model_name].append(now)
                account.trim_usage(model_name, now)
            else:
                account.failed_requests += 1
                account.last_error = str(error)
                code = getattr(error, "code", None)
                if code in {401, 403, 429, 503, 504}:
                    account.cooldown_until = now + account.error_cooldown_seconds
            self._condition.notify_all()

    @asynccontextmanager
    async def lease(
        self,
        *,
        preferred_account_id: str | None,
        model_name: str,
    ) -> AsyncIterator[BrowserAccountRuntime]:
        account = await self._acquire(preferred_account_id, model_name)
        error = None
        try:
            yield account
        except BaseException as exc:
            error = exc
            raise
        finally:
            await asyncio.shield(self._release(account, model_name, error))

    async def status(self, *, refresh: bool = False, refresh_quota: bool = False) -> dict[str, Any]:
        if refresh:
            await asyncio.gather(
                *(self._refresh_health(account) for account in self._accounts.values() if not account.busy),
                return_exceptions=True,
            )
        if refresh_quota:
            await asyncio.gather(
                *(self._refresh_quota_hint(account) for account in self._accounts.values()
                  if account.enabled and account.logged_in and not account.busy),
                return_exceptions=True,
            )

        now = time.time()
        accounts = []
        for account in self._accounts.values():
            accounts.append({
                "id": account.account_id,
                "name": account.name,
                "enabled": account.enabled,
                "weight": account.weight,
                "busy": account.busy,
                "connected": account.connected,
                "logged_in": account.logged_in,
                "cooldown_until": account.cooldown_until or None,
                "successful_requests": account.successful_requests,
                "failed_requests": account.failed_requests,
                "last_selected_at": account.last_selected_at or None,
                "last_success_at": account.last_success_at,
                "last_error": account.last_error,
                "quota": account.quota_status(now),
                "remote_quota_hint": account.remote_quota_hint,
                "remote_quota_checked_at": account.remote_quota_checked_at,
            })
        return {
            "transport": "browser",
            "capacity": sum(account.enabled for account in self._accounts.values()),
            "busy": sum(account.busy for account in self._accounts.values()),
            "accounts": accounts,
        }

    async def _refresh_quota_hint(self, account: BrowserAccountRuntime) -> None:
        try:
            account.remote_quota_hint = await account.bridge.quota_hint()
            account.remote_quota_checked_at = time.time()
        except Exception as exc:
            account.last_error = str(exc)
