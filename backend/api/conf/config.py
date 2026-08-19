from typing import Optional, Literal

from pydantic import field_validator, model_validator, ConfigDict, BaseModel, Field

from api.conf.base_config import BaseConfig
from api.enums import OpenaiWebChatModels, OpenaiApiChatModels
from api.enums.options import OpenaiWebFileUploadStrategyOption
from utils.common import SingletonMeta

_TYPE_CHECKING = False

default_openai_web_model_code_mapping = {
    "chatgpt_auto": "auto",
    "chatgpt_instant": "instant",
    "chatgpt_thinking": "thinking",
    "chatgpt_pro": "pro",
    "gpt_3_5": "text-davinci-002-render-sha",
    "gpt_3_5_mobile": "text-davinci-002-render-sha-mobile",
    "gpt_4": "gpt-4",
    "gpt_4o": "gpt-4o",
    "gpt_4_mobile": "gpt-4-mobile",
    "gpt_4_browsing": "gpt-4-browsing",
    "gpt_4_plugins": "gpt-4-plugins",
    "gpt_4_code_interpreter": "gpt-4-code-interpreter",
    "gpt_4_dalle": "gpt-4-dalle"
}


class CommonSetting(BaseModel):
    print_sql: bool = False
    print_traceback: bool = True
    create_initial_admin_user: bool = True
    initial_admin_user_username: str = 'admin'
    initial_admin_user_password: str = 'password'

    @field_validator("initial_admin_user_password")
    @classmethod
    def validate_password(cls, v):
        if len(v) < 6:
            raise ValueError("Password too short")
        return v


class HttpSetting(BaseModel):
    host: str = '127.0.0.1'
    port: int = Field(8000, ge=1, le=65535)
    cors_allow_origins: list[str] = ['http://localhost:8000', 'http://localhost:5173', 'http://127.0.0.1:8000',
                                     'http://127.0.0.1:5173']


class DataSetting(BaseModel):
    data_dir: str = './data'
    database_url: str = 'sqlite+aiosqlite:///data/database.db'
    mongodb_url: str = 'mongodb://cws:password@mongo:27017'
    mongodb_db_name: str = 'cws'
    run_migration: bool = True
    max_file_upload_size: int = Field(100 * 1024 * 1024, ge=0)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v):
        if not v.startswith('sqlite+aiosqlite:///'):
            raise ValueError("Only support sqlite: 'sqlite+aiosqlite:///'")
        return v


class AuthSetting(BaseModel):
    jwt_secret: str = 'MODIFY_THIS_TO_RANDOM_SECURE_STRING'
    jwt_lifetime_seconds: int = Field(3 * 24 * 3600, ge=1)
    cookie_max_age: int = Field(3 * 24 * 3600, ge=1)
    user_secret: str = 'MODIFY_THIS_TO_ANOTHER_RANDOM_SECURE_STRING'


class OpenaiWebBrowserAccountSetting(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_.-]+$')
    name: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    cdp_url: str
    chatgpt_url: Optional[str] = None
    weight: float = Field(1.0, gt=0, le=100)
    model_labels: dict[OpenaiWebChatModels, str] = {}
    quota_limits: dict[OpenaiWebChatModels, int] = {}
    quota_window_seconds: dict[OpenaiWebChatModels, int] = {}
    error_cooldown_seconds: int = Field(60, ge=5, le=86400)

    @field_validator("quota_limits")
    @classmethod
    def validate_quota_limits(cls, value):
        if any(limit <= 0 for limit in value.values()):
            raise ValueError("quota limits must be positive")
        return value

    @field_validator("quota_window_seconds")
    @classmethod
    def validate_quota_windows(cls, value):
        if any(window <= 0 for window in value.values()):
            raise ValueError("quota windows must be positive")
        return value


