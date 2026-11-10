"""Binance USD-M returns on starting equity, excluding verified transfers.

Only the background synchronizer makes exchange requests. Readers never infer
zero flows from absent data or switch to unadjusted returns when evidence fails.
"""
from __future__ import annotations

from bisect import bisect_right
import calendar
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import math
import time

from backend import database


BASIS = 'net_profit_over_starting_equity'
DAY_MS = 86_400_000
PAGE_SIZE = 1000


def timestamp_ms(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        stamp = database.TZ_CN.localize(stamp)
    return int(stamp.timestamp() * 1000)


def account_scope(api_key: str) -> str:
    if not isinstance(api_key, str) or not api_key:
        raise ValueError('account_unavailable')
    return hashlib.sha256(f'binanceusdm:{api_key}'.encode()).hexdigest()


def configured_scope(config_id: str, config_source=None) -> str:
    if config_source is None:
        from backend.config import config as config_source
    cfg = config_source.get_config_by_id(config_id)
    if not cfg or cfg.get('mode') != 'REAL' or cfg.get('market_type', 'swap') != 'swap':
        raise ValueError('unsupported_account')
    exchange, key, _, _ = config_source.get_exchange_credentials(config_id=config_id)
    if exchange != 'binance':
        raise ValueError('unsupported_exchange')
    return account_scope(key)


def merged_intervals(intervals) -> list[list[int]]:
    result = []
    for start, end in sorted(intervals):
        if not result or start > result[-1][1] + 1:
            result.append([start, end])
        else:
            result[-1][1] = max(result[-1][1], end)
    return result


def first_gap(start: int, end: int, intervals) -> tuple[int, int] | None:
    if start > end:
        return None
    for left, right in merged_intervals(intervals):
        if right < start:
            continue
        if left > start:
            return start, min(end, left - 1)
        start = max(start, right + 1)
        if start > end:
            return None
    return (start, end) if start <= end else None


def adjusted_points(rows: list[dict], flows: list[dict], coverage, *, reason=None) -> list[dict]:
    """Keep actual equity and a separate, flow-neutral performance series."""
    events = sorted(flows, key=lambda flow: flow['timestamp_ms'])
    times, sums = [], [Decimal(0)]
    for event in events:
        times.append(int(event['timestamp_ms']))
        sums.append(sums[-1] + Decimal(str(event['amount'])))
    intervals = merged_intervals(coverage)
    baseline = baseline_time = None
    result = []
    for row in rows:
        point = dict(row, cashflow_return_pct=None, adjusted_equity=None,
                     net_transfers=None, net_profit=None, return_status='pending',
                     return_reason=reason or 'cashflow_history_incomplete', return_basis=BASIS)
        try:
            equity = float(row['equity'])
            when = timestamp_ms(row['timestamp'])
        except (KeyError, ValueError, TypeError, OverflowError):
            point['return_reason'] = 'invalid_snapshot'
            result.append(point)
            continue
        if not math.isfinite(equity):
            point['return_reason'] = 'invalid_snapshot'
        elif baseline is None and equity <= 0:
            point['return_reason'] = 'positive_baseline_required'
        else:
            if baseline is None:
                baseline, baseline_time = equity, when
            if reason is None and first_gap(baseline_time + 1, when, intervals) is None:
                flow = sums[bisect_right(times, when)] - sums[bisect_right(times, baseline_time)]
                adjusted = Decimal(str(equity)) - flow
                profit = adjusted - Decimal(str(baseline))
                point.update(cashflow_return_pct=float(profit / Decimal(str(baseline)) * 100),
                             adjusted_equity=float(adjusted), net_transfers=float(flow),
                             net_profit=float(profit), return_status='ready', return_reason=None)
        result.append(point)
    return result


def read_adjusted_points(conn, config_id: str, rows: list[dict], *, config_source=None) -> list[dict]:
    if not rows:
        return []
    try:
        scope = configured_scope(config_id, config_source)
    except (ValueError, AttributeError, KeyError):
        return adjusted_points(rows, [], [], reason='account_or_exchange_unavailable')
    # Existing REAL snapshots are denominated in the USDT futures wallet.
    coverage = [(r['start_ms'], r['end_ms']) for r in conn.execute(
        'SELECT start_ms,end_ms FROM real_cashflow_coverage WHERE account_scope=%s', (scope,))]
    flows = [dict(r) for r in conn.execute(
        'SELECT timestamp_ms,amount FROM real_cashflows WHERE account_scope=%s AND currency=%s '
        'AND timestamp_ms>%s AND timestamp_ms<=%s ORDER BY timestamp_ms,transaction_id',
        (scope, 'USDT', timestamp_ms(rows[0]['timestamp']), timestamp_ms(rows[-1]['timestamp'])))]
    return adjusted_points(rows, flows, coverage)


def real_history_points(conn, config_id: str, *, config_ids=None, symbol=None, config_source=None) -> list[dict]:
    rows = conn.execute('''SELECT day,timestamp,total_equity FROM (
        SELECT substring(timestamp from 1 for 10) AS day,timestamp,total_equity,
            row_number() OVER (PARTITION BY substring(timestamp from 1 for 10) ORDER BY timestamp DESC,id DESC) AS rn
        FROM balance_history WHERE config_id=ANY(%s) AND (%s::text IS NULL OR symbol=%s)
            AND total_equity IS NOT NULL
        ) history WHERE rn=1 ORDER BY timestamp''', (config_ids or [config_id], symbol, symbol)).fetchall()
    points = [dict(date=r['day'], timestamp=r['timestamp'], equity=r['total_equity']) for r in rows]
    return read_adjusted_points(conn, config_id, points, config_source=config_source)


def return_metadata(points: list[dict]) -> dict:
    last = points[-1]
    return dict(data_state='ready', point_count=len(points), first_date=points[0]['date'],
                last_date=last['date'], latest_equity=last['equity'],
                return_pct=last['cashflow_return_pct'], return_basis=BASIS,
                return_status=last['return_status'], return_reason=last['return_reason'],
                net_transfers=last['net_transfers'], net_profit=last['net_profit'])


def adjusted_metrics(points: list[dict]) -> dict:
    from backend.utils.performance_context import equity_metrics
    usable = [p for p in points if p['return_reason'] != 'positive_baseline_required']
    if not usable or any(p['return_status'] != 'ready' for p in usable):
        return dict(available=False, samples=len(usable), basis=BASIS, return_status='pending',
                    return_reason=next((p['return_reason'] for p in usable if p['return_status'] != 'ready'), 'no_snapshots'))
    metric = equity_metrics([dict(timestamp=p['timestamp'], total_equity=p['adjusted_equity']) for p in usable])
    for name, point in [('start', usable[0]), ('latest', usable[-1])]:
        metric[name] = dict(timestamp=point['timestamp'], equity=point['equity'], adjusted_equity=point['adjusted_equity'])
    metric.update(change=usable[-1]['net_profit'], net_transfers=usable[-1]['net_transfers'],
                  basis=BASIS, return_status='ready', return_reason=None)
    return metric


def _retention_start(now_ms: int) -> int:
    now = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
    month = now.year * 12 + now.month - 1 - 3
    year, index = divmod(month, 12)
    prior = now.replace(year=year, month=index + 1, day=min(now.day, calendar.monthrange(year, index + 1)[1]))
    return int(prior.timestamp() * 1000) + 1


def _validate_page(raw, start: int, end: int) -> list[tuple]:
    if not isinstance(raw, list) or len(raw) > PAGE_SIZE:
        raise ValueError('invalid_cashflow_response')
    events = {}
    for row in raw:
        if not isinstance(row, dict) or row.get('incomeType') != 'TRANSFER':
            raise ValueError('unexpected_income_type')
        try:
            amount = Decimal(str(row['income']))
            raw_time = Decimal(str(row['time']))
            when = int(raw_time)
            identity, currency = str(row['tranId']), row['asset']
        except (KeyError, TypeError, ValueError, OverflowError, InvalidOperation):
            raise ValueError('invalid_cashflow_event') from None
        if (not amount.is_finite() or not start <= when <= end or not identity or identity == 'None'
                or not isinstance(currency, str) or not currency or raw_time != when
                or isinstance(row['time'], bool) or isinstance(row['tranId'], bool)):
            raise ValueError('invalid_cashflow_event')
        event = ('TRANSFER', identity, currency, when, amount)
        if identity in events and events[identity] != event:
            raise ValueError('conflicting_cashflow_event')
        events[identity] = event
    return list(events.values())


def _save_page(scope: str, start: int, end: int, events: list[tuple], now_ms: int) -> None:
    from backend.storage import lock_scope
    with database.get_db_conn() as conn:
        lock_scope(conn, 'cashflow-coverage', scope)
        for kind, identity, currency, when, amount in events:
            conn.execute('''INSERT INTO real_cashflows(account_scope,income_type,transaction_id,currency,timestamp_ms,amount)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''', (scope, kind, identity, currency, when, amount))
            saved = conn.execute('''SELECT currency,timestamp_ms,amount FROM real_cashflows
                WHERE account_scope=%s AND income_type=%s AND transaction_id=%s''', (scope, kind, identity)).fetchone()
            if (saved['currency'], saved['timestamp_ms'], saved['amount']) != (currency, when, amount):
                raise ValueError('conflicting_cashflow_event')
        overlapping = conn.execute('''DELETE FROM real_cashflow_coverage WHERE account_scope=%s
            AND start_ms<=%s AND end_ms>=%s RETURNING start_ms,end_ms''', (scope, end + 1, start - 1)).fetchall()
        start = min([start, *[r['start_ms'] for r in overlapping]])
        end = max([end, *[r['end_ms'] for r in overlapping]])
        conn.execute('''INSERT INTO real_cashflow_coverage(account_scope,start_ms,end_ms,checked_at_ms)
            VALUES(%s,%s,%s,%s)''', (scope, start, end, now_ms))
        conn.commit()


def sync_binance_cashflows(exchange, config_id: str, *, now_ms=None, max_requests=6) -> dict:
    """Read-only exchange synchronization, bounded and shared across same-key tasks.

    Binance limits income history to three months. A full page is split by time
    rather than assumed complete; an indivisible full page remains unverified.
    """
    if getattr(exchange, 'id', None) != 'binanceusdm':
        return {'status': 'unsupported_exchange'}
    scope = account_scope(exchange.apiKey)
    if configured_scope(config_id) != scope:
        raise ValueError('account_changed')
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    # Leave the newest minute pending so a balance snapshot is not treated as
    # reconciled before the exchange has had time to publish its transfer row.
    end = now_ms - 60_000
    with database.get_db_conn() as conn:
        first = conn.execute('SELECT MIN(timestamp) FROM balance_history WHERE config_id=%s AND total_equity>0',
                             (config_id,)).fetchone()[0]
        if not first:
            return {'status': 'no_snapshots'}
        start = max(timestamp_ms(first) + 1, _retention_start(now_ms))
        if start > end:
            return {'status': 'pending'}
        claimed = conn.execute('''INSERT INTO real_cashflow_sync(account_scope,attempted_at_ms,lease_until_ms,status)
            VALUES(%s,%s,%s,'syncing') ON CONFLICT(account_scope) DO UPDATE
            SET attempted_at_ms=excluded.attempted_at_ms,lease_until_ms=excluded.lease_until_ms,status='syncing',error_code=NULL
            WHERE real_cashflow_sync.lease_until_ms<%s AND real_cashflow_sync.attempted_at_ms<%s RETURNING query_span_ms''',
            (scope, now_ms, now_ms + 120_000, now_ms, now_ms - 30_000)).fetchone()
        conn.commit()
        if not claimed:
            return {'status': 'busy'}
        span = claimed['query_span_ms']
        intervals = [(r['start_ms'], r['end_ms']) for r in conn.execute(
            'SELECT start_ms,end_ms FROM real_cashflow_coverage WHERE account_scope=%s', (scope,))]
    requests = 0
    outcome, error = 'pending', None
    deadline = time.monotonic() + 60
    try:
        while requests < max_requests and time.monotonic() < deadline:
            gap = first_gap(start, end, intervals)
            if gap is None:
                outcome = 'ready'
                break
            left, right = gap[0], min(gap[1], gap[0] + span - 1)
            raw = exchange.fapiPrivateGetIncome({'incomeType': 'TRANSFER', 'startTime': left,
                                                 'endTime': right, 'limit': PAGE_SIZE})
            requests += 1
            events = _validate_page(raw, left, right)
            if len(raw) == PAGE_SIZE:
                if left == right:
                    raise ValueError('cashflow_page_overflow')
                # Persist the smaller range across bounded runs. Otherwise a
                # dense first page could consume every run without progress.
                span = max(1, (right - left + 1) // 2)
                continue
            _save_page(scope, left, right, events, now_ms)
            intervals.append((left, right))
            span = min(7 * DAY_MS, span * 2)
        if first_gap(start, end, intervals) is None:
            outcome = 'ready'
    except Exception as exc:
        outcome, error = 'error', type(exc).__name__
    finally:
        with database.get_db_conn() as conn:
            conn.execute('''UPDATE real_cashflow_sync SET lease_until_ms=0,status=%s,error_code=%s,query_span_ms=%s
                WHERE account_scope=%s AND attempted_at_ms=%s''',
                (outcome, error, 7 * DAY_MS if outcome == 'ready' else span, scope, now_ms))
            conn.commit()
    return {'status': outcome, 'error': error, 'requests': requests}
