"""Offline behavior checks for hourly reports, memory, scheduling and run budgets."""
from datetime import datetime
import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage
import pandas as pd
import pytest
import pytz

from backend import database
from backend.database_schema import initialize_schema
from backend.agent import agent_graph
from backend.agent.memory_updates import enqueue_memory_update, process_memory_update
from backend.agent.trade_report import TradeReport, assemble_report
from backend.utils.run_schedule import DcaSchedule, latest_dca_slot, preview_dca_schedule, effective_schedule


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'hourly.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    monkeypatch.setattr(agent_graph, 'format_recent_position_history_for_memory', lambda *_: 'verified fills')


def at(text):
    return pytz.timezone('Asia/Shanghai').localize(datetime.fromisoformat(text))


def draft():
    return {'version': 1, 'market_analysis': '震荡', 'strategy': '等待突破',
            'decision': {'action': 'HOLD', 'rationale': '尚未触发'},
            'forecast': {'next_1h': '突破后确认', 'next_4h': '跌破支撑则失效'},
            'risks': ['流动性'], 'invalidation': '支撑失守', 'next_watchpoints': ['收盘确认'], 'summary': '等待确认'}


def test_report_preserves_actual_unknown_receipt():
    report = assemble_report(json.dumps(draft()), [
        AIMessage(content='', tool_calls=[{'id': 't1', 'name': 'open_position_real', 'args': {}}]),
        ToolMessage(content='{"status":"unknown"}', tool_call_id='t1')], 'original')
    assert report['validation_status'] == 'valid'
    assert report['execution_results'] == [{'tool': 'open_position_real', 'tool_call_id': 't1', 'result': '{"status":"unknown"}'}]
    assert report['raw_analysis'] == 'original'
    with pytest.raises(ValueError):
        TradeReport.model_validate({**draft(), 'forecast': {}})


def test_report_has_one_readonly_repair(isolated, monkeypatch):
    responses = iter([AIMessage(content='not json'), AIMessage(content=json.dumps(draft()))])
    calls = []
    class Model:
        def invoke(self, messages):
            calls.append(list(messages))
            return next(responses)
    monkeypatch.setattr(agent_graph, 'build_chat_model', lambda **_: Model())
    monkeypatch.setattr(agent_graph, 'run_trade_tool', lambda *_: pytest.fail('formatter must not trade'))
    result = agent_graph.summarize_content('source receipt', {'model': 'offline', 'config_id': 'cfg'}, 'report')
    assert TradeReport.model_validate_json(result).summary == '等待确认'
    assert len(calls) == 2 and 'source receipt' in calls[0][0].content


@pytest.mark.parametrize('mode', ['json_schema', 'tool'])
def test_report_native_mode_is_explicit_and_never_executes_submission(isolated, monkeypatch, mode):
    bindings = []
    class Model:
        def bind(self, **kwargs):
            bindings.append(kwargs)
            return self
        def bind_tools(self, tools, **kwargs):
            bindings.append({'tools': tools, **kwargs})
            return self
        def invoke(self, messages):
            return (AIMessage(content='', tool_calls=[{'id': 'report', 'name': 'TradeReport', 'args': draft()}])
                    if mode == 'tool' else AIMessage(content=json.dumps(draft())))
    monkeypatch.setattr(agent_graph, 'build_chat_model', lambda **_: Model())
    monkeypatch.setattr(agent_graph, 'run_trade_tool', lambda *_: pytest.fail('report submission is not a trade tool'))
    result = agent_graph.summarize_content('completed evidence', {'model': 'offline', 'report_output_mode': mode}, 'report')
    assert TradeReport.model_validate_json(result).decision.action == 'HOLD'
    assert bindings[0].get('tool_choice') == 'TradeReport' if mode == 'tool' else bindings[0]['response_format']['json_schema']['strict']


