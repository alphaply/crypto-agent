from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    password: str


class CreateSessionRequest(BaseModel):
    config_id: str
    title: str | None = None


class BulkDeleteSessionsRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)


class SaveConfigRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    configs: list[dict]
    global_settings: dict = Field(default_factory=dict, alias="global")


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
