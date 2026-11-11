"""Read-only position lifetimes. Missing execution evidence is never a zero."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import math

from backend.utils.spot_valuation import apply_spot_fill


def numeric(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def timestamp(value):
    if value is None or value == '':
        return None
    if isinstance(value, (int, float)):
        return int(value if value > 1e11 else value * 1000)
    from backend.database import TZ_CN
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return int((parsed if parsed.tzinfo else TZ_CN.localize(parsed)).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def iso_time(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat() if value is not None else None


def new_position(config_id, mode, scope, event):
    identity = '|'.join(str(v) for v in (config_id, mode, scope, event['symbol'], event['side'], event['event_id']))
    return dict(position_id=hashlib.sha256(identity.encode()).hexdigest(), config_id=config_id,
                mode=mode, symbol=event['symbol'], side=event['side'], status='open',
                opened_at=None, closed_at=None, last_event_at=None, holding_seconds=None,
                entered_quantity=0.0, remaining_quantity=0.0, realized_pnl_before_fees=0.0,
                realized_net_pnl=0.0, fees_by_currency={}, currency=event['symbol'].split(':')[-1]
                if ':' in event['symbol'] else event['symbol'].split('/')[-1],
                complete=True, issues=[], events=[])


def build_positions(config_id: str, mode: str, scope: str, events: list[dict], *, now_ms: int) -> list[dict]:
    """Zero-to-zero grouping over a complete ordered stream, before UI filtering.

    Each normalized event retains its source identity. Coarse totals with no
    execution time remain isolated fragments, never inserted at order creation.
    """
    active, positions, seen, unplaced = {}, [], set(), set()
    ordered = sorted(events, key=lambda e: (not e.get('initial'), e.get('timestamp_ms') is None,
                     e.get('timestamp_ms') or 0, e.get('sequence', 0), str(e['event_id'])))
    for source in ordered:
        event = dict(source)
        key = (event['symbol'], event['side'])
        identity = (*key, event['event_id'])
        if identity in seen or (event.get('timestamp_ms') is not None and event['timestamp_ms'] > now_ms):
            continue
        seen.add(identity)
        isolated = (event.get('timestamp_ms') is None or event.get('ambiguous_time', False)) and not event.get('initial')
        if isolated:
            unplaced.add(key)
        row = None if isolated else active.get(key)
        if row is None:
            row = new_position(config_id, mode, scope, event)
            row.update(_cost=0.0, _deferred_fee=0.0, _basis_known=True, _fees_known=True,
                       _gross=0.0, _net=0.0, _gross_known=True, _net_known=True,
                       _quantity_known=True)
            positions.append(row)
            if not isolated:
                active[key] = row
        issues = list(event.get('issues') or [])
        if isolated:
            issues.append('missing_execution_time')
        when, kind = event.get('timestamp_ms'), event['kind']
        amount, price = numeric(event.get('quantity')), numeric(event.get('price'))
        before = row['remaining_quantity']
        event.update(timestamp=iso_time(when), quantity_before=before,
                     realized_pnl_before_fees=None, realized_net_pnl=None)
        parts = event.get('fees') or []
        fee_known = bool(parts) and all(numeric(f.get('cost')) is not None and f.get('currency') for f in parts)
        for fee in parts:
            value, currency = numeric(fee.get('cost')), fee.get('currency')
            if value is not None and currency:
                row['fees_by_currency'][currency] = row['fees_by_currency'].get(currency, 0.0) + value
        if kind == 'calibration':
            row['remaining_quantity'] = amount
            row['_cost'] = event.get('cost')
            row['_basis_known'] = amount is not None and numeric(event.get('cost')) is not None
            row['_quantity_known'] = amount is not None
            row['_fees_known'] = row['_basis_known']
            row['_deferred_fee'] = 0.0
            issues.append('cost_calibration')
            if not row['events']:
                issues.append('missing_entry_history')
                row['_net_known'] = False
                if mode == 'SPOT_DCA':
                    row['_gross_known'] = False
            if amount == 0:
                row['status'] = 'unconfirmed'
                active.pop(key, None)
        elif amount is None or amount <= 0 or kind not in {'entry', 'exit'}:
            issues.append('unknown_execution')
            row['_net_known'] = row['_gross_known'] = row['_quantity_known'] = False
        else:
            first = not row['events']
            if kind == 'entry':
                row['entered_quantity'] += amount
                event['kind'] = 'entry' if first or before == 0 else 'add'
                if first and not isolated:
                    row['opened_at'] = iso_time(when)
            elif first:
                issues.append('missing_entry_history')
                row['_quantity_known'] = row['_basis_known'] = False
                row['_net_known'] = False
                if mode == 'SPOT_DCA':
                    row['_gross_known'] = False
            if not fee_known:
                issues.append('incomplete_fees')
            if mode == 'SPOT_DCA':
                base, quote = event['symbol'].split('/')
                fee_map = {}
                for fee in parts:
                    if numeric(fee.get('cost')) is not None and fee.get('currency'):
                        fee_map[fee['currency']] = fee_map.get(fee['currency'], 0.0) + numeric(fee['cost'])
                supported = not (set(fee_map) - {base, quote})
                if not supported:
                    issues.append('fee_currency_conversion_required')
                # The current spot schema stores one fee per fill. The helper
                # is the same cost step used by the live holding valuation.
                fee = parts[0] if len(parts) == 1 else {}
                value = numeric(event.get('cost'))
                if not row['_basis_known'] or value is None or isolated:
                    issues.append('cost_basis_unavailable')
                    row['_basis_known'] = False
                    step = None
                else:
                    try:
                        step = apply_spot_fill(dict(side='buy' if kind == 'entry' else 'sell',
                            quantity=amount, cost=value, fee=fee.get('cost'), fee_currency=fee.get('currency')),
                            before, row['_cost'], event['symbol'])
                    except ValueError:
                        issues.append('quantity_mismatch')
                        row['_basis_known'] = row['_quantity_known'] = False
                        step = None
                if step:
                    converted_fee = (fee_map.get(quote, 0.0) + fee_map.get(base, 0.0) * price
                                     if price is not None and fee_known and supported else None)
                    if kind == 'entry':
                        row['_fees_known'] &= fee_known and supported
                        row['_deferred_fee'] += converted_fee or 0.0
                        event.update(realized_pnl_before_fees=0.0, realized_net_pnl=0.0)
                    else:
                        net = step['realized_pnl']
                        allocated_fee = row['_deferred_fee'] * step['disposed_fraction']
                        event['realized_net_pnl'] = net if row['_fees_known'] and fee_known and supported else None
                        event['realized_pnl_before_fees'] = (net + allocated_fee + converted_fee
                            if row['_fees_known'] and converted_fee is not None else None)
                        row['_deferred_fee'] -= allocated_fee
                    row['remaining_quantity'], row['_cost'] = step['quantity'], step['remaining_cost']
                elif kind == 'entry' and row['_quantity_known']:
                    row['remaining_quantity'] = before + amount - fee_map.get(base, 0.0)
                else:
                    row['_quantity_known'] = False
            else:
                if row['_quantity_known']:
                    remaining = before + amount if kind == 'entry' else before - amount
                    if remaining < -max(1e-12, before * 1e-9):
                        issues.append('quantity_mismatch')
                        row['_quantity_known'] = False
                    else:
                        row['remaining_quantity'] = max(remaining, 0.0)
                pnl = numeric(source.get('pnl'))
                if pnl is None:
                    issues.append('missing_pnl')
                supported = all(f.get('currency') == row['currency'] for f in parts)
                if not supported and parts:
                    issues.append('fee_currency_conversion_required')
                event['realized_pnl_before_fees'] = pnl
                event['realized_net_pnl'] = (pnl - sum(numeric(f['cost']) for f in parts)
                    if pnl is not None and fee_known and supported else None)
            if kind == 'exit' and row['_quantity_known'] and row['remaining_quantity'] <= 1e-12 and not isolated:
                row['status'] = 'closed'
                row['closed_at'] = iso_time(when)
                event['kind'] = 'close'
                active.pop(key, None)
            elif kind == 'exit':
                event['kind'] = 'reduce'
            for field, total, known in [('realized_pnl_before_fees', '_gross', '_gross_known'),
                                        ('realized_net_pnl', '_net', '_net_known')]:
                if event[field] is None:
                    row[known] = False
                else:
                    row[total] += event[field]
        if not row['_quantity_known']:
            row['remaining_quantity'] = None
            row['status'] = 'unconfirmed'
        event['quantity_after'] = row['remaining_quantity']
        if isolated:
            row['status'] = 'unconfirmed'
            row['_net_known'] = row['_gross_known'] = False
        if set(issues) & {'foreign_activity', 'history_incomplete', 'conflicting_evidence'}:
            row['_net_known'] = False
            event['realized_net_pnl'] = None
            row['status'] = 'unconfirmed'
        event['issues'] = sorted(set(issues))
        row['issues'] = sorted(set(row['issues']) | set(issues))
        row['events'].append(event)
        if when is not None:
            row['last_event_at'] = iso_time(when)
    for row in positions:
        if (row['symbol'], row['side']) in unplaced:
            row['issues'] = sorted(set(row['issues']) | {'missing_execution_time'})
            row['_net_known'] = False
            if row['status'] == 'open':
                row['status'] = 'unconfirmed'
            for event in row['events']:
                if event['kind'] in {'exit', 'reduce', 'close'}:
                    event['realized_net_pnl'] = None
                    event['issues'] = sorted(set(event['issues']) | {'missing_execution_time'})
        row['realized_pnl_before_fees'] = row['_gross'] if row['_gross_known'] else None
        row['realized_net_pnl'] = row['_net'] if row['_net_known'] else None
        if 'missing_entry_history' in row['issues'] or 'unknown_execution' in row['issues']:
            row['entered_quantity'] = None
        row['complete'] = not row['issues']
        if row['opened_at'] and row['status'] != 'unconfirmed':
            end = timestamp(row['closed_at']) if row['closed_at'] else now_ms
            row['holding_seconds'] = max(0, (end - timestamp(row['opened_at'])) / 1000)
        for field in [field for field in row if field.startswith('_')]:
            del row[field]
    return sorted(positions, key=lambda r: (r['status'] == 'open', timestamp(r['last_event_at']) or 0,
                                           r['position_id']), reverse=True)
