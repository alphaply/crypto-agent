"""Versioned decision reports. Execution facts always come from tool receipts."""
from __future__ import annotations

import json
from typing import Literal

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field

from backend.utils.llm_utils import extract_message_text


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['BUY', 'SELL', 'HOLD', 'CLOSE', 'MANAGE']
    rationale: str = Field(min_length=1)


class Forecast(BaseModel):
    model_config = ConfigDict(extra='forbid')
    next_1h: str = Field(min_length=1)
    next_4h: str = Field(min_length=1)


class TradeReport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: Literal[1]
    market_analysis: str = Field(min_length=1)
    strategy: str = Field(min_length=1)
    decision: Decision
    forecast: Forecast
    risks: list[str] = Field(min_length=1)
    invalidation: str = Field(min_length=1)
    next_watchpoints: list[str] = Field(min_length=1)
    summary: str = Field(min_length=1)


REPORT_POLICY = '''将已完成的本轮交易分析整理为 TradeReport v1，不执行交易，不补造事实。
只保留一个主策略，未来1h和4h写带触发及失效条件的判断；缺失证据明确写未知。
必须填写行情解析、主策略、交易决策、未来判断、风险、失效条件、下次观察点、摘要。
decision.action仅可为BUY/SELL/HOLD/CLOSE/MANAGE；等待是合法决定，不能为了报告强制交易。
计划不等于成交，实际执行状态由程序另行添加。只输出符合下列schema的JSON，不使用代码围栏：
'''


def report_instructions() -> str:
    return REPORT_POLICY + json.dumps(TradeReport.model_json_schema(), ensure_ascii=False)


def parse_report_response(response) -> TradeReport:
    calls = getattr(response, 'tool_calls', None) or []
    if calls:
        if len(calls) != 1 or calls[0].get('name') != 'TradeReport':
            raise ValueError('Report must return only the TradeReport submission')
        return TradeReport.model_validate(calls[0].get('args'))
    text = extract_message_text(response).strip()
    if text.startswith('```') and text.endswith('```'):
        text = text.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    return TradeReport.model_validate_json(text)


def execution_receipts(messages) -> list[dict]:
    names = {call['id']: call.get('name', 'tool') for msg in messages if isinstance(msg, AIMessage)
             for call in (msg.tool_calls or [])}
    return [{'tool': getattr(msg, 'name', None) or names.get(msg.tool_call_id, 'tool'),
             'tool_call_id': msg.tool_call_id, 'result': extract_message_text(msg)}
            for msg in messages if isinstance(msg, ToolMessage)]


def assemble_report(raw: str, messages, original: str) -> dict:
    try:
        result = TradeReport.model_validate_json(raw).model_dump()
        status = 'valid'
    except (ValueError, TypeError):
        # The executed decision survives a failed formatter. Never invent its direction.
        result = {'version': 1, 'market_analysis': '报告整理未完成，请查看原始分析。',
                  'strategy': '未获得有效结构化策略',
                  'decision': {'action': 'HOLD', 'rationale': '报告状态未知；HOLD仅表示无新增报告动作，实际交易以回执为准。'},
                  'forecast': {'next_1h': '未知', 'next_4h': '未知'}, 'risks': ['报告格式校验失败'],
                  'invalidation': '未知', 'next_watchpoints': ['重新整理本轮报告'], 'summary': raw or original}
        status = 'invalid'
    return {**result, 'validation_status': status, 'execution_results': execution_receipts(messages),
            'raw_analysis': original}


def render_report(report: dict) -> str:
    receipts = '\n'.join(f"- {item['tool']}: {item['result']}" for item in report['execution_results']) or '本轮没有工具执行。'
    return (f"## 行情解析\n{report['market_analysis']}\n\n## 主策略\n{report['strategy']}"
            f"\n\n## 交易决策\n{report['decision']['action']} · {report['decision']['rationale']}"
            f"\n\n## 未来判断\n1h：{report['forecast']['next_1h']}\n\n4h：{report['forecast']['next_4h']}"
            f"\n\n## 风险与失效条件\n" + '\n'.join('- ' + item for item in report['risks'])
            + f"\n失效：{report['invalidation']}\n\n## 下次观察\n"
            + '\n'.join('- ' + item for item in report['next_watchpoints'])
            + f"\n\n## 实际执行回执\n{receipts}")
