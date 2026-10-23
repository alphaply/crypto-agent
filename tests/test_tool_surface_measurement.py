"""Reproducible UTF-8 sizes of current schemas and actual offline paper receipts."""
import asyncio
import json

from langchain_core.utils.function_calling import convert_to_openai_tool
from mcp.server.fastmcp import FastMCP

from backend.agent.tool_registry import get_trade_tools_for_mode
from backend.mcp import paper, paper_batch, service, settings, store
from backend.mcp.tools import register_tools
from backend.mcp.paper_tools import register_paper_tools
from test_mcp_paper import create, ledger


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


def test_schema_metrics_are_actual_advertised_contracts(ledger, monkeypatch):
    caller, *_ = ledger
    server = FastMCP('Offline schema measurement')
    register_tools(server)
    register_paper_tools(server)
    monkeypatch.setattr(service, 'principal', lambda: caller)
    result = {}
    for mode, exit_mode in [('REAL', 'independent_exits'), ('REAL', 'attached_optional'), ('STRATEGY', 'attached_required'), ('SPOT_DCA', 'attached_optional')]:
        cfg = {'mode': mode, 'exit_mode': exit_mode}
        public = [convert_to_openai_tool(tool) for tool in get_trade_tools_for_mode(mode, cfg)]
        compatibility = [convert_to_openai_tool(tool) for tool in get_trade_tools_for_mode(mode, cfg, include_legacy=True)]
        assert [tool['function']['name'] for tool in public] == ['execute_trade_actions']
        assert not {'get_trade_operation', 'get_operation', 'get_paper_operation'} & {
            tool['function']['name'] for tool in compatibility}
        result[f'{mode}/{exit_mode}'] = {'default_tools': len(public), 'default_bytes': size(public),
                                       'compatibility_tools': len(compatibility), 'compatibility_bytes': size(compatibility)}
    public = [tool.model_dump(exclude_none=True) for tool in asyncio.run(server.list_tools())]
    core_names = {'get_trading_context', 'search_symbols', 'get_market', 'get_news',
                  'get_account_state', 'get_trade_records', 'execute_trade_actions'}
    assert {tool['name'] for tool in public} == core_names | {
        'paper_bank', 'configure_paper_account', 'get_paper_leaderboard', 'paper_transfer', 'adjust_paper_position'}
    result['MCP/paper'] = {'default_tools': len(public), 'default_bytes': size(public)}
    live = {'id': 'live:test', 'key_type': 'live', 'scopes': ['read', 'trade', 'cancel'], 'profile_ids': ['fixture']}
    monkeypatch.setattr(service, 'principal', lambda: live)
    monkeypatch.setattr(service, 'list_profiles', lambda who: [{'profile_id': 'fixture'}])
    monkeypatch.setattr(settings, 'get_runtime_profile', lambda identity: {'mode': 'REAL', 'exit_mode': 'independent_exits'})
    public = [tool.model_dump(exclude_none=True) for tool in asyncio.run(server.list_tools())]
    assert {tool['name'] for tool in public} == core_names
    result['MCP/live'] = {'default_tools': len(public), 'default_bytes': size(public)}
    print('SCHEMA_METRICS=' + json.dumps(result, ensure_ascii=False, separators=(',', ':')))


def test_actual_twenty_step_receipt_has_one_optional_summary_and_full_audit(ledger, monkeypatch):
    caller, *_ = ledger
    create(caller)
    actions = [{'action': 'open', 'market_type': 'spot', 'symbol': 'BTC/USDT', 'side': 'LONG',
                'order_type': 'market', 'amount': .01, 'reason': 'offline measurement'}] * 20
    server = FastMCP('Offline paper receipt measurement')
    register_tools(server)
    register_paper_tools(server)
    monkeypatch.setattr(service, 'principal', lambda: caller)
    response = asyncio.run(server.call_tool('execute_trade_actions', {'user_id': '123456', 'operation_id': 'measure-20-steps',
                           'contract_version': 3, 'actions': actions, 'include': ['account']}))
    assert not response.isError
    result = response.structuredContent
    assert size(result) <= 32768
    assert len(response.content[0].text.encode('utf-8')) <= 32768
    assert len(result['results']) == 20 and result['status'] == 'completed'
    assert 'account_summary' in result
    assert all('account' not in step and 'account_summary' not in step for step in result['results'])
    with store.connection() as conn:
        full = [json.loads(row[0]) for row in conn.execute("SELECT result FROM paper_operations WHERE operation_id LIKE 'paper-child:%' ORDER BY operation_id")]
    assert len(full) == 20 and all('account' in receipt for receipt in full)
    assert paper.get_account(caller, '123456')['spot'][0]['amount'] > .19
    print('RECEIPT_METRICS=' + json.dumps({'steps': 20, 'full_child_receipts_bytes': size(full),
          'compact_parent_with_account_bytes': size(result), 'full_child_accounts': 20, 'wire_account_summaries': 1}, separators=(',', ':')))