class OpenaiWebChatGPTSetting(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    enabled: bool = True
    transport: Literal['browser', 'legacy_http'] = 'browser'
    is_plus_account: bool = True
    enable_team_subscription: bool = False
    team_account_id: Optional[str] = None
    chatgpt_base_url: Optional[str] = None
    browser_cdp_url: str = 'http://host.docker.internal:9222'
    browser_chatgpt_url: str = 'https://chatgpt.com'
    browser_poll_interval_ms: int = Field(250, ge=100, le=5000)
    browser_stable_seconds: float = Field(1.5, ge=0.5, le=30)
    browser_model_labels: dict[OpenaiWebChatModels, str] = {
        'chatgpt_auto': '',
        'chatgpt_instant': 'Instant',
        'chatgpt_thinking': 'Thinking',
        'chatgpt_pro': 'Pro',
    }
    browser_accounts: list[OpenaiWebBrowserAccountSetting] = []
    proxy: Optional[str] = None
    wss_proxy: Optional[str] = None
    enable_arkose_endpoint: bool = False
    arkose_endpoint_base: Optional[str] = None
    common_timeout: int = Field(20, ge=1,
                                description="Increase this value if timeout error occurs.")  # connect, read, write
    ask_timeout: int = Field(600, ge=1)
    sync_conversations_on_startup: bool = False
    sync_conversations_schedule: bool = False
    sync_conversations_schedule_interval_hours: int = Field(12, ge=1)
    enabled_models: list[OpenaiWebChatModels] = ["chatgpt_auto"]
    model_code_mapping: dict[OpenaiWebChatModels, str] = default_openai_web_model_code_mapping
    file_upload_strategy: OpenaiWebFileUploadStrategyOption = OpenaiWebFileUploadStrategyOption.browser_upload_only
    max_completion_concurrency: int = Field(1, ge=1)
    disable_uploading: bool = True

    @model_validator(mode="after")
    def validate_browser_transport(self):
        account_ids = [account.id for account in self.browser_accounts]
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("browser account ids must be unique")
        enabled_count = sum(account.enabled for account in self.browser_accounts)
        if self.transport == "browser" and self.browser_accounts and enabled_count == 0:
            raise ValueError("at least one browser account must be enabled")
        capacity = enabled_count or 1
        if self.transport == "browser" and self.max_completion_concurrency > capacity:
            raise ValueError(
                "max_completion_concurrency cannot exceed the number of enabled browser accounts"
            )
        return self

    @field_validator("chatgpt_base_url")
    @classmethod
    def chatgpt_base_url_end_with_slash(cls, v):
        if v is not None and not v.endswith('/'):
            v += '/'
        return v

    @field_validator("arkose_endpoint_base")
    @classmethod
    def arkose_endpoint_base_end_with_slash(cls, v):
        if v is not None and not v.endswith('/'):
            v += '/'
        return v

    @field_validator("model_code_mapping")
    @classmethod
    def check_all_model_key_appears(cls, v):
        if not set(OpenaiWebChatModels) == set(v.keys()):
            # add missing keys
            for model in OpenaiWebChatModels:
                if model not in v:
                    assert model in default_openai_web_model_code_mapping
                    v[model] = default_openai_web_model_code_mapping[model]
        return v


class OpenaiApiSetting(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    enabled: bool = True
    openai_base_url: str = 'https://api.openai.com/v1/'
    proxy: Optional[str] = None
    connect_timeout: int = Field(10, ge=1)
    read_timeout: int = Field(20, ge=1)
    enabled_models: list[OpenaiApiChatModels] = ["gpt_3_5", "gpt_4"]
    model_code_mapping: dict[OpenaiApiChatModels, str] = {
        "gpt_3_5": "gpt-3.5-turbo",
        "gpt_4": "gpt-4",
    }


class LogSetting(BaseModel):
    console_log_level: Literal['INFO', 'DEBUG', 'WARNING'] = 'INFO'


class StatsSetting(BaseModel):
    ask_stats_ttl: int = 90 * 24 * 60 * 60  # 90 days
    request_stats_ttl: int = 30 * 24 * 60 * 60  # 30 days. -1 means never expire
    request_stats_filter_keywords: list[str] = ['/status']


class ConfigModel(BaseModel):
    openai_web: OpenaiWebChatGPTSetting = OpenaiWebChatGPTSetting()
    openai_api: OpenaiApiSetting = OpenaiApiSetting()
    common: CommonSetting = CommonSetting()
    http: HttpSetting = HttpSetting()
    data: DataSetting = DataSetting()
    auth: AuthSetting = AuthSetting()
    stats: StatsSetting = StatsSetting()
    log: LogSetting = LogSetting()


class Config(BaseConfig[ConfigModel], metaclass=SingletonMeta):
    if _TYPE_CHECKING:
        openai_web: OpenaiWebChatGPTSetting = OpenaiWebChatGPTSetting()
        openai_api: OpenaiApiSetting = OpenaiApiSetting()
        common: CommonSetting = CommonSetting()
        http: HttpSetting = HttpSetting()
        log: LogSetting = LogSetting()
        stats: StatsSetting = StatsSetting()
        data: DataSetting = DataSetting()
        auth: AuthSetting = AuthSetting()

    def __init__(self, load_config: bool = True):
        super().__init__(ConfigModel, "config.yaml", load_config=load_config)
