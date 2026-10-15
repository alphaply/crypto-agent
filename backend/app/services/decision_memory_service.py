from backend import storage
"""Published text and memory-job status; retired projections are audit-only."""
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
    from backend.utils.trade_operations import _same_symbol
    cfg = _config(config_id)
    result = dict(config_id=config_id, enabled=projections.enabled(cfg), published={}, pending=0,
                  failed=0, not_generated=0, errors=[], read_errors=[], historical_errors=[])
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
                if p['error']:
                    result['historical_errors'].append(dict(source_kind=kind, source_id=source['source_id'],
                                                 as_of=source['timestamp'], status=p['status'],
                                                 attempts=p['attempts'], error=p['error']))
        except storage.Error:
            raise
        except Exception:
            projections.logger.exception('Unable to read projection status for %s/%s', config_id, kind)
            result['read_errors'].append(kind)
    try:
        with database.get_db_conn() as conn:
            tables = storage.table_names(conn)
            jobs = conn.execute('SELECT j.*,s.timestamp,s.symbol FROM memory_update_jobs j JOIN summaries s '
                                'ON s.id=j.summary_id AND s.config_id=j.config_id WHERE j.config_id=%s '
                                "AND j.status IN ('pending','failed','partial') ORDER BY j.summary_id DESC",
                                (config_id,)).fetchall() if 'memory_update_jobs' in tables else []
        for job in jobs:
            try:
                witness = json.loads(job['review_source'] or '{}')
                if not isinstance(witness, dict):
                    raise ValueError('Invalid memory source witness')
            except (ValueError, TypeError):
                result['read_errors'].append(f"memory_update:{job['summary_id']}:review_source")
                continue
            if (not _same_symbol(job['symbol'], cfg.get('symbol'))
                    or (witness and witness.get('account_scope') != scope)):
                continue
            result['pending' if job['status'] == 'pending' else 'failed'] += 1
            if job['error']:
                result['errors'].append(dict(source_kind='memory_update', source_id=str(job['summary_id']),
                                             as_of=job['timestamp'], status=job['status'],
                                             attempts=job['attempts'], error=job['error']))
    except storage.Error:
        raise
    except Exception:
        projections.logger.exception('Unable to read memory jobs for %s', config_id)
        result['read_errors'].append('memory_update_jobs')
    return result


def retry_failed_projections(config_id: str) -> dict:
    """Compatibility endpoint: retry failed memory jobs, never retired projections."""
    cfg = _config(config_id)
    from backend.agent.memory_updates import _initialize
    from backend.agent.memory_contract_recovery import job_source
    from backend.summary_memory_source import valid_compressed_summary
    scope = trade_evidence.configured_account_scope(config_id)
    with database.get_db_conn() as conn:
        _initialize(conn)
        jobs = conn.execute('SELECT j.*,s.strategy_logic FROM memory_update_jobs j JOIN summaries s '
                            'ON s.id=j.summary_id AND s.config_id=j.config_id '
                            "WHERE j.config_id=%s AND j.status='failed'", (config_id,)).fetchall()
        requeued = 0
        for job in jobs:
            try:
                captured = json.loads(job['review_source'] or '{}')
                source = job_source(conn, config_id, job['summary_id'], scope, cfg.get('symbol', ''))
                if (not source or (captured and captured != source)
                        or not valid_compressed_summary(job['strategy_logic'])):
                    continue
            except (ValueError, TypeError):
                continue
            requeued += conn.execute("UPDATE memory_update_jobs SET status='pending',attempts=0,next_attempt=0,error='' "
                                     "WHERE config_id=%s AND run_id=%s AND status='failed'",
                                     (config_id, job['run_id'])).rowcount
        conn.commit()
    return {**projection_status(config_id), 'requeued': requeued}
