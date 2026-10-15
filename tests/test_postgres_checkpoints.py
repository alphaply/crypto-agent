from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from backend.storage.checkpoints import close_checkpoints, get_checkpointer


class State(TypedDict):
    value: str


def graph():
    workflow = StateGraph(State)
    workflow.add_node('approval', lambda state: {'value': interrupt(state['value'])})
    workflow.add_edge(START, 'approval')
    workflow.add_edge('approval', END)
    return workflow.compile(checkpointer=get_checkpointer())


def test_interrupt_stream_restart_resume_and_targeted_delete(checkpoint_url, tmp_path):
    one = {'configurable': {'thread_id': 'one'}}
    two = {'configurable': {'thread_id': 'two'}}
    events = list(graph().stream({'value': '中文'}, one, stream_mode='updates'))
    assert events[-1]['__interrupt__'][0].value == '中文'
    graph().invoke({'value': 'second'}, two)
    close_checkpoints()
    assert graph().get_state(one).values == {'value': '中文'}
    assert graph().invoke(Command(resume='approved'), one) == {'value': 'approved'}
    from backend.agent.chat_graph import delete_chat_threads
    assert delete_chat_threads(['one']) > 0
    assert not graph().get_state(one).values
    assert graph().get_state(two).values == {'value': 'second'}
    assert not list(tmp_path.glob('*.db'))


def test_checkpoint_and_application_data_survive_dump_restore(checkpoint_url, pg_cli):
    import os
    from urllib.parse import urlsplit, urlunsplit
    from uuid import uuid4
    import psycopg
    from psycopg import sql
    from backend.storage import get_connection

    graph().invoke({'value': 'saved'}, {'configurable': {'thread_id': 'restore'}})
    with get_connection() as conn:
        conn.execute("INSERT INTO app_settings(key,value_json,updated_at) VALUES('backup-proof','{\"中文\":true}','now')")
        conn.commit()
    blob = pg_cli('pg_dump', ['--format=custom', '--no-owner', '--no-privileges'], checkpoint_url)
    assert blob.startswith(b'PGDMP')
    name = 'restore_test_' + uuid4().hex
    admin_url = os.environ['TEST_POSTGRES_URL']
    target = urlunsplit(urlsplit(admin_url)._replace(path='/' + name, query=''))
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        pg_cli('pg_restore', ['--exit-on-error', '--no-owner', '--no-privileges', '--dbname', name], target, data=blob)
        with psycopg.connect(target) as conn:
            assert conn.execute("SELECT value_json FROM app_settings WHERE key='backup-proof'").fetchone()[0] == '{"中文":true}'
            assert conn.execute("SELECT count(*) FROM checkpoints.checkpoints WHERE thread_id='restore'").fetchone()[0] > 0
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
