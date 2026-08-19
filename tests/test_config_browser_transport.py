import pytest
from pydantic import ValidationError

from api.conf.config import ConfigModel


def test_browser_transport_is_default():
    config = ConfigModel()
    assert config.openai_web.transport == "browser"
    assert config.openai_web.enabled_models == ["chatgpt_auto"]
    assert config.openai_web.model_code_mapping["chatgpt_auto"] == "auto"


def test_legacy_transport_remains_available():
    config = ConfigModel.model_validate(
        {"openai_web": {"transport": "legacy_http", "chatgpt_base_url": "http://proxy/backend-api/"}}
    )
    assert config.openai_web.transport == "legacy_http"
    assert config.openai_web.chatgpt_base_url.endswith("/")


def test_browser_transport_rejects_parallel_tab_use():
    with pytest.raises(ValidationError, match="max_completion_concurrency"):
        ConfigModel.model_validate(
            {"openai_web": {"transport": "browser", "max_completion_concurrency": 2}}
        )


def test_browser_transport_allows_one_concurrent_request_per_account():
    config = ConfigModel.model_validate({
        "openai_web": {
            "transport": "browser",
            "max_completion_concurrency": 2,
            "browser_accounts": [
                {"id": "plus-a", "name": "A", "cdp_url": "http://127.0.0.1:9222"},
                {"id": "plus-b", "name": "B", "cdp_url": "http://127.0.0.1:9223"},
            ],
        }
    })
    assert len(config.openai_web.browser_accounts) == 2


def test_browser_transport_rejects_an_explicit_fully_disabled_pool():
    with pytest.raises(ValidationError, match="at least one browser account"):
        ConfigModel.model_validate({
            "openai_web": {
                "transport": "browser",
                "browser_accounts": [
                    {
                        "id": "plus-a",
                        "name": "A",
                        "enabled": False,
                        "cdp_url": "http://127.0.0.1:9222",
                    }
                ],
            }
        })
