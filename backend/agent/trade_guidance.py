"""Shared model-facing guidance; malformed identities are rejected, never inferred."""
from __future__ import annotations

import json
from typing import Any

PARAMETER_ERROR_PREFIX = '交易参数校验失败：'

ACTION_DESCRIPTION = 'Use the exact field action (not action_type).'
ORDER_ID_DESCRIPTION = (
    'Copy the complete current order_id, including its prefix, from an order or receipt. '
    'If missing, query open orders. operation_id identifies the request.'
)
OPEN_SIDE_DESCRIPTION = 'Opening direction for this item: LONG opens a long; SHORT opens a short.'
CLOSE_SIDE_DESCRIPTION = (
    'Required on each close, including stops, take profits and close_all. LONG closes a long (平多); '
    'SHORT closes a short (平空). Each item supplies its own side.'
)
ORDER_TYPE_DESCRIPTION = 'Choose a lowercase order type from the listed values.'
ACTIONS_DESCRIPTION = (
    '1–20 ordered items. Put action (not action_type), reason and parameters at the same level. '
    'Each item supplies its own required fields; nothing is inherited from the preceding item. '
    'Use your intended prices and quantities in place of the examples.'
)
TRADE_ARGUMENT_GUIDANCE = (
    'Submit 1–20 actions in order with contract_version=3. Submit directly for authorized holdings, '
    'including shared ones. The entire batch is validated before submission and each target is checked at execution. '
    'Read each result by index: submitted means accepted, filled_amount reports fills. '
    'Do not replay a successful or uncertain item to correct another.'
)
LIVE_TRADE_GUIDANCE = TRADE_ARGUMENT_GUIDANCE + ' A pending or unknown submission stops remaining items for background verification.'


def describe_trade_tool(schema: dict, *, paper: bool = False) -> str:
    """Keep field names and a valid entry visible even when only the tool description is read."""
    actions = schema['properties']['actions']
    variants = [schema['$defs'][ref['$ref'].rsplit('/', 1)[-1]] for ref in actions.get('anyOf', [])] or [actions]
    lines = []
    for variant in variants:
        allowed = variant.get('items', {})
        mapping = allowed.get('discriminator', {}).get('mapping', {}) if isinstance(allowed, dict) else {}
        if not mapping:
            continue
        prefix = variant['title'] + ' · ' if len(variants) > 1 else ''
        lines.append(prefix + 'action 可用值为 ' + ', '.join(mapping) + '。')
        if 'open' in mapping:
            entry = next(batch[0] for batch in variant['examples'] if batch[0]['action'] == 'open')
            definitions = variant.get('$defs', schema['$defs'])
            opening = definitions[mapping['open'].rsplit('/', 1)[-1]]['properties']
            if paper:
                direction = '现货用 LONG。合约开多用 LONG，开空用 SHORT。'
            elif opening['side'].get('const') == 'LONG':
                direction = '现货买入用 LONG。'
            else:
                direction = '合约开多用 LONG，开空用 SHORT。'
            lines.append('开仓与加仓都填 action="open"，方向单独填 side。' + direction +
                         '标的币数量填 amount。actions 中的一项开仓示例（量价按本次意图填写）：' +
                         json.dumps(entry, ensure_ascii=False, separators=(',', ':')))
    lines.append(TRADE_ARGUMENT_GUIDANCE if paper else LIVE_TRADE_GUIDANCE)
    return '\n'.join(lines)


def validate_action_keys(value: Any) -> Any:
    """Catch common field-name mistakes before dispatching by action."""
    if not isinstance(value, dict):
        return value
    if 'action' not in value or 'action_type' in value:
        raise ValueError(
            'Every actions item requires the field action, not action_type. '
            'Use an allowed literal such as action="edit"; keep its parameters at the same level. '
            'edit/cancel also require the current order_id from an order query or receipt.'
        )
    if value.get('action') in ('edit', 'cancel') and (
        not isinstance(value.get('order_id'), str) or not value['order_id']
    ):
        raise ValueError(
            'order_id is required for edit/cancel and must be a non-empty string. '
            + ORDER_ID_DESCRIPTION
        )
    return value


def validate_action_identity(value: Any) -> Any:
    """Keep the compatibility contract's original direction and order-type hints."""
    value = validate_action_keys(value)
    if not isinstance(value, dict):
        return value
    hints = []
    if value.get('action') in ('open', 'close'):
        if value.get('side') not in ('LONG', 'SHORT'):
            description = CLOSE_SIDE_DESCRIPTION if value['action'] == 'close' else OPEN_SIDE_DESCRIPTION
            hints.append(f'{value["action"]}.side is required and must be LONG or SHORT. {description}')
        kind = value.get('order_type')
        if value['action'] == 'close' and not kind:
            hints.append('close.order_type is required. ' + ORDER_TYPE_DESCRIPTION)
    if hints:
        raise ValueError(' '.join(hints))
    return value
