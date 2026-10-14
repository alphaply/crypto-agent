"""Publish strategy test fixtures through the production source/hash contract."""
from backend import database
from backend.agent import context_projections as projections
from backend.agent.strategy_material import strategy_material, validate_strategy_text
from backend.config import config


def publish_strategy_views(monkeypatch, config_id='cfg', symbol='ETH/USDT'):
    cfg = dict(config_id=config_id, symbol=symbol, mode='STRATEGY')
    monkeypatch.setattr(config, 'get_config_by_id', lambda cid: cfg if cid == config_id else None)
    with database.get_db_conn() as conn:
        conn.execute('UPDATE summaries SET symbol=? WHERE config_id=? AND symbol IS NULL', (symbol, config_id))
        conn.commit()
    context = projections.prepare_review(config_id)
    def narrative(text):
        return text if validate_strategy_text(text) else strategy_material(text)
    value = dict(summary='策略测试素材', memory=dict(strategy_text='策略测试素材'), projections=[
        {**{k: source[k] for k in ('source_kind', 'source_id', 'source_hash')},
         'strategy_text': narrative(source['text'])} for source in context['sources']])
    projections.persist_projections(config_id, projections.validate_review(value, context))
