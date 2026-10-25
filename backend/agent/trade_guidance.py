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
ACTIONS_DESCRIPTION = (
    '1–20 flat action objects. Every item requires action and reason; edit/cancel also require '
    'order_id. Put action, order_id and change fields at the same level, without an order wrapper. '
    'Use only fields published for the selected action and trading mode.'
)
TRADE_ARGUMENT_GUIDANCE = (
    'Set contract_version=3. Each actions item must contain action (not action_type) and reason. '
    'edit changes an existing order and requires its current order_id plus at least one supported '
    'change field; cancel also requires order_id. Copy order_id from current order evidence, '
    'preserving prefixes; operation_id is a request identity and cannot replace it. '
    'If the order ID is missing, query open orders before editing/cancelling. Never guess it. '
    'Use flat fields, not a nested order object. For edit, remaining_amount is the target unfilled '
    'base-coin quantity, price is the limit price, and trigger_price is a conditional-order trigger. '
    'Only use fields and actions available in the current schema. '
    'Example action shape (replace the placeholder with a real current order ID): '
    '{"action":"edit","order_id":"<current order_id>","price":2351.8,"reason":"adjust existing limit order"}. '
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
    return value
