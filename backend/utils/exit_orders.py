"""Versioned, exchange-independent semantics for independent exit orders.

Unversioned records retain the historical ordinary-limit meaning of
``take_profit_limit``. Only an explicit v2 contract selects conditional TP LIMIT.
"""
from __future__ import annotations

import math

ORDER_TYPES = frozenset({'market', 'limit', 'stop_market', 'stop_limit',
                         'take_profit_market', 'take_profit_limit'})
CONDITIONAL_TYPES = frozenset({'stop_market', 'stop_limit', 'take_profit_market', 'take_profit_limit'})
LIMIT_TYPES = frozenset({'limit', 'stop_limit', 'take_profit_limit'})
ALL_TYPES = frozenset({'market', 'stop_market', 'take_profit_market'})


def positive_number(value, name='amount', *, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a finite number')
    value = float(value)
    if not math.isfinite(value) or value < 0 or (not zero and value == 0):
        raise ValueError(f'{name} must be finite and {"nonnegative" if zero else "positive"}')
    return value


def canonical_order_type(record_or_type, contract_version=1):
    if isinstance(record_or_type, dict):
        if record_or_type.get('order_type'):
            return record_or_type['order_type']
        contract_version = record_or_type.get('contract_version', 1)
        record_or_type = record_or_type.get('exit_type', 'limit')
    if contract_version not in (1, 2):
        raise ValueError('Unsupported trading contract version')
    if contract_version == 1 and record_or_type == 'take_profit_limit':
        return 'limit'
    return record_or_type


normalize_exit_type = canonical_order_type


def is_conditional(record_or_type, contract_version=1):
    return canonical_order_type(record_or_type, contract_version) in CONDITIONAL_TYPES


def exit_group(record):
    kind = canonical_order_type(record)
    if kind.startswith('stop_'):
        return 'stop_loss'
    if kind.startswith('take_profit_'):
        return 'take_profit'
    return kind


def validate_exit_order(order_type, side, reference, price=None, trigger_price=None):
    if side not in {'LONG', 'SHORT'} or order_type not in ORDER_TYPES:
        raise ValueError('Invalid position side or exit type')
    positive_number(reference, 'current price')
    if order_type in LIMIT_TYPES:
        positive_number(price, 'limit price')
    elif price is not None:
        raise ValueError('Market exits do not accept a limit price')
    if order_type in CONDITIONAL_TYPES:
        positive_number(trigger_price, 'trigger price')
        higher = (side == 'LONG') == order_type.startswith('take_profit_')
        if not (trigger_price > reference if higher else trigger_price < reference):
            raise ValueError('Conditional trigger is on the wrong side of the current reference price')
    elif trigger_price is not None:
        raise ValueError('Regular exits do not accept trigger_price')


validate_exit = validate_exit_order


def normalize_close(exit_type, amount=None, close_all=False, contract_version=1):
    kind = canonical_order_type(exit_type, contract_version)
    if kind not in ORDER_TYPES:
        raise ValueError('Invalid exit type')
    if contract_version == 1:
        if close_all:
            raise ValueError('Legacy close does not accept close_all')
        amount = positive_number(amount, zero=True)
        return {'order_type': kind, 'quantity_mode': 'fixed', 'amount': amount, 'close_all': False}
    if not isinstance(close_all, bool):
        raise ValueError('close_all must be a boolean')
    if close_all:
        if amount is not None:
            raise ValueError('Choose exactly one of amount or close_all=true')
        if kind not in ALL_TYPES:
            raise ValueError('unsupported_combination: close_all requires market, stop_market or take_profit_market')
    else:
        amount = positive_number(amount)
    return {'order_type': kind, 'quantity_mode': 'all' if close_all else 'fixed',
            'amount': amount, 'close_all': close_all}


def parse_order_reference(value):
    value = str(value)
    channel, separator, raw = value.partition(':')
    if separator and channel in {'regular', 'algo', 'mock'}:
        if not raw:
            raise ValueError('Empty order identity')
        return channel, raw
    return None, value


def order_reference(record, current=True):
    child = record.get('execution_order_id') if current else None
    raw = child or record.get('exchange_order_id') or record.get('id') or record.get('order_id')
    if raw is None:
        return None
    channel = 'regular' if child else record.get('exchange_channel') or ('algo' if is_conditional(record) else 'regular')
    existing_channel, raw = parse_order_reference(raw)
    return f'{existing_channel or channel}:{raw}'


def observed_order_status(record):
    """Project stored evidence without treating a triggered parent as a fill."""
    if record.get('order_status'):
        return record['order_status']
    status = str(record.get('status') or 'unknown').lower()
    if status == 'closed':
        return 'filled'
    if status == 'cancelled':
        return 'canceled'
    if status == 'open':
        if record.get('waiting_trigger'):
            return 'waiting_trigger'
        if record.get('filled', 0):
            return 'partially_filled'
        if record.get('triggered'):
            return 'triggered'
    return status
