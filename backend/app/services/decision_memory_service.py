"""Administrative projection status/retry without invoking a model or trading tool."""
import json

from backend import database
from backend.agent import context_projections as projections
from backend.config import config
from backend.utils import trade_evidence


def _config(config_id: str) -> dict:
    cfg = config.get_config_by_id(config_id)
    if not cfg:
        raise FileNotFoundError('Config not found')
    return cfg


def projection_status(config_id: str) -> dict:
    cfg = _config(config_id)
    result = dict(config_id=config_id, enabled=projections.enabled(cfg), published={}, pending=0,
                  failed=0, not_generated=0, errors=[], read_errors=[])
    if not result['enabled']:
        return result
    scope = trade_evidence.configured_account_scope(config_id)
    for kind, limit in [('short_memory', 1), ('strategy_summary', 3)]:
        try:
            sources = projections.stored_sources(config_id, scope, kind, diagnostics=result['read_errors'])
            ready = [s for s in sources if s['projection']['status'] == 'ready'][:limit]
            result['published'][kind] = [dict(source_id=s['source_id'], as_of=s['timestamp']) for s in ready]
            for source in sources:
                p = source['projection']
                try:
                    refs = json.loads(p['execution_refs'])
                    if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                        raise ValueError('Invalid execution references')
                except (ValueError, TypeError):
                    result['read_errors'].append(f"{kind}:{source['source_id']}:execution_refs")
                if p['status'] in {'pending', 'failed'}:
                    result[p['status']] += 1
                if p['error']:
                    result['errors'].append(dict(source_kind=kind, source_id=source['source_id'],
                                                 as_of=source['timestamp'], status=p['status'],
                                                 attempts=p['attempts'], error=p['error']))
            known = {(s['source_id'], s['source_hash']) for s in sources}
            result['not_generated'] += sum((s['source_id'], s['source_hash']) not in known
                                          for s in projections.current_sources(config_id, kind))
        except Exception:
            projections.logger.exception('Unable to read projection status for %s/%s', config_id, kind)
            result['read_errors'].append(kind)
    return result


def retry_failed_projections(config_id: str) -> dict:
    cfg = _config(config_id)
    if not projections.enabled(cfg):
        raise ValueError('Strategy projections only apply to independent exits')
    scope = trade_evidence.configured_account_scope(config_id)
    requeued = 0
    with database.get_db_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute("SELECT * FROM decision_context_projections WHERE config_id=? AND account_scope=? "
                            "AND projection_version=? AND status='failed'", (config_id, scope, projections.VERSION)).fetchall()
        for row in rows:
            p = dict(row)
            if projections._original(conn, config_id, p):
                requeued += conn.execute("UPDATE decision_context_projections SET status='pending',attempts=0,next_attempt=0,error='' "
                                         'WHERE ' + projections.KEY_WHERE + " AND status='failed'",
                                         projections.key(config_id, scope, p)).rowcount
        conn.commit()
    return {**projection_status(config_id), 'requeued': requeued}