def test_memory_per_run_four_hours_retry_and_idempotence(isolated, monkeypatch):
    with database.get_db_conn() as conn:
        conn.executemany('INSERT INTO summaries(timestamp,symbol,config_id,strategy_logic) VALUES (?,?,?,?)', [
            ('2026-10-08 05:59:59', 'BTC/USDT', 'cfg', 'outside-window'),
            ('2026-10-08 06:00:00', 'BTC/USDT', 'cfg', 'boundary-evidence'),
            ('2026-10-08 10:00:00', 'BTC/USDT', 'cfg', 'current-evidence'),
            ('2026-10-08 10:01:00', 'BTC/USDT', 'cfg', 'future-evidence'),
            ('2026-10-08 08:00:00', 'ETH/USDT', 'other', 'other-config')])
        conn.commit()
    database.save_short_memory('2026-10-08 00:00:00', '2026-10-08 00:00:00', 'BTC/USDT', 'cfg', 'prior-memory', '', 1)
    enqueue_memory_update('cfg', 'run-1', 3)
    seen = []
    def review(source, *args, **kwargs):
        seen.append(source)
        return '' if len(seen) == 1 else 'updated-memory'
    monkeypatch.setattr(agent_graph, '_run_memory_organizer', review)
    monkeypatch.setattr(agent_graph, 'run_agent_for_config', lambda *_: pytest.fail('memory retry must not trade'))
    assert not process_memory_update('cfg', 'run-1', {'symbol': 'BTC/USDT'})
    assert database.get_short_memories('cfg', 1)[0]['market_summary'] == 'prior-memory'
    assert not process_memory_update('cfg', 'run-1', {'symbol': 'BTC/USDT'})
    assert len(seen) == 1
    with database.get_db_conn() as conn:
        conn.execute("UPDATE memory_update_jobs SET next_attempt=0 WHERE config_id='cfg' AND run_id='run-1'")
        conn.commit()
    assert process_memory_update('cfg', 'run-1', {'symbol': 'BTC/USDT'})
    assert process_memory_update('cfg', 'run-1', {'symbol': 'BTC/USDT'})
    assert len(seen) == 2
    assert 'boundary-evidence' in seen[-1] and seen[-1].count('current-evidence') == 1
    assert all(text not in seen[-1] for text in ('outside-window', 'future-evidence', 'other-config'))
    assert 'prior-memory' in seen[-1] and database.get_short_memories('cfg', 1)[0]['market_summary'] == 'updated-memory'
    memory = database.get_short_memories('cfg', 1)[0]
    assert memory['window_start'] == '2026-10-08 06:00:00'
    assert memory['window_end'] == memory['bucket_end'] == '2026-10-08 10:00:00'
    assert memory['source_summary_ids'] == [2, 3]
    assert memory['run_id'] == 'run-1' and memory['version'] == 2
    # Persistence retries retain the same version and do not create another snapshot.
    assert len(database.get_short_memories('cfg', 10)) == 2
    assert '覆盖 2026-10-08 06:00:00 → 2026-10-08 10:00:00' in agent_graph.format_short_memory_for_llm('cfg', include_metadata=True)


def test_memory_versions_and_window_metadata_survive_same_second_updates(isolated):
    for suffix in (1, 2):
        key = f'2026-10-08 10:00:00.00000{suffix}'
        database.save_short_memory(key, '2026-10-08 10:00:00', 'BTC/USDT', 'same-second',
                                   f'memory-{suffix}', '', 1, window_start='2026-10-08 06:00:00',
                                   window_end='2026-10-08 10:00:00', source_summary_ids=[suffix], run_id=f'run-{suffix}')
    database.save_short_memory(key, '2026-10-08 10:00:00', 'BTC/USDT', 'same-second',
                               'memory-2', '', 1, window_start='2026-10-08 06:00:00',
                               window_end='2026-10-08 10:00:00', source_summary_ids=[2], run_id='run-2')
    memories = database.list_short_memories(config_id='same-second')
    assert [item['version'] for item in memories] == [2, 1]
    assert [item['source_summary_ids'] for item in memories] == [[2], [1]]
    assert database.get_short_memory('same-second', key)['window_start'] == '2026-10-08 06:00:00'


def test_manual_memory_sources_keep_actual_receipts(isolated):
    database.save_summary('BTC/USDT', 'agent', 'analysis', 'proposed buy', 'cfg',
                          report_json=json.dumps({'execution_results': [{'result': 'exchange rejected order'}]}))
    with database.get_db_conn() as conn:
        conn.execute("UPDATE summaries SET timestamp='2026-10-08 10:00:00' WHERE config_id='cfg'")
        conn.commit()
    rows = database.get_summary_logic_between('cfg', '2026-10-08 06:00:00', '2026-10-08 10:00:01')
    assert rows[0]['id'] == 1
    assert 'exchange rejected order' in rows[0]['strategy_logic']


def test_background_services_are_isolated_from_memory_failure(monkeypatch):
    import importlib
    from backend.app.core import scheduler
    calls = []
    def service(name):
        def tick():
            calls.append(name)
            if name == 'tick_memory_updates':
                raise RuntimeError('memory store busy')
        return tick
    monkeypatch.setattr(importlib, 'import_module', lambda _: SimpleNamespace(**{
        name: service(name) for name in ('tick_news_pipeline', 'tick_price_sync', 'tick_memory_updates')}))
    scheduler.tick_shared_services()
    assert calls == ['tick_news_pipeline', 'tick_price_sync', 'tick_memory_updates']


