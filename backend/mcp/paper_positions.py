"""Atomic adjustments to one identified current Paper perpetual position."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator

from . import paper, paper_orders, store
from .paper_lifecycle import capture_epoch
from .paper_policy import require_target, visible
from .paper_storage import bounded


class PositionAdjustment(BaseModel):
    model_config = ConfigDict(extra='forbid')

    operation_id: str = Field(min_length=8, max_length=160, pattern=r'^[a-zA-Z0-9_.:-]+$')
    action: Literal['set_leverage', 'add_margin', 'reduce_margin']
    leverage: StrictInt | None = Field(None, ge=1, le=125)
    amount: StrictFloat | None = Field(None, gt=0, le=1e15, allow_inf_nan=False)

    @model_validator(mode='after')
    def validate_action_amount(self):
        if self.action == 'set_leverage':
            if self.leverage is None or self.amount is not None:
                raise ValueError('set_leverage requires leverage and does not accept amount')
        elif self.amount is None or self.leverage is not None:
            raise ValueError('Margin adjustments require amount and do not accept leverage')
        if self.amount is not None:
            amount = Decimal(str(self.amount))
            if amount != amount.quantize(Decimal('.00000001')):
                raise ValueError('Margin amounts support up to 8 decimal places')
        return self


def _position(account: dict, position_id: str, conn) -> tuple[str, dict]:
    matches = [(key, position) for key, position in account['positions'].items()
               if position.get('resource_id') == position_id]
    if len(matches) != 1:
        raise ValueError('Current position_id was not found uniquely; read the current positions again')
    key, position = matches[0]
    require_target(position, account=account, kind='positions', key=key, conn=conn)
    if not position.get('cycle_id'):
        raise ValueError('Position cycle is unverified; the position cannot be adjusted')
    return key, position


def _assert_enabled(account: dict) -> None:
    if not account['enabled'] or 'swap' not in account['markets']:
        raise ValueError('Paper account or perpetual market is disabled')


def _check_pending_entries(conn, account: dict, position: dict, leverage: int) -> None:
    conflicting = [order['order_id'] for order in paper_orders.rows(conn, account['namespace'], account['user_id'])
                   if order['market_type'] == 'swap'
                   and order['symbol'] == position['symbol']
                   and order['action'] == 'open_' + position['side'].lower()
                   and visible(order, account=account, conn=conn)
                   and order.get('leverage') != leverage]
    if conflicting:
        raise ValueError('Cancel or update conflicting entry orders before changing position leverage: '
                         + ', '.join(conflicting))


@bounded
@capture_epoch
def manage(caller: dict, user_id: str, position_id: str, operation_id: str,
           action: str, leverage: int | None = None, amount: float | None = None) -> dict:
    adjustment = PositionAdjustment(operation_id=operation_id, action=action, leverage=leverage, amount=amount)
    paper.identity(position_id, 'position_id')
    request = {'action': adjustment.action, 'position_id': position_id,
               'leverage': adjustment.leverage, 'amount': adjustment.amount}
    previous = paper_orders.existing_receipt(caller, user_id, operation_id, request)
    if previous is not None:
        return previous
    namespace = paper.require(caller, 'trade')
    with store.connection() as conn:
        account = paper._get(conn, namespace, user_id)
        _assert_enabled(account)
        _, observed = _position(account, position_id, conn)
        expected_cycle, symbol, side = observed['cycle_id'], observed['symbol'], observed['side']
        conn.commit()
    # Public network reads are outside the account write transaction. Identity,
    # current funds, configuration and quote freshness are rechecked at commit.
    quote = paper_orders.get_quote(symbol, 'swap', 'binance')

    def change(account, conn):
        _assert_enabled(account)
        _, position = _position(account, position_id, conn)
        if (position['cycle_id'], position['symbol'], position['side']) != (expected_cycle, symbol, side):
            raise ValueError('Position cycle changed while fetching a quote; inspect the current position')
        if quote['exchange'] != 'binance' or quote['symbol'] != symbol or quote['market_type'] != 'swap':
            raise ValueError('Position adjustment quote does not match the Binance perpetual position')
        if time.time() - quote['observed_at'] > 15:
            raise paper_orders.PaperQuoteExpired('Paper execution quote expired; no position adjustment recorded')
        config = paper.settings(conn)
        old_leverage = position['leverage']
        new_leverage = adjustment.leverage if adjustment.action == 'set_leverage' else old_leverage
        if (adjustment.action == 'set_leverage' and new_leverage != old_leverage
                and new_leverage > config['max_leverage']):
            raise ValueError('Leverage exceeds the configured paper maximum')
        old_margin = paper.number(position['margin'])
        added = paper.number(position.get('added_margin', 0))
        quantity = paper.number(position['amount'], positive=True)
        entry = paper.number(position['entry_price'], positive=True)
        base_margin = Decimal(str(paper.money(entry * quantity / new_leverage)))
        if adjustment.action == 'set_leverage':
            if new_leverage != old_leverage:
                _check_pending_entries(conn, account, position, new_leverage)
                new_margin = base_margin + added
            else:
                new_margin = old_margin
        else:
            amount_value = paper.number(adjustment.amount, positive=True)
            delta = amount_value if adjustment.action == 'add_margin' else -amount_value
            new_margin, added = old_margin + delta, added + delta
            if adjustment.action == 'reduce_margin' and (added < 0 or new_margin < base_margin):
                raise ValueError('Margin cannot fall below the leverage base; only manually added margin can be reduced')
        new_margin = Decimal(str(paper.money(new_margin)))
        if time.time() - quote['observed_at'] > 15:
            raise paper_orders.PaperQuoteExpired('Paper execution quote expired; no position adjustment recorded')
        mark = paper.number(quote['price'], positive=True)
        unrealized = (mark - entry) * quantity * (1 if side == 'LONG' else -1)
        maintenance = mark * quantity * paper.number(config['maintenance_margin_rate'])
        if new_margin + unrealized <= maintenance:
            raise ValueError('Adjusted margin would immediately trigger liquidation; retain more margin')
        cash = paper.signed_cash(account['cash'])
        reserved = paper.number(account.get('reserved_cash', 0))
        required = new_margin - old_margin
        if required > 0 and required > cash - reserved:
            raise ValueError('Insufficient unreserved cash for the position adjustment')
        account['cash'] = paper.money(cash - required)
        position.update(leverage=new_leverage, margin=paper.money(new_margin), added_margin=paper.money(added),
                        mark_price=paper.money(mark), marked_at=quote['observed_at'])
        presented = {**deepcopy(position), 'position_id': position_id, 'base_margin': paper.money(base_margin),
                     'unrealized_pnl': paper.money(unrealized), 'margin_equity': paper.money(new_margin + unrealized)}
        return {'action': adjustment.action, 'position_id': position_id, 'resource_id': position_id,
                'symbol': symbol, 'side': side, 'market_type': 'swap', 'exchange': 'binance',
                'origin_version': 1, 'cycle_id': expected_cycle,
                'leverage_before': old_leverage, 'leverage_after': new_leverage,
                'margin_before': paper.money(old_margin), 'margin_after': paper.money(new_margin),
                'base_margin': paper.money(base_margin), 'added_margin': paper.money(added),
                'cash_change': paper.money(-required), 'available_cash': paper.money(max(cash - required - reserved, paper.ZERO)),
                'currency': 'USDT', 'position': presented}

    return paper._write(caller, user_id, operation_id, request, change)
