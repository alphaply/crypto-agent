"""One model-visible MCP call carries an ordered cancel-first paper request."""
import asyncio
import json

from mcp.server.fastmcp import FastMCP

from backend.mcp import paper, paper_orders, service, store
from backend.mcp.tools import register_tools
from backend.mcp.paper_tools import register_paper_tools
from test_mcp_paper import ledger


def call(server, name, args):
    blocks = asyncio.run(server.call_tool(name, args))
    if hasattr(blocks, 'structuredContent'):
        assert blocks.content[0].type == 'text' and not blocks.content[0].text.startswith('{')
        return blocks.structuredContent
    return json.loads(blocks[0].text)


def test_sdk_directory_bootstrap_cancel_first_and_read_only_recovery(ledger, monkeypatch):
    caller, *_ = ledger
    monkeypatch.setattr(service, 'principal', lambda: caller)
    server = FastMCP('Complete offline model call chain')
    register_tools(server)
    register_paper_tools(server)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert len(names) == 10 and 'paper_trade' not in names and 'adopt_perpetual_position' not in names
    assert server._tool_manager.get_tool('get_operation') is None
    context = call(server, 'get_trading_context', {})
    assert context['contract_version'] == 3
    with store.connection() as conn:
        assert conn.execute('SELECT count(*) FROM paper_accounts').fetchone()[0] == 0
    call(server, 'configure_paper_account', {'user_id': '123456', 'operation_id': 'sdk-create-0001', 'action': 'create'})
    queued = paper_orders.place(caller, '123456', 'spot', 'BTC/USDT', 'buy', 'limit', 'sdk-old-queue', amount=9, price=99)
    request = {'contract_version': 3, 'user_id': '123456', 'operation_id': 'sdk-cancel-first-0001',
               'actions': [{'action': 'cancel', 'order_id': queued['order']['order_id'], 'reason': 'release unused reservation'},
                           {'action': 'open', 'market_type': 'spot', 'symbol': 'BTC/USDT', 'side': 'LONG',
                            'order_type': 'market', 'amount': 9, 'reason': 'replacement entry'},
                           {'action': 'protect', 'market_type': 'spot', 'symbol': 'BTC/USDT', 'side': 'LONG',
                            'amount': 9, 'stop_loss': 90, 'reason': 'protect current holding'}]}
    first = call(server, 'execute_trade_actions', request)
    assert [row['index'] for row in first['results']] == [0, 1, 2]
    assert [row['status'] for row in first['results']] == ['completed', 'completed', 'submitted']
    assert all('account' not in row for row in first['results'])
    balance = paper.get_account(caller, '123456')['cash']
    fills_before = len(paper.history(caller, '123456'))
    assert call(server, 'execute_trade_actions', request) == first
    from backend.mcp import paper_batch
    from backend.utils.trade_summary import compact_trade_receipt
    detailed = paper_batch.operation(caller,'123456',request['operation_id'])
    default = compact_trade_receipt(detailed)
    child_original=paper.operation(caller,'123456',detailed['results'][1]['operation_id'])
    assert 'account' in child_original and 'account' not in default['results'][1]
    view = call(server, 'get_account_state', {'user_id': '123456', 'include': ['positions']})
    assert 'balances' not in view and 'orders' not in view
    assert paper.get_account(caller, '123456')['cash'] == balance
    assert len(paper.history(caller, '123456')) == fills_before
