from types import SimpleNamespace

import pytest

from backend.agent import agent_tools, tool_registry
from backend.mcp import service, store, guard
from backend.utils.independent_exits import IndependentExits
from test_independent_exits import Exchange, entry
from test_mcp_tool_contracts import contracts, enable_independent_profile, real_run_trade_tool


def test_mcp_read_and_replay_refresh_same_internal_operation_without_writes(contracts, monkeypatch):
    caller, _, _, _ = contracts
    enable_independent_profile()
    exchange = Exchange()
    mt = SimpleNamespace(exchange=exchange, config_id='mcp:swap')
    independent = IndependentExits(mt)
    monkeypatch.setattr(guard, 'assert_entry_allowed', lambda *a, **k: None)
    monkeypatch.setattr(guard, 'preflight_tool', lambda *a, **k: None)
    monkeypatch.setattr(service, 'market_tool', lambda _: mt)
    monkeypatch.setattr(agent_tools, '_independent_service', lambda *_: (independent, True))
    monkeypatch.setattr(tool_registry, 'run_trade_tool', real_run_trade_tool)
    opening = independent.open('ETH/USDT', entry(), operation_id='mcp-entry', contract_version=2)
    exchange.orders[opening['id']].update(status='closed', filled=1)
    exchange.quantity = 1
    independent.reconcile_all()
    args = {'side': 'LONG', 'order_type': 'market', 'close_all': True, 'reason': 'exit'}
    exchange.timeout_next = True
    first = service.execute(caller, 'swap', 'ETH/USDT:USDT', 'close', args, 'mcp-close-recover')
    assert first['status'] == 'unknown'
    assert first['result']['recovery']['tool'] == 'get_operation'
    assert first['result']['recovery']['target_operation_id'] == 'mcp-close-recover'
    for name in ['create_order', 'cancel_order', 'edit_order']:
        monkeypatch.setattr(exchange, name, lambda *a, **k: pytest.fail('read/replay wrote financially'))
    reader = {**caller, 'scopes': ['read']}
    current = service.operation(reader, 'swap', 'mcp-close-recover')
    assert current['state'] == 'completed'
    assert current['result']['result']['filled_amount'] == 1
    replay = service.execute(caller, 'swap', 'ETH/USDT:USDT', 'close', args, 'mcp-close-recover')
    assert replay['status'] == 'completed'
    with pytest.raises(ValueError, match='not found'):
        service.operation({**reader, 'id': 'another-principal'}, 'swap', 'mcp-close-recover')
    with pytest.raises(ValueError, match='not found'):
        service.operation(reader, 'spot', 'mcp-close-recover')
    assert len(exchange.calls) == 2
    with store.connection() as conn:
        link = conn.execute('SELECT * FROM mcp_operation_links WHERE operation_id=?', ('mcp-close-recover',)).fetchone()
        assert link['principal'] == caller['id']
        assert link['internal_operation_id'] == 'mcp:' + store.digest(caller['id'] + ':mcp-close-recover')
        assert conn.execute('SELECT count(*) FROM mcp_operation_revisions').fetchone()[0] >= 2
