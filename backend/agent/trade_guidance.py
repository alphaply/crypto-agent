"""Shared model-facing guidance; malformed identities are rejected, never inferred."""
from __future__ import annotations

from typing import Any

ACTION_DESCRIPTION = (
    'Required action discriminator: use the exact field name action, never action_type. '
    'Use the literal value specified by this action schema.'
)
ORDER_ID_DESCRIPTION = (
    'Required current order ID from an order query or execution receipt. Copy it as a string, '
    'preserving any prefix. An operation_id identifies a request, not an order. '
    'If the current order ID is unavailable, query open orders first; never invent an ID.'
)
OPEN_SIDE_DESCRIPTION = 'Required position side: LONG opens a long, SHORT opens a short. Use uppercase LONG or SHORT.'
CLOSE_SIDE_DESCRIPTION = (
    'Required on every close, including stop-loss/take-profit orders and close_all=true. '
    'This is the existing position side: LONG closes a long (平多); SHORT closes a short (平空). '
    'Use uppercase LONG or SHORT, never BUY/SELL. Do not infer or omit side.'
)
ORDER_TYPE_DESCRIPTION = (
    'Use an exact lowercase order_type from this schema, e.g. market, limit or stop_market; '
    'case-only variants such as MARKET/LIMIT/STOP_MARKET are normalized to lowercase. Only listed types are supported.'
)
ACTIONS_DESCRIPTION = (
    '1–20 flat action objects. Every item requires action and reason; edit/cancel also require '
    'order_id. Put action, order_id and change fields at the same level, without an order wrapper. '
    'open/close require side=LONG|SHORT; close also requires lowercase order_type, even for '
    'stop-loss/take-profit or close_all=true. Use only fields published for the selected action and trading mode.'
)
TRADE_ARGUMENT_GUIDANCE = (
    'Set contract_version=3. Each actions item must contain action (not action_type) and reason. '
    'edit changes an existing order and requires its current order_id plus at least one supported '
    'change field; cancel also requires order_id. Copy order_id from current order evidence, '
    'preserving prefixes; operation_id is a request identity and cannot replace it. '
    'If the order ID is missing, query open orders before editing/cancelling. Never guess it. '
    'Use flat fields, not a nested order object. For edit, remaining_amount is the target unfilled '
    'base-coin quantity, price is the limit price, and trigger_price is a conditional-order trigger. '
    'Every open/close must explicitly include side="LONG" or side="SHORT". For close, side is '
    'the position being reduced: LONG=平多, SHORT=平空, including stop-loss/take-profit and close_all=true; '
    'never substitute BUY/SELL or infer a missing side. order_type values are lowercase, e.g. '
    '"market", "limit", "stop_market", not "MARKET", "LIMIT", "STOP_MARKET". '
    'Only use fields and actions available in the current schema. '
    'Example action shape (replace the placeholder with a real current order ID): '
    '{"action":"edit","order_id":"<current order_id>","price":2351.8,"reason":"adjust existing limit order"}. '
    'Close action example (use the intended side and quantity): '
    '{"action":"close","side":"SHORT","order_type":"market","amount":0.12,"reason":"close short position"}. '
    'Check each results item by index and its own status, order_status and filled_amount; '
    'batch success or submitted does not prove a fill. Never resubmit successful or unknown items '
    'to fix another item; unknown/pending results are verified in the background. '
)


def validate_action_identity(value: Any) -> Any:
    """Explain common invalid inputs before union dispatch without rewriting requests."""
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
    # Missing direction must be corrected explicitly, never inferred from the
    # account or another action. Order-type casing is handled by the field validator.
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
