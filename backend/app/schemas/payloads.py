from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    password: str


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
    secrets: GlobalSecretsPayload = Field(default_factory=GlobalSecretsPayload)


class AgentSecretsPayload(BaseModel):
    api_key: SecretUpdate = Field(default_factory=SecretUpdate)
    secret: SecretUpdate = Field(default_factory=SecretUpdate)
    passphrase: SecretUpdate = Field(default_factory=SecretUpdate)
    binance_api_key: SecretUpdate = Field(default_factory=SecretUpdate)
    binance_secret: SecretUpdate = Field(default_factory=SecretUpdate)
    summarizer_api_key: SecretUpdate = Field(default_factory=SecretUpdate)


class AgentSummarizerPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str | None = None
    api_base: str | None = None
    temperature: float | None = None


class ConfigAgentPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    config_id: str
    title: str | None = None
    symbol: str
    enabled: bool = True
    mode: str = "STRATEGY"
    model: str
    api_base: str | None = None
    temperature: float | None = None
    prompt_file: str | None = None
    run_interval: int | None = None
    leverage: int | None = None
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
    summarizer: AgentSummarizerPayload = Field(default_factory=AgentSummarizerPayload)
    secrets: AgentSecretsPayload = Field(default_factory=AgentSecretsPayload)


class SaveConfigRequest(BaseModel):
    globals: ConfigGlobalPayload
    agents: list[ConfigAgentPayload] = Field(default_factory=list)


class PromptSaveRequest(BaseModel):
    name: str
    content: str = ""


class PromptDeleteRequest(BaseModel):
    name: str


class PricingSaveRequest(BaseModel):
    model: str
    input_price: float = 0
    output_price: float = 0


class PricingDeleteRequest(BaseModel):
    model: str


class GenerateDailySummaryRequest(BaseModel):
    config_id: str
    date: str


class UpdateDailySummaryRequest(BaseModel):
    date: str
    config_id: str
    summary: str


class CleanHistoryRequest(BaseModel):
    symbol: str
