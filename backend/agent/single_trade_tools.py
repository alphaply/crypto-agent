"""Flat internal trading tools backed by the existing versioned execution path."""
from __future__ import annotations

from copy import deepcopy
from functools import partial
import json
import re
from typing import Any

from langchain_core.tools import StructuredTool

from backend.agent.trade_guidance import PARAMETER_ERROR_PREFIX
from backend.agent.trading_contracts import parse_v3_request
from backend.agent.unified_trading import capabilities_for_config, unified_schema_for_config, validate_for_config


SINGLE_TRADE_ACTIONS = {
    'open_position': 'open',
    'close_position': 'close',
    'edit_order': 'edit',
    'cancel_order': 'cancel',
    'set_position_protection': 'protect',
}
ACTION_LABELS = {'open': '开仓', 'close': '平仓', 'edit': '改单', 'cancel': '撤单', 'protect': '保护设置'}


class SingleTradeParameterError(ValueError):
    """A rejection of this call only, before any financial submission."""

    def __init__(self, tool_name: str, detail: str):
        label = ACTION_LABELS[SINGLE_TRADE_ACTIONS[tool_name]]
        detail = detail.removeprefix(PARAMETER_ERROR_PREFIX).replace('本批未提交。', '')
        detail = re.sub(r'第\s*1\s*项[：:]?\s*|actions\[0\]:\s*', '', detail)
        detail = detail.replace('Field required', '必填').replace('Extra inputs are not permitted', '不接受此字段')
        if 'requires trigger_price' in detail:
            detail += ('；trigger_price 是止盈/止损的触发价，必须按本次计划显式填写。'
                       '条件市价单只填 trigger_price；条件限价单还要填写执行限价 price。'
                       '请补齐缺参后重新调用本工具，不要重发其他已成功或待核验的操作')
        super().__init__(f'{PARAMETER_ERROR_PREFIX}{tool_name}：{detail.rstrip("。 ")}。本次{label}未提交。')


def single_trade_error_receipt(tool_name: str, error: Exception) -> dict:
    return {'action': SINGLE_TRADE_ACTIONS[tool_name], 'status': 'failed', 'phase': 'preflight',
            'submitted': False, 'error_code': 'invalid_request', 'error': str(error)}


def _schema(tool_name: str, config: dict, batch_schema: dict) -> dict:
    action = SINGLE_TRADE_ACTIONS[tool_name]
    reference = batch_schema['properties']['actions']['items']['discriminator']['mapping'][action]
    schema = deepcopy(batch_schema['$defs'][reference.rsplit('/', 1)[-1]])
    schema['title'] = tool_name
    for key in ('action', 'depends_on'):
        schema['properties'].pop(key, None)
    schema['required'] = [key for key in schema.get('required', []) if key != 'action']
    if len(config.get('symbols') or []) > 1:
        schema['required'].append('symbol')
    schema['properties']['symbol']['description'] = '授权交易对；多标的任务必填，单标的可省略。'
    schema['properties']['reason']['description'] = '本次操作原因，必填。'
    if action == 'edit':
        schema['properties']['remaining_amount']['description'] = (
            '目标未成交币数；已成交0.2、此处填0.5，订单总量为0.7。全部平仓单不可设置。')
    schema['examples'] = [_example(action, config, schema)]
    if action == 'close':
        kinds = schema['properties']['order_type']['enum']
        rules = []
        prices = {'limit': {'price': 100}, 'stop_market': {'trigger_price': 95},
                  'take_profit_market': {'trigger_price': 105},
                  'stop_limit': {'trigger_price': 95, 'price': 94},
                  'take_profit_limit': {'trigger_price': 105, 'price': 104},
                  'stop_loss': {'trigger_price': 95}, 'take_profit': {'trigger_price': 105},
                  'oco': {'stop_loss': 95, 'take_profit': 105}}
        for kind in kinds:
            fields = prices.get(kind, {})
            rules.append(f'{kind}: ' + (' + '.join(fields) if fields else '不填价格'))
            if fields:
                schema['examples'].append({**schema['examples'][0], 'order_type': kind, **fields})
        schema['properties']['order_type']['description'] += '价格字段必须与类型匹配：' + '；'.join(rules) + '。'
        if 'trigger_price' in schema['properties']:
            schema['properties']['trigger_price']['description'] = (
                '止盈/止损触发价。stop_market、take_profit_market、stop_limit、take_profit_limit 必填；'
                '不同于执行限价 price，不会自动从 price 或理由中提取。')
    return schema


def _example(action: str, config: dict, schema: dict) -> dict:
    example = {
        'open': {'side': 'LONG', 'order_type': 'limit', 'amount': 0.1, 'price': 100, 'reason': '限价入场'},
        'close': {'side': 'LONG', 'order_type': 'market', 'amount': 0.1, 'reason': '减少持仓'},
        'edit': {'order_id': 'regular:当前订单ID', 'remaining_amount': 0.5, 'reason': '调整剩余待成交数量'},
        'cancel': {'order_id': 'regular:当前订单ID', 'reason': '撤销挂单'},
        'protect': {'side': 'LONG', 'stop_loss': 95, 'take_profit': 105, 'reason': '调整持仓保护'},
    }[action]
    if action == 'open' and 'stop_loss' in schema['required']:
        example.update(stop_loss=95, take_profit=105)
    if action == 'protect':
        if 'amount' in schema['required']:
            example['amount'] = 0.1
        if 'order_id' in schema['required']:
            example['order_id'] = 'regular:当前订单ID'
    if 'symbol' in schema['required']:
        example['symbol'] = config['symbols'][0]
    return example


