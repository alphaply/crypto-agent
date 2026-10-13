from pydantic import BaseModel, ConfigDict, Field


class RetryDecisionMemoryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    config_id: str = Field(min_length=1)
