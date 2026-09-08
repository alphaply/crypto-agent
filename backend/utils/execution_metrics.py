"""Observed excursions and fixed-horizon post-exit prices, with sampling limits."""
import json
import time

from backend.database import get_db_conn
from backend.utils.execution_ledger import number


def observe(service, plan, position, reference=None):
    episode_id = plan.get('episode_id')
    if not episode_id:
        return
    now = int(time.time() * 1000)
    with get_db_conn() as conn:
        row = conn.execute('SELECT payload FROM execution_episodes WHERE episode_id=?', (episode_id,)).fetchone()
        value = json.loads(row['payload']) if row else {}
        if position:
            entry = number(position.get('entryPrice'))
            if not value:
                value = {'first_observed_at': now, 'side': plan['side'], 'samples': 0, 'post_exit': {},
                         'stop_distance_atr': next((e.get('risk_evidence', {}).get('stop_distance_atr')
                                                    for e in plan.get('entries', []) if e.get('risk_evidence')), None)}
            pnl = number(position.get('unrealizedPnl'))
            if pnl is None and entry and reference:
                size = float(service.ex.market(plan['symbol']).get('contractSize') or 1)
                direction = 1 if plan['side'] == 'LONG' else -1
                pnl = (reference - entry) * float(position['contracts']) * size * direction
            value.update(last_observed_at=now, entry_price=entry, samples=value['samples'] + 1,
                         reference_basis=plan.get('trigger_basis', 'last'))
            if pnl is not None:
                value['max_observed_unrealized_pnl'] = max(value.get('max_observed_unrealized_pnl', pnl), pnl)
                value['min_observed_unrealized_pnl'] = min(value.get('min_observed_unrealized_pnl', pnl), pnl)
            if reference is not None:
                value['last_observed_price'] = reference
        elif value and not value.get('flat_observed_at'):
            value.update(flat_observed_at=now, exit_reason=plan.get('exit_reason', 'unknown'))
        if value:
            conn.execute('INSERT INTO execution_episodes VALUES(?,?,?,?,?) ON CONFLICT(episode_id) DO UPDATE SET payload=excluded.payload',
                         (episode_id, service.account_scope, service.config_id, plan['symbol'], json.dumps(value)))
            conn.commit()


def update_post_exit(exchange, scope, symbol, now_ms=None):
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    with get_db_conn() as conn:
        rows = conn.execute('SELECT episode_id,payload FROM execution_episodes WHERE account_scope=? AND symbol=?',
                            (scope, symbol)).fetchall()
    for row in rows:
        value = json.loads(row['payload'])
        closed = value.get('flat_observed_at')
        if not closed or now - closed > 7 * 86400000:
            continue
        horizons = [minutes for minutes in (15, 60) if now >= closed + minutes * 60000
                    and str(minutes) not in value.get('post_exit', {})]
        if not horizons:
            continue
        # Query closed one-minute bars around the observation time. This is post-exit
        # market context, not proof that a different stop would have been profitable.
        bars = exchange.fetch_ohlcv(symbol, '1m', since=closed // 60000 * 60000, limit=65)
        for minutes in horizons:
            target = closed + minutes * 60000
            candidates = [b for b in bars if target <= int(b[0]) + 60000 <= now]
            if not candidates:
                continue
            bar = min(candidates, key=lambda b: b[0])
            if int(bar[0]) + 60000 - target > 60000:
                continue
            value.setdefault('post_exit', {})[str(minutes)] = {
                'close_price': float(bar[4]), 'bar_close_ms': int(bar[0]) + 60000,
                'basis': 'last_price_1m_close_after_flat_observation',
            }
        with get_db_conn() as conn:
            conn.execute('UPDATE execution_episodes SET payload=? WHERE episode_id=?', (json.dumps(value), row['episode_id']))
            conn.commit()


def metrics_context(config_id, start_ms, end_ms, scope=None, symbol=None):
    with get_db_conn() as conn:
        rows = conn.execute('SELECT payload FROM execution_episodes WHERE config_id=? AND (? IS NULL OR account_scope=?) '
                            'AND (? IS NULL OR symbol=?)',
                            (config_id, scope, scope, symbol, symbol)).fetchall()
    values = [json.loads(row['payload']) for row in rows]
    values = [v for v in values if start_ms <= v.get('flat_observed_at', v.get('last_observed_at', 0)) < end_ms]
    return {'observation_count': len(values), 'details': values[-20:], 'omitted_details': max(0, len(values) - 20),
            'limits': '浮盈浮亏为维护任务采样极值，非逐笔真实MFE/MAE；首次发现仓位时间不是开仓成交时间；退出后价格不证明原交易应继续持有。'}
