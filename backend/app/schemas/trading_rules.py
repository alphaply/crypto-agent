from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CreateTradingRuleRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    config_id: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=1200)
    enabled: bool = True
    locked: bool = True
    reason: str = Field(default='', max_length=1000)

    @field_validator('content')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Rule content cannot be blank')
        return value.strip()


class UpdateTradingRuleRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    config_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)
    content: str | None = Field(default=None, min_length=1, max_length=1200)
    enabled: bool | None = None
    locked: bool | None = None
    reason: str = Field(default='', max_length=1000)

    @model_validator(mode='after')
    def has_change(self):
        if all(getattr(self, key) is None for key in ('content', 'enabled', 'locked')):
            raise ValueError('Provide content, enabled, or locked to update')
        if self.content is not None and not self.content.strip():
            raise ValueError('Rule content cannot be blank')
        return self
