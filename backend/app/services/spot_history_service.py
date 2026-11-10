"""Historical holding ROI never derives from growth in invested capital."""
from backend import storage
from backend.database import TZ_CN
from datetime import datetime


def spot_history_points(conn, config_id: str, symbols: list[str]) -> list[dict]:
    if not symbols:
        return []
    placeholders = ','.join('%s' for _ in symbols)
    rows = conn.execute(f'''SELECT d.snapshot_date,d.symbol,d.total_invested,
        d.remaining_cost,d.market_value,d.holding_return_pct,d.cost_basis_revision,d.valuation_status,
        b.updated_at AS calibrated_at
        FROM dca_daily_snapshots d LEFT JOIN spot_cost_basis_revisions b
          ON b.config_id=d.config_id AND b.symbol=d.symbol AND b.revision=d.cost_basis_revision
        WHERE d.config_id=%s AND d.symbol IN ({placeholders})
        ORDER BY d.snapshot_date,d.symbol''', (config_id, *symbols)).fetchall()
    dates = {}
    for row in rows:
        dates.setdefault(row['snapshot_date'], []).append(storage.record(row))
    result = []
    for day, items in dates.items():
        complete = len(items) == len(symbols)
        cost = sum(r['remaining_cost'] for r in items) if complete and all(r['remaining_cost'] is not None for r in items) else None
        value = sum(r['market_value'] for r in items) if complete and all(r['market_value'] is not None for r in items) else None
        revision = '|'.join(f"{r['symbol']}:{r['cost_basis_revision']}" for r in items)
        calibrations = [dict(symbol=r['symbol'],updated_at=r['calibrated_at']) for r in items if r['calibrated_at']]
        def calibration_date(item):
            stamp = datetime.fromisoformat(item['updated_at'].replace('Z','+00:00'))
            return (TZ_CN.localize(stamp) if stamp.tzinfo is None else stamp.astimezone(TZ_CN)).date().isoformat()
        calibrated = any(calibration_date(item) == str(day) for item in calibrations)
        result.append(dict(date=day, equity=value, remaining_cost=cost,
                           invested=sum(r['total_invested'] for r in items) if complete else None,
                           holding_return_pct=(value-cost)/cost*100 if cost is not None and cost>0 and value is not None else None,
                           cost_basis_revision=revision, calibrated=calibrated, calibrations=calibrations,
                           valuation_status='ready' if cost is not None and cost>0 and value is not None else 'historical_valuation_unavailable'))
    return result


def spot_history_source(config_id: str, symbols: list[str]) -> dict:
    return dict(table='dca_daily_snapshots', field='holding_return_pct', scope='config_id',
                config_id=config_id, symbols=symbols, symbol=symbols[0] if len(symbols)==1 else None,
                label='dca_daily_snapshots.holding_return_pct', display_label='现货持仓收益与成本快照',
                kind='spot_holding', return_basis='holding_remaining_cost')