def test_disabled_scheduler_keeps_shared_services_and_observes_reload(monkeypatch):
    from backend.app.core import scheduler
    from unittest.mock import Mock
    class Clock:
        @staticmethod
        def now():
            return datetime(2026, 10, 8, 9, 5)
    def stop():
        raise KeyboardInterrupt()
    waits = iter([None, None])
    def wait_minute():
        if next(waits, 'stop') == 'stop':
            stop()
    cfg = SimpleNamespace(enable_scheduler=False)
    reload_count = 0
    def reload():
        nonlocal reload_count
        reload_count += 1
        if reload_count == 3:
            cfg.enable_scheduler = True
    cfg.reload_config = reload
    shared, job = Mock(), Mock()
    monkeypatch.setattr(scheduler, 'global_config', cfg)
    monkeypatch.setattr(scheduler, 'datetime', Clock)
    monkeypatch.setattr(scheduler, 'wait_until_next_minute', wait_minute)
    monkeypatch.setattr(scheduler, 'tick_shared_services', shared)
    monkeypatch.setattr(scheduler, 'job', job)
    monkeypatch.setattr(scheduler, 'init_db', lambda: None)
    monkeypatch.setattr(scheduler, '_start_protection_monitor', lambda: None)
    with pytest.raises(KeyboardInterrupt):
        scheduler.run_scheduler_forever()
    assert shared.call_count == 2 and job.call_count == 1


def test_daily_multi_slots_weekly_and_latest_catchup():
    cfg = {'dca_schedule': {'frequency': 'daily', 'times': ['21:00', '09:00'], 'timezone': 'Asia/Shanghai'}}
    assert preview_dca_schedule(cfg, at('2026-10-08T08:00')) == [
        '2026-10-08T09:00:00+08:00', '2026-10-08T21:00:00+08:00', '2026-10-09T09:00:00+08:00']
    assert latest_dca_slot(cfg, at('2026-10-08T22:00')).hour == 21
    cfg['dca_schedule'].update(frequency='weekly', days=[0], times=['09:00'])
    assert preview_dca_schedule(cfg, at('2026-10-08T08:00')) == ['2026-10-12T09:00:00+08:00',
                                                             '2026-10-19T09:00:00+08:00',
                                                             '2026-10-26T09:00:00+08:00']
    with pytest.raises(ValueError):
        DcaSchedule(times=['09:00', '09:00'])
    assert effective_schedule({'mode': 'REAL', 'market_profile': 'hourly'}, at('2026-10-08T09:00'))['interval'] == 60
    assert effective_schedule({'mode': 'REAL', 'run_interval': 15}, at('2026-10-08T09:00'))['interval'] == 15


def test_slot_dispatch_without_order_is_persistently_consumed(isolated):
    from backend.app.core import scheduler
    cfg = {'config_id': 'spot', 'mode': 'SPOT_DCA', 'dca_schedule': {'times': ['09:00', '21:00']}}
    assert scheduler.is_time_to_run(cfg, at('2026-10-08T09:01'))
    scheduler._insert_scheduler_run('spot', 'agent', '2026-10-08 09:00:00', status='FINISHED')
    assert not scheduler.is_time_to_run(cfg, at('2026-10-08T20:59'))
    assert scheduler.is_time_to_run(cfg, at('2026-10-08T21:00'))


@pytest.mark.parametrize('tf,freq,visible', [('1h','1h',24), ('4h','4h',12), ('1d','1d',7)])
def test_hourly_market_profile_preserves_warmup(tf, freq, visible):
    from backend.utils.market_data import MarketTool
    now = pd.Timestamp.now(tz='UTC').floor(freq) - pd.Timedelta(freq)
    dates = pd.date_range(end=now, periods=600, freq=freq)
    bars = [[int(t.timestamp()*1000), 100+i, 102+i, 99+i, 101+i, 1000] for i,t in enumerate(dates)]
    requested = []
    def fetch(*args, **kwargs):
        requested.append(kwargs['limit'])
        return bars
    tool = object.__new__(MarketTool)
    tool.exchange = SimpleNamespace(fetch_ohlcv=fetch)
    result = tool.process_timeframe('BTC/USDT', tf, profile='hourly')
    assert requested == [600]
    assert result['data_quality']['bars'] == 600
    assert len(result['recent_times']) == visible
    assert result['data_quality']['display_bars'] == visible
