import sqlite3
from contextlib import contextmanager
from unittest.mock import Mock

from backend.app.services.dashboard_service import _scheduler_timestamp_iso
from backend.database_schema import initialize_schema
from backend.database_summary import SummaryStore


def test_reasoning_tokens_migrate_and_persist_with_summaries():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            timeframe TEXT,
            content TEXT,
            strategy_logic TEXT
        )
        """
    )
    initialize_schema(conn)

    @contextmanager
    def temp_conn():
        yield conn

    store = SummaryStore(temp_conn, lambda: "2026-08-13 12:00:00", Mock())
    store.save_summary(
        "BTC/USDT",
        "model-a",
        "answer",
        "strategy",
        config_id="cfg-a",
        reasoning_content="risk check",
        reasoning_tokens=456,
    )

    row = conn.execute(
        "SELECT reasoning_content, reasoning_tokens FROM summaries WHERE config_id = ?",
        ("cfg-a",),
    ).fetchone()
    conn.close()

    assert row == ("risk check", 456)


def test_dashboard_scheduler_timestamp_includes_china_timezone():
    assert _scheduler_timestamp_iso("2026-08-13 12:00:00") == "2026-08-13T12:00:00+08:00"


def test_diagnostics_survive_summary_checkpoint_progress_and_audit(tmp_path, monkeypatch):
    import json
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import StateGraph, MessagesState, START, END
    from backend import database
    from backend.agent import agent_graph
    from backend.agent.call_audit import audited_invoke
    from backend.app.core import scheduler
    from backend.app.services.common import serialize_message
    from backend.utils.decision_record import build_decision_record

    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'diagnostics.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    diagnostic = {'model': 'synthetic-fallback', 'protocol': 'anthropic', 'requested_effort': 'high',
                  'sent_parameters': {'output_config': {'effort': 'high'}}, 'evidence_state': 'usage_only',
                  'reported_reasoning_tokens': 145, 'text_available': False}
    response = AIMessage(content='answer', additional_kwargs={'reasoning_diagnostics': diagnostic},
                         usage_metadata={'input_tokens': 10, 'output_tokens': 200, 'total_tokens': 210,
                                         'output_token_details': {'reasoning': 145}})
    result = audited_invoke(lambda: response, config_id='cfg', purpose='decision', model='synthetic-fallback', messages=[])
    assert serialize_message(result)['reasoning_diagnostics'] == diagnostic
    record = build_decision_record([result], 'completed')
    database.save_summary('ETH/USDT', 'synthetic-fallback', 'answer', 'summary', config_id='cfg',
                          reasoning_tokens=145, reasoning_diagnostics=[diagnostic], decision_json=json.dumps(record))
    scheduled = '2026-10-12 01:00:00'
    scheduler._insert_scheduler_run('cfg', scheduler.AGENT_JOB_TYPE, scheduled)
    agent_graph._emit_task_progress({'progress_callback': lambda event: scheduler._mark_scheduler_progress('cfg', scheduled, event)},
                                   phase='thinking', message='done', decision_messages=[result], reasoning_tokens=145)
    with database.get_db_conn() as conn:
        assert json.loads(conn.execute('SELECT reasoning_diagnostics FROM summaries').fetchone()[0]) == [diagnostic]
        assert json.loads(conn.execute('SELECT reasoning_diagnostics FROM scheduler_runs').fetchone()[0]) == [diagnostic]
        assert json.loads(conn.execute('SELECT details_json FROM agent_runs').fetchone()[0])['reasoning_diagnostics'] == diagnostic
    graph = StateGraph(MessagesState)
    graph.add_node('answer', lambda state: {'messages': [result]})
    graph.add_edge(START, 'answer')
    graph.add_edge('answer', END)
    settings = {'configurable': {'thread_id': 'offline-reasoning'}}
    path = str(tmp_path / 'checkpoint.sqlite')
    with SqliteSaver.from_conn_string(path) as saver:
        graph.compile(checkpointer=saver).invoke({'messages': []}, settings)
    with SqliteSaver.from_conn_string(path) as saver:
        restored = graph.compile(checkpointer=saver).get_state(settings).values['messages'][-1]
    assert serialize_message(restored)['reasoning_tokens'] == 145
    assert serialize_message(restored)['reasoning_diagnostics'] == diagnostic
