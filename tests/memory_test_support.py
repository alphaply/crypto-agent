"""Publish text fixtures through the production source/hash contract."""
from backend import database
from backend.agent import context_projections as projections
from backend.config import config


def publish_strategy_views(monkeypatch, config_id='cfg', symbol='ETH/USDT'):
    cfg = dict(config_id=config_id, symbol=symbol, mode='STRATEGY')
    monkeypatch.setattr(config, 'get_config_by_id', lambda cid: cfg if cid == config_id else None)
    with database.get_db_conn() as conn:
        conn.execute('UPDATE summaries SET symbol=%s WHERE config_id=%s AND symbol IS NULL', (symbol, config_id))
        conn.commit()
    for source in projections.current_sources(config_id):
        refs = projections.source_refs(config_id, source)
        with database.get_db_conn() as conn:
            projections.publish_text(conn, config_id, source['source_kind'], source['row'], refs)
            conn.commit()
