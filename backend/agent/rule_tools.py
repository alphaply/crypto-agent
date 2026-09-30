import json
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.services.trading_rules_service import apply_rule_changes, list_rules
from backend.utils.trade_operations import current_operation_id


class RuleChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['add', 'update', 'disable']
    rule_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    content: str | None = Field(default=None, min_length=1, max_length=1200)
    enabled: bool | None = None
    reason: str = Field(min_length=1, max_length=1000, description='修改依据与适用条件；不能把未经验证的假设写成事实')

    @model_validator(mode='after')
    def validate_change(self):
        if self.action == 'add' and not (self.content or '').strip():
            raise ValueError('Adding a rule requires content')
        if self.action != 'add' and (not self.rule_id or self.expected_revision is None):
            raise ValueError('Updating a rule requires rule_id and expected_revision')
        if self.action == 'update' and self.content is None and self.enabled is None:
            raise ValueError('Provide content or enabled to update')
        if not self.reason.strip():
            raise ValueError('A change reason is required')
        return self


class ManageTradingRulesSchema(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['list', 'apply'] = 'list'
    changes: list[RuleChange] = Field(default_factory=list, max_length=20)


@tool(args_schema=ManageTradingRulesSchema)
def manage_trading_rules(action: str = 'list', changes: list | None = None,
                         config_id: str = 'unknown', symbol: str = 'Unknown') -> str:
    """查看或一次新增/修改/停用本任务长期交易规则，成功即生效。先查看版本，修改必须带 expected_revision。
    人工锁定规则不可修改。只记录可复用、有依据的经验与条件；市场快照与短期计划应留在短期记忆。
    规则不会覆盖账户事实、程序风控或交易工具权限。多项规则变更整体成功或整体失败，返回实际结果和新版本。
    """
    payload = ManageTradingRulesSchema.model_validate({'action': action, 'changes': changes or []})
    if payload.action == 'list':
        if payload.changes:
            raise ValueError('Use apply to change rules')
        result = list_rules(config_id)
    else:
        result = apply_rule_changes(config_id, [change.model_dump(exclude_none=True) for change in payload.changes],
                                    actor='model', operation_id=current_operation_id.get())
    return json.dumps({'success': True, 'status': 'completed', 'rules': result}, ensure_ascii=False)
