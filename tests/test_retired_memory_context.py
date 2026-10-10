from unittest.mock import Mock

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from backend import database
from backend.agent import agent_graph, decision_context, chat_graph
from backend.agent.agent_models import AgentState
from backend.app.core import scheduler
from backend.database_schema import initialize_schema


MEMORY = decision_context.DecisionMemory('dynamic-memory', 'recent-decisions', 'human-rule')


@pytest.mark.parametrize('template', [
    'Trade {symbol}\n{history_text}\n## Account\n{positions_text}',
    'Trade {symbol}\n## Daily Memory\n(暂无历史记录)\n## Account\n{positions_text}',
    'Trade {symbol}\n## daily memory\n{history_text}\n### Old subsection\nstale\n## Account\n{positions_text}',
    'Trade {symbol}\n## 每日记忆\nold review\n## Account\n{positions_text}',
    'Trade {symbol}\n## 自定义历史标题\n{history_text}\n## Account\n{positions_text}',
])
def test_retired_sections_removed_without_losing_active_context(template):
    prompt = decision_context.render_decision_prompt(template, MEMORY, symbol='ETH/USDT', positions_text='live-position')
    assert 'ETH/USDT' in prompt and 'live-position' in prompt
    assert all(prompt.count(text) == 1 for text in ('dynamic-memory', 'recent-decisions'))
    assert 'human-rule' not in prompt
    for retired in ('Daily Memory', 'daily memory', '暂无历史记录', '{history_text}', 'Old subsection', 'stale', '每日记忆', 'old review', '自定义历史标题'):
        assert retired not in prompt


def test_explicit_memory_fields_not_repeated_and_escaped_fields_are_literals():
    template = 'literal {{short_memory_text}}\n{recent_summaries_text}\n{trading_rules_text}'
    prompt = decision_context.render_decision_prompt(template, MEMORY)
    assert '{short_memory_text}' in prompt
    assert all(prompt.count(text) == 1 for text in ('dynamic-memory', 'recent-decisions'))
    assert 'human-rule' not in prompt


def test_failed_memory_source_does_not_erase_other_sources(monkeypatch):
    monkeypatch.setattr(decision_context, 'format_short_memory_for_llm', Mock(side_effect=RuntimeError('unavailable')))
    monkeypatch.setattr(decision_context, 'format_recent_decisions', lambda _: 'retained recent evidence')
    loaded = decision_context.load_decision_memory('cfg')
    assert '读取失败' in loaded.short_memory_text
    assert loaded.recent_summaries_text == 'retained recent evidence'
    assert loaded.trading_rules_text == ''


@pytest.mark.parametrize('role,expected', [('system', SystemMessage), ('user', HumanMessage)])
def test_actual_trade_and_bound_chat_prompt_remove_daily_sections(tmp_path, monkeypatch, role, expected):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'retirement.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    market = Mock()
    market.get_market_analysis.side_effect = RuntimeError('offline isolated test')
    monkeypatch.setattr(agent_graph, 'MarketTool', lambda **_: market)
    monkeypatch.setattr(agent_graph, 'resolve_prompt_template', lambda *_: 'Trade {symbol}\n## Daily Memory\n{history_text}\n## Plan\nkeep-this')
    monkeypatch.setattr(agent_graph, 'load_decision_memory', lambda _: MEMORY)
    monkeypatch.setattr(agent_graph.global_config, 'get_leverage', lambda _: 1)
    state = AgentState(symbol='ETH/USDT', messages=[], market_context={}, account_context={})
    # Bound task chats use this same context builder.
    assert chat_graph.scheduler_start_node is agent_graph.start_node
    result = agent_graph.start_node(state, {'configurable': {'config_id': 'cfg', 'agent_config': {'mode': 'STRATEGY', 'system_prompt_role': role}}})
    prompt = result.messages[0]
    assert isinstance(prompt, expected) and prompt.additional_kwargs['is_instruction']
    assert 'keep-this' in prompt.content and 'Daily Memory' not in prompt.content
    assert 'history_context' not in result.model_dump()
    assert all(prompt.content.count(text) == 1 for text in ('dynamic-memory', 'recent-decisions'))
    assert 'human-rule' not in prompt.content


def test_retired_daily_and_batch_scheduler_entry_points_are_removed():
    assert not hasattr(scheduler, 'run_daily_summary_job')
    assert not hasattr(scheduler, 'run_short_memory_job')
    assert not hasattr(agent_graph, 'generate_manual_daily_summary')


def test_old_daily_generation_http_endpoint_is_retired_without_touching_archive(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api.history import router
    from backend.app.core.deps import get_current_user
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'daily-archive.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    database.save_daily_summary('2026-10-01', 'ETH/USDT', 'cfg', 'preserved archive', 1)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {'username': 'test'}
    with TestClient(app) as client:
        response = client.post('/api/history/daily-summaries/generate', json={'config_id': 'cfg', 'date': '2026-10-01'})
        assert response.status_code == 410
    assert database.list_daily_summaries(config_id='cfg')[0]['summary'] == 'preserved archive'
