"""Validate timestamped Binance ratio evidence without fabricating neutral values."""

import math


def ratio_evidence(rows: list, field: str, now_ms: int) -> dict:
    points = {}
    for row in rows or []:
        try:
            timestamp = int(row['timestamp'])
            value = float(row[field])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and value >= 0 and 0 < timestamp <= now_ms:
            points[timestamp] = value
    if not points:
        return {'available': False, 'reason': 'missing or invalid data'}
    ordered = sorted(points.items())
    timestamp, value = ordered[-1]
    age = (now_ms - timestamp) / 1000
    result = {'available': age <= 900, 'value': value, 'timestamp_ms': timestamp,
              'age_seconds': round(age), 'stale': age > 900, 'period': '5m'}
    previous = points.get(timestamp - 300000)
    result['change_5m_pct'] = (value / previous - 1) * 100 if previous and previous > 0 else None
    return result