def _description(tool_name: str, schema: dict) -> str:
    description = {
        'open_position': '限价开仓或加仓。amount 为币数，price 为入场限价。',
        'close_position': '平仓或设置退出单。按 order_type 填写数量和价格。',
        'edit_order': ('修改挂单，填完整 order_id；至少改一项，省略项保留原值。'
                       'remaining_amount 为目标未成交币数。'),
        'cancel_order': '撤销挂单，填完整 order_id。撤单不会平仓。',
        'set_position_protection': '设置持仓保护，stop_loss（止损）和 take_profit（止盈）至少填一项。',
    }[tool_name]
    if 'side' in schema['properties']:
        if schema['properties']['side'].get('const') == 'LONG':
            description += '现货 side=LONG。'
        else:
            description += {
                'open_position': 'side：LONG 开多，SHORT 开空。',
                'close_position': 'side：LONG 平多，SHORT 平空。',
                'set_position_protection': 'side：LONG 保护多头，SHORT 保护空头。',
            }[tool_name]
    if tool_name == 'close_position':
        description += schema['properties']['order_type']['description']
        description += 'amount（币数）与 close_all=true（全部平仓，限支持的类型）二选一。' if 'close_all' in schema['properties'] else 'amount 必填，单位为币数。'
    example = json.dumps(schema['examples'][0], ensure_ascii=False, separators=(',', ':'))
    conditional = ('。其他订单类型示例（每项为独立调用）：' +
                   json.dumps(schema['examples'][1:], ensure_ascii=False, separators=(',', ':'))
                   if len(schema['examples']) > 1 else '')
    return (description + '每次一笔。参数示例（按实际意图填写）：' + example + conditional +
            '。明确失败只影响本次；pending/unknown 停止本轮后续交易，由后台核验原操作。'
            '后续决策按当前账户事实和具体动作的准入结果判断，历史未决记录不等于整个账户永久停用。'
            'client: 开头的是本地提交引用，不是可撤销的交易所订单号，不要对它反复撤单。'
            '依赖前项的操作须等其成功。submitted 表示已受理，未必成交；成功或结果未知的操作勿重发。')


def _invoke(tool_name: str, config_id: str, trusted_symbol: str, **arguments: Any) -> str:
    """Like the legacy adapters, execution requires injected trusted context."""
    from backend.agent.tool_registry import run_trade_tool
    return run_trade_tool(tool_name, arguments, config_id, trusted_symbol)


def get_single_trade_tools(config: dict) -> list[StructuredTool]:
    if config.get('read_only'):
        return []
    batch_schema = unified_schema_for_config(config)
    allowed = capabilities_for_config(config)['actions']
    tools = []
    for name, action in SINGLE_TRADE_ACTIONS.items():
        if action not in allowed:
            continue
        schema = _schema(name, config, batch_schema)
        tools.append(StructuredTool.from_function(
            func=partial(_invoke, name), name=name, args_schema=schema, description=_description(name, schema)))
    return tools


def prepare_single_trade_request(tool_name: str, arguments: Any, config: dict,
                                 default_symbol: str) -> dict:
    """Validate one call without resolving live targets or reserving an operation."""
    def reject(detail: str):
        raise SingleTradeParameterError(tool_name, detail)

    if not config or config.get('read_only'):
        reject('当前任务没有可用的交易授权')
    action = SINGLE_TRADE_ACTIONS[tool_name]
    if action not in capabilities_for_config(config)['actions']:
        reject('当前交易模式不支持此工具')
    if not isinstance(arguments, dict):
        reject('参数必须是一个对象，每次只提交一笔操作')
    schema = _schema(tool_name, config, unified_schema_for_config(config))
    extra = set(arguments) - set(schema['properties'])
    if extra:
        hints = []
        if action == 'edit' and 'amount' in extra:
            hints.append('改单不支持 amount。请用 remaining_amount 表示目标未成交基础币数量')
            extra.remove('amount')
        if extra:
            hints.append('不支持参数 ' + ', '.join(sorted(extra)) + '，请按此工具的参数说明填写')
        reject('。'.join(hints))
    payload = {'contract_version': 3, 'actions': [{'action': action, **deepcopy(arguments)}]}
    try:
        # Preserve the existing v3 normalization and durable request meaning.
        payload = parse_v3_request(payload, market=capabilities_for_config(config)['market_type']).model_dump(exclude_none=True)
        validate_for_config(payload, config, default_symbol=default_symbol)
    except ValueError as exc:
        raise SingleTradeParameterError(tool_name, str(exc)) from exc
    return payload
