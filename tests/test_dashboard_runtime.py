from types import SimpleNamespace

from backend.app.services import dashboard_service


def test_new_waiting_runs_do_not_hide_current_execution(pg, monkeypatch):
    cfg = {'config_id': 'queued-task', 'mode': 'STRATEGY', 'symbol': 'BTC/USDT',
           'enabled': True, 'run_interval': 5}
    monkeypatch.setattr(dashboard_service, 'global_config', SimpleNamespace(
        get_all_symbol_configs=lambda: [cfg], get_leverage=lambda _: 1))
    monkeypatch.setattr(dashboard_service, 'get_paginated_orders', lambda *a, **kw: ([], 0))
    monkeypatch.setattr(dashboard_service, 'get_scheduler_status', lambda: True)
    with pg() as conn:
        active_id = conn.execute("""INSERT INTO scheduler_runs
            (config_id,job_type,scheduled_at,status,started_at,reasoning_content,created_at,updated_at)
            VALUES('queued-task','agent','2026-10-18 12:00:00','RUNNING',
                   '2026-10-18 12:00:01','current reasoning',
                   '2026-10-18 12:00:00','2026-10-18 12:00:01') RETURNING id""").fetchone()[0]
        for minute in (5, 10):
            timestamp = f'2026-10-18 12:{minute:02d}:00'
            waiting_id = conn.execute("""INSERT INTO scheduler_runs
                (config_id,job_type,scheduled_at,status,created_at,updated_at)
                VALUES('queued-task','agent',%s,'QUEUED',%s,%s)
                RETURNING id""", (timestamp, timestamp, timestamp)).fetchone()[0]
            conn.execute("""INSERT INTO agent_run_queue
                (config_id,trigger_key,trigger_type,run_id,scheduled_at,scope_hash,scheduler_run_id)
                VALUES('queued-task',%s,'scheduled',%s,%s,'test-scope',%s)""",
                         (str(minute), str(minute), timestamp, waiting_id))
        conn.commit()
    result = dashboard_service.get_dashboard_data('BTC/USDT')[0]
    assert result['execution']['id'] == active_id
    assert result['execution']['reasoning_content'] == 'current reasoning'
    assert result['runtime']['running'] == result['execution']
    assert result['runtime']['queued_count'] == 2
    assert result['runtime']['next_queued']['scheduled_at'] == '2026-10-18 12:05:00'
    assert result['runtime']['last_run'] is None
