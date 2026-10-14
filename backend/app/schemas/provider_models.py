"""Read-only provider discovery payloads, independent of saved model configuration."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class DiscoveryFields(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftCredential(DiscoveryFields):
    mode: Literal["draft"]
    api_key: SecretStr = Field(max_length=8192)


class SavedCredential(DiscoveryFields):
    mode: Literal["saved"]
    provider_id: str = Field(min_length=1, max_length=200)


class NoCredential(DiscoveryFields):
    mode: Literal["none"]


class ProviderDiscoveryRequest(DiscoveryFields):
    api_base: str = Field(max_length=2048)
    api_protocol: Literal["chat", "decisions"] = "chat"
    compatibility_mode: Literal["auto", "openai", "deepseek", "anthropic"] = "auto"
    decisions_api: Literal["bai", "typesafe"] = "bai"
    credential: Annotated[DraftCredential | SavedCredential | NoCredential, Field(discriminator="mode")]


class DiscoveredModel(DiscoveryFields):
    id: str
    name: str
    description: str = ""


class DiscoveryIssue(DiscoveryFields):
    code: str
    message: str
    retryable: bool = False


class DiscoverySource(DiscoveryFields):
    api_base: str
    url: str
    protocol: str


class ProviderDiscoveryResponse(DiscoveryFields):
    success: bool = True
    models: list[DiscoveredModel]
    fetched_at: str
    source: DiscoverySource
    complete: bool
    warning: DiscoveryIssue | None = None
