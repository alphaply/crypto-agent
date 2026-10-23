"""The same position identity and financial result reach MCP and admin clients."""
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.core.security import create_access_token
from backend.mcp import canonical_tools, management, paper, service
from test_mcp_canonical_catalog import server_for
from test_mcp_paper import create, ledger


def open_position(caller):
    create(caller)
    receipt = paper.trade(caller, '123456', 'swap', 'BTC/USDT:USDT',
                          'open_long', 2, 'api-position-open', leverage=5)
    return receipt['account']['positions'][0]['resource_id']


def test_sdk_adjustments_keep_identity_and_report_money_without_a_trade(ledger, monkeypatch):
    caller, _, quotes = ledger
    position_id = open_position(caller)
    server = server_for(caller, monkeypatch)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == set(canonical_tools.CORE_NAMES + canonical_tools.PAPER_NAMES)
    assert 'adjust_paper_position' in names
    from backend.mcp.server import tool_catalog
    assert next(tool for tool in tool_catalog() if tool['name'] == 'adjust_paper_position')['scope'] == 'trade'
    args = {'user_id': '123456', 'position_id': position_id,
            'operation_id': 'sdk-position-add-margin', 'action': 'add_margin', 'amount': 20}
    response = asyncio.run(server.call_tool('adjust_paper_position', args))
    assert not response.isError
    receipt = response.structuredContent
    assert receipt['status'] == 'completed'
    assert receipt['resource_id'] == position_id
    assert receipt['margin_before'] == 40 and receipt['margin_after'] == 60
    assert receipt['cash_change'] == -20 and receipt['currency'] == 'USDT'
    assert 'account' not in receipt and 'filled_amount' not in receipt
    assert 'USDT' in response.content[0].text and '保证金' in response.content[0].text
    count = len(quotes)
    repeated = asyncio.run(server.call_tool('adjust_paper_position', args))
    assert repeated.structuredContent == receipt and len(quotes) == count
    args = {**args, 'operation_id': 'sdk-position-leverage', 'action': 'set_leverage', 'leverage': 10}
    args.pop('amount')
    adjusted = asyncio.run(server.call_tool('adjust_paper_position', args))
    assert not adjusted.isError
    assert adjusted.structuredContent['margin_after'] == 40
    assert adjusted.structuredContent['added_margin'] == 20
    account = paper.get_account(caller, '123456')
    assert account['cash'] == 959.8 and account['fees'] == .2
    assert account['positions'][0]['amount'] == 2
    assert account['positions'][0]['entry_price'] == 100
    assert account['positions'][0]['leverage'] == 10
    assert account['leverage'] == 1
    monkeypatch.setattr(service, 'principal', lambda: {**caller, 'scopes': ['read']})
    assert 'adjust_paper_position' not in {tool.name for tool in asyncio.run(server.list_tools())}


def test_admin_position_route_auth_validation_and_retry(ledger):
    caller, _, _ = ledger
    position_id = open_position(caller)
    app = FastAPI()
    app.include_router(management.router)
    headers = {'Authorization': 'Bearer ' + create_access_token()}
    url = f'/api/mcp/paper/accounts/qqbot/123456/positions/{position_id}'
    body = {'operation_id': 'admin-position-add-margin', 'action': 'add_margin', 'amount': 5}
    with TestClient(app) as client:
        assert client.post(url, json=body).status_code == 401
        first = client.post(url, headers=headers, json=body)
        assert first.status_code == 200, first.text
        result = first.json()
        assert result['success'] and result['status'] == 'completed'
        assert result['margin_after'] == 45 and result['cash_change'] == -5
        assert client.post(url, headers=headers, json=body).json() == result
        assert client.post(url, headers=headers, json={**body, 'amount': 6}).status_code == 400
        assert client.post(url, headers=headers, json={**body, 'leverage': 2}).status_code == 422
        assert client.post(url, headers=headers, json={**body, 'amount': True}).status_code == 422
    assert paper.get_account(caller, '123456')['positions'][0]['margin'] == 45
