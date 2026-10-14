"""Run with /app/.venv/bin/python in an isolated, network-disabled smoke container."""
import json
import time
import sys
import urllib.error
import urllib.request


def request(path, body=None, token=None, method=None, mcp=False):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if mcp:
        headers['Accept'] = 'application/json, text/event-stream'
    req = urllib.request.Request('http://localhost:7860' + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


health = request('/health')
assert health['success'] and not health['scheduler']['run_in_web'] and not health['scheduler']['enabled'], health
assert b'<html' in urllib.request.urlopen('http://localhost:7860/', timeout=5).read().lower()
admin = request('/api/auth/login', {'password': 'paper-isolated-smoke-password'})['token']
overview = request('/api/mcp', token=admin)
assert overview['paper_settings']['max_leverage'] == 125
key = request('/api/mcp/keys', {'name': 'Isolated QQBot smoke', 'key_type': 'paper', 'paper_namespace': 'smoke', 'scopes': ['read', 'trade']}, admin)['key']


def rpc(name, args):
    result = request('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}, key['secret'], mcp=True)['result']
    assert not result.get('isError'), result
    if name == 'get_paper_history':
        return [json.loads(block['text']) for block in result['content']]
    value = json.loads(result['content'][0]['text'])
    return value['result'] if isinstance(value, dict) and set(value) == {'result'} else value


assert rpc('get_paper_config', {})['namespace'] == 'smoke'
assert rpc('get_paper_config', {})['market_data_is_simulated'] is False
assert rpc('get_paper_bot_account', {})['account_type'] == 'bot'
created = rpc('create_paper_account', {'user_id': 'qq-001', 'operation_id': 'smoke-create-qq-001'})
assert created['account']['cash'] == 10000
borrow_args = {'user_id': 'qq-001', 'action': 'borrow', 'amount': 500, 'operation_id': 'smoke-borrow-001'}
borrowed = rpc('paper_bank', borrow_args)
assert borrowed['account']['debt'] == 500 and borrowed['account']['net_equity'] == 10000
assert rpc('paper_bank', borrow_args) == borrowed
assert rpc('get_paper_account', {'user_id': 'qq-001'})['cash'] == 10500
request('/api/mcp/paper/accounts/smoke/qq-001', {'cash': 2000, 'debt': 100, 'leverage': 10}, admin, 'PATCH')
assert rpc('get_paper_account', {'user_id': 'qq-001'})['debt'] == 100
try:
    request('/api/mcp/paper/accounts', token=key['secret'])
    raise AssertionError('MCP key obtained admin access')
except urllib.error.HTTPError as exc:
    assert exc.code == 401
assert any(e['kind'] == 'admin_adjustment' for e in rpc('get_paper_history', {'user_id': 'qq-001'}))

# Local ledger validation uses the container's isolated data, never private APIs.
sys.path.insert(0, '/app')
from backend.mcp import paper, service, store
from backend import database
caller = {'id': 'image-smoke', 'key_type': 'paper', 'paper_namespace': 'ledger-smoke', 'scopes': ['read', 'trade'], 'profile_ids': []}
quote = {'price': 100, 'bid': 100, 'ask': 100, 'exchange': 'isolated-public-fixture'}
paper.market = lambda *a, **k: {**quote, 'observed_at': time.time()}
paper.create_account(caller, 'alice', 'image-create-alice')
paper.create_account(caller, 'bob', 'image-create-bob')
paper.trade(caller, 'alice', 'spot', 'BTC/USDT', 'buy', 1, 'image-spot-buy')
paper.trade(caller, 'alice', 'swap', 'ETH/USDT:USDT', 'open_short', 1, 'image-short-entry', 10)
quote.update(price=90, bid=90, ask=90)
paper.trade(caller, 'alice', 'swap', 'ETH/USDT:USDT', 'close_short', 1, 'image-short-exit')
paper.bank(caller, 'alice', 'borrow', 100, 'image-bank-loan')
paper.trade(caller, 'alice', 'swap', 'ETH/USDT:USDT', 'open_long', 1, 'image-long-entry', 10)
quote.update(price=1, bid=1, ask=1)
paper.reconcile()
assert paper.get_account(caller, 'alice')['positions'] == []
assert paper.get_account(caller, 'alice')['debt'] == 100
assert paper.get_account(caller, 'bob')['cash'] == 10000
assert any(e['kind'] == 'liquidation' for e in paper.history(caller, 'alice'))
from backend.mcp import paper_orders, paper_competition
paper.create_account(caller, 'charlie', 'image-create-charlie')
quote.update(price=100, bid=100, ask=100)
limit = paper_orders.place(caller, 'charlie', 'swap', 'BTC', 'open_long', 'limit', 'image-limit-entry',
                          amount=1, leverage=5, price=90, stop_loss=85, take_profit=110)
assert limit['order']['state'] == 'open' and limit['account']['reserved_cash'] > 0
quote.update(price=89, bid=89, ask=89)
paper.reconcile()
assert len(paper_orders.get_orders(caller, 'charlie')['orders']) == 2
quote.update(price=110, bid=110, ask=110)
paper.reconcile()
assert paper.get_account(caller, 'charlie')['positions'] == []
assert len(paper_orders.get_orders(caller, 'charlie', 'filled')['orders']) == 2
assert len(paper_orders.get_orders(caller, 'charlie', 'cancelled')['orders']) == 1
feed_key = request('/api/mcp/keys', {'name': 'Ledger feed smoke', 'key_type': 'paper', 'paper_namespace': 'ledger-smoke', 'scopes': ['read']}, admin)['key']
feed = request('/api/paper/events', token=feed_key['secret'])
assert any(e['kind'] == 'order_filled' and e['recipient_user_id'] == 'charlie' for e in feed['events'])
assert any(e['kind'] == 'liquidation' and e['recipient_user_id'] == 'alice' for e in feed['events'])
assert request('/api/paper/events?after_id=' + str(feed['next_cursor']), token=feed_key['secret'])['events'] == []
ranking = request('/api/paper/leaderboard', token=feed_key['secret'])
assert any(r['account_type'] == 'bot' for r in ranking['items'])
assert all('positions' not in r and 'cash' not in r and 'debt' not in r for r in ranking['items'])
dashboard = request('/api/mcp/paper/dashboard?namespace=ledger-smoke', token=admin)
assert dashboard['orders'] and dashboard['events'] and dashboard['leaderboard']
with database.get_db_conn() as conn:
    store.initialize_schema(conn)
    conn.commit()
try:
    service.require({**caller, 'profile_ids': ['real']}, 'trade', 'real')
    raise AssertionError('Paper key reached live profile')
except PermissionError:
    pass
request('/api/mcp/keys/' + key['id'], token=admin, method='DELETE')
print('PASS: health, SPA, HTTP MCP, bot account, account/loan receipts, admin controls, auth isolation, spot/long/short, liquidation, limit reservations and auto fills, OCO TP/SL, leaderboard, HTTP event cursor, dashboard and repeat migration')
