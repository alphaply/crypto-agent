from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    password: str


class SetupApplyRequest(BaseModel):
    admin_password: str
    jwt_secret: str
    config_master_key: str
    jwt_expire_hours: int = 8
    port: int = 7860
    timezone: str = "Asia/Shanghai"
    run_scheduler_in_web: bool = True


class CreateSessionRequest(BaseModel):
    config_id: str
    title: str | None = None


class BulkDeleteSessionsRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)


class SecretUpdate(BaseModel):
    value: str | None = None
    clear: bool = False


class GlobalSecretsPayload(BaseModel):
    global_binance_api_key: SecretUpdate = Field(default_factory=SecretUpdate)
    global_binance_secret: SecretUpdate = Field(default_factory=SecretUpdate)
    global_okx_api_key: SecretUpdate = Field(default_factory=SecretUpdate)
    global_okx_secret: SecretUpdate = Field(default_factory=SecretUpdate)
    global_okx_passphrase: SecretUpdate = Field(default_factory=SecretUpdate)
    langchain_api_key: SecretUpdate = Field(default_factory=SecretUpdate)
    global_summarizer_api_key: SecretUpdate = Field(default_factory=SecretUpdate)


class ConfigGlobalPayload(BaseModel):
    leverage: int = 20
    enable_scheduler: bool = True
    trading_mode: str = "REAL"
    langchain_tracing: bool = False
    langchain_project: str = "crypto-agent"
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 2
    global_summarizer_model: str = ""
    global_summarizer_api_base: str = ""
    market_timeframes: list[str] = Field(default_factory=lambda: ["15m", "30m", "1h", "4h", "1d", "1w", "1M"])
    secrets: GlobalSecretsPayload = Field(default_factory=GlobalSecretsPayload)


class AgentSecretsPayload(BaseModel):
    api_key: SecretUpdate = Field(default_factory=SecretUpdate)
    secret: SecretUpdate = Field(default_factory=SecretUpdate)
    passphrase: SecretUpdate = Field(default_factory=SecretUpdate)
    binance_api_key: SecretUpdate = Field(default_factory=SecretUpdate)
    binance_secret: SecretUpdate = Field(default_factory=SecretUpdate)
    okx_api_key: SecretUpdate = Field(default_factory=SecretUpdate)
    okx_secret: SecretUpdate = Field(default_factory=SecretUpdate)
    summarizer_api_key: SecretUpdate = Field(default_factory=SecretUpdate)


class LlmProviderSecretsPayload(BaseModel):
    api_key: SecretUpdate = Field(default_factory=SecretUpdate)


class ExchangeProfileSecretsPayload(BaseModel):
    api_key: SecretUpdate = Field(default_factory=SecretUpdate)
    secret: SecretUpdate = Field(default_factory=SecretUpdate)
    passphrase: SecretUpdate = Field(default_factory=SecretUpdate)


class AgentSummarizerPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str | None = None
    api_base: str | None = None
    temperature: float | None = None
    strategy_prompt_file: str | None = None
    daily_prompt_file: str | None = None
    short_memory_prompt_file: str | None = None
    strategy_prompt: str | None = None
    daily_prompt: str | None = None
    short_memory_prompt: str | None = None


class ConfigAgentPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    config_id: str
    title: str | None = None
    symbol: str
    enabled: bool = True
    mode: str = "STRATEGY"
    model: str = ""
    api_base: str | None = None
    temperature: float | None = None
    prompt_file: str | None = None
    run_interval: int | None = None
    leverage: int | None = None
    market_timeframes: list[str] | None = None
    exchange: str | None = None
    market_type: str | None = None
    dca_amount: float | None = None
    dca_budget: float | None = None
    dca_freq: str | None = None
    dca_time: str | None = None
    dca_weekday: int | None = None
    initial_cost: float | None = None
    initial_qty: float | None = None
    extra_body: dict = Field(default_factory=dict)
    llm_provider_id: str | None = None
    summarizer_provider_id: str | None = None
    exchange_profile_id: str | None = None
    summarizer: AgentSummarizerPayload = Field(default_factory=AgentSummarizerPayload)
    secrets: AgentSecretsPayload = Field(default_factory=AgentSecretsPayload)


class LlmProviderPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    provider_id: str
    name: str
    model: str
    api_base: str | None = None
    temperature: float | None = None
    role: str = "llm"
    extra_body: dict = Field(default_factory=dict)
    thinking_enabled: bool | None = None
    reasoning_effort: str | None = None
    input_price_per_m: float | None = 0
    output_price_per_m: float | None = 0
    pricing_currency: str | None = "USD"
    secrets: LlmProviderSecretsPayload = Field(default_factory=LlmProviderSecretsPayload)


class ExchangeProfilePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    profile_id: str
    name: str
    exchange: str
    market_type: str | None = "swap"
    secrets: ExchangeProfileSecretsPayload = Field(default_factory=ExchangeProfileSecretsPayload)


class SaveConfigRequest(BaseModel):
    globals: ConfigGlobalPayload
    agents: list[ConfigAgentPayload] = Field(default_factory=list)
    llm_providers: list[LlmProviderPayload] = Field(default_factory=list)
    exchange_profiles: list[ExchangeProfilePayload] = Field(default_factory=list)


class FullImportRequest(BaseModel):
    data: dict
    write_env: bool = False


class PromptSaveRequest(BaseModel):
    name: str
    content: str = ""


class PromptDeleteRequest(BaseModel):
    name: str


class PricingSaveRequest(BaseModel):
    model: str
    input_price: float = 0
    output_price: float = 0
    currency: str = "USD"


class PricingDeleteRequest(BaseModel):
    model: str


class GenerateDailySummaryRequest(BaseModel):
    config_id: str
    date: str


class GenerateShortMemoryRequest(BaseModel):
    config_id: str
    bucket_start: str | None = None


class UpdateShortMemoryRequest(BaseModel):
    config_id: str
    bucket_start: str
    market_summary: str = ""
    position_summary: str = ""


class UpdateDailySummaryRequest(BaseModel):
    date: str
    config_id: str
    summary: str


class DeleteDailySummaryRequest(BaseModel):
    date: str
    config_id: str


class CleanHistoryRequest(BaseModel):
    symbol: str
