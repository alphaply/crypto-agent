"""Real MCP settings UI interactions with mocked backend, desktop and mobile."""
import copy
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


def exercise(browser, width, locale):
    zh = locale == 'zh'
    page = browser.new_page(viewport={'width': width, 'height': 1000}, reduced_motion='reduce')
    errors, writes = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    cfg = {'enabled': True, 'initial_balance': 10000, 'max_leverage': 125, 'loan_limit': 1000000, 'fee_rate': .001, 'maintenance_margin_rate': .005, 'exchange': 'binance', 'bank_enabled': True, 'daily_interest_rate': .001}
    account = {'namespace': 'qqbot', 'user_id': '123456', 'account_status': 'bankrupt', 'account_epoch': 2, 'reset_count': 2, 'name': 'QQ user', 'cash': 9999, 'debt': 100, 'loan_limit': 5000, 'leverage': 10, 'markets': ['spot', 'swap'], 'enabled': True, 'used_margin': 100, 'net_equity': 9999, 'spot': [], 'positions': [], 'fees': 1}
    keys = []
    def reply(route, value):
        route.fulfill(content_type='application/json', body=json.dumps(value))
    def handle(route):
        url = urlsplit(route.request.url)
        method = route.request.method
        if url.path == '/api/mcp' and method == 'GET':
            return reply(route, {'settings': {'enabled': True, 'public_url': 'https://example.test'}, 'connection': {'url': 'https://example.test/mcp'}, 'profiles': [], 'keys': keys, 'oauth_connections': [], 'tools': [], 'audit': [], 'paper_settings': cfg})
        if url.path == '/api/mcp/paper/dashboard':
            position = {'symbol': 'BTC/USDT:USDT', 'side': 'LONG', 'exchange': 'binance', 'amount': .01, 'entry_price': 80000, 'mark_price': 83000, 'unrealized_pnl': 30, 'leverage': 5, 'marked_at': 1791968157}
            open_order = {'order_id': 'fixture-limit', 'user_id': account['user_id'], 'symbol': 'BTC/USDT:USDT', 'exchange': 'binance', 'action': 'open_long', 'order_type': 'limit', 'price': 79000, 'amount': .01, 'state': 'open', 'reserved_cash': 158.79}
            tp_order = {**open_order, 'order_id': 'fixture-tp', 'action': 'close_long', 'order_type': 'take_profit', 'price': None, 'trigger_price': 85000, 'close_all': True, 'oco_group': 'fixture-oco', 'reserved_cash': 0}
            events = [{'id': 101, 'timestamp': 1791968157, 'user_id': account['user_id'], 'kind': 'order_filled', 'payload': {'fill': {'symbol': 'BTC/USDT:USDT', 'price': 80000}}}, {'id': 102, 'timestamp': 1791968158, 'user_id': account['user_id'], 'kind': 'liquidation', 'payload': {'symbol': 'ETH/USDT:USDT', 'price': 2000, 'debt_after': 500}}]
            return reply(route, {'accounts': [{**account, 'positions': [position]}], 'account_total': 2, 'summary': {'cash': account['cash'], 'debt': account['debt'], 'net_equity': account['net_equity'], 'open_orders': 2}, 'orders': [open_order, tp_order], 'events': events, 'reset_leaderboard': [{'participant_id': 'resets-user', 'name': 'QQ user', 'rank': 1, 'reset_count': 2}], 'leaderboard': [{'participant_id': 'bot', 'rank': 1, 'name': '小金萌智子', 'account_type': 'bot', 'pnl': 200, 'return_pct': 2}, {'participant_id': 'user', 'rank': 2, 'name': 'QQ user', 'account_type': 'user', 'pnl': 100, 'return_pct': 1}]})
        if method in {'PUT', 'PATCH', 'POST'}:
            body = route.request.post_data_json
            writes.append((method, url.path, body))
            if url.path == '/api/mcp/keys':
                keys.append({**body, 'id': 'fixture-paper', 'secret': 'fixture-key-not-real', 'revoked': False})
            elif url.path == '/api/mcp/paper/settings':
                cfg.update(body)
            elif method == 'PATCH':
                account.update(body)
            return reply(route, {'success': True})
        if url.path == '/api/mcp/paper/accounts':
            query = parse_qs(url.query)
            rows = [copy.deepcopy(account)] if query.get('user_id', [account['user_id']])[0] == account['user_id'] else []
            return reply(route, {'items': rows, 'total': len(rows)})
        reply(route, {'success': True})
    page.route('**/api/**', handle)
    page.goto(f'http://127.0.0.1:18973/tests/paper-fixture.html?locale={locale}')
    title = 'QQBot 模拟交易 · Paper' if zh else 'QQBot paper trading'
    expect(page.get_by_text(title, exact=True)).to_have_count(0)
    page.get_by_role('tab', name='QQBot · Paper', exact=True).click()
    panel = page.locator('.ant-card').filter(has=page.get_by_text(title, exact=True))
    expect(panel.get_by_text('123456', exact=True).first).to_be_visible()
    panel.get_by_role('button', name='创建 Paper Key' if zh else 'Create paper key', exact=True).click()
    dialog = page.get_by_role('dialog')
    expect(dialog.get_by_role('combobox', name='Key 类型' if zh else 'Key type')).to_be_visible()
    expect(dialog.get_by_role('textbox', name='Paper 命名空间' if zh else 'Paper namespace')).to_have_value('qqbot')
    dialog.locator('.ant-modal-footer .ant-btn-primary').click()
    expect(dialog).not_to_be_visible()
    assert keys[0]['key_type'] == 'paper' and keys[0]['profile_ids'] == [] and keys[0]['scopes'] == ['read', 'trade']
    panel.get_by_role('button', name='调整账户' if zh else 'Adjust account', exact=True).click()
    dialog = page.get_by_role('dialog')
    dialog.get_by_role('spinbutton', name='cash', exact=True).fill('2500')
    dialog.get_by_role('spinbutton', name='debt', exact=True).fill('500')
    dialog.locator('.ant-modal-footer .ant-btn-primary').click()
    expect(dialog).not_to_be_visible()
    assert account['cash'] == 2500 and account['debt'] == 500
    assert any(path == '/api/mcp/paper/accounts/qqbot/123456' and method == 'PATCH' for method, path, _ in writes)
    field = panel.locator('.form-field').filter(has=page.get_by_text('新账户初始金额 · USDT' if zh else 'New account initial cash · USDT', exact=True))
    field.get_by_role('spinbutton').fill('2000')
    panel.get_by_role('button', name='保存模拟交易设置' if zh else 'Save paper settings', exact=True).click()
    expect(field.get_by_role('spinbutton')).to_have_value('2000')
    expect(panel.get_by_role('button', name='保存模拟交易设置' if zh else 'Save paper settings', exact=True)).to_be_enabled()
    assert cfg['initial_balance'] == 2000
    page.get_by_role('tab', name='MCP', exact=True).click()
    expect(panel).not_to_be_visible()
    page.get_by_role('tab', name='QQBot · Paper', exact=True).click()
    expect(field.get_by_role('spinbutton')).to_have_value('2000')
    output = Path('docs/screenshots/account-lifecycle')
    output.mkdir(parents=True, exist_ok=True)
    data_panel = panel.locator('.ant-card').filter(has=page.get_by_text('模拟盘数据 · 持仓 / 委托 / 排行 / 事件' if zh else 'Paper data · positions / orders / ranking / events', exact=True))
    expect(data_panel.get_by_text('BTC/USDT:USDT', exact=True).first).to_be_visible()
    data_panel.screenshot(path=str(output / f'paper-v2-positions-{width}-{locale}.png'))
    for key, tab, expected_text in [('orders', '挂单 / 止盈止损' if zh else 'Orders / TP / SL', 'take_profit'), ('ranking', '交易收益率榜' if zh else 'Trading return ranking', '小金萌智子'), ('resets', '下海排行榜' if zh else 'Reset ranking', 'QQ user'), ('events', '成交 / 爆仓事件' if zh else 'Fill / liquidation events', '爆仓' if zh else 'Liquidation')]:
        target = data_panel.get_by_role('tab', name=tab, exact=True, include_hidden=True)
        target.dispatch_event('click')
        expect(target).to_have_attribute('aria-selected', 'true')
        expect(data_panel.locator('.ant-tabs-tabpane-active').get_by_text(expected_text, exact=False).first).to_be_visible()
        data_panel.screenshot(path=str(output / f'paper-v2-{key}-{width}-{locale}.png'))
    panel.screenshot(path=str(output / f'paper-trading-{width}-{locale}.png'))
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Page overflows viewport'
    assert not errors, errors
    page.close()


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel=os.getenv('PAPER_TEST_BROWSER_CHANNEL'))
    for width, locale in [(1440, 'zh'), (1440, 'en'), (390, 'zh')]:
        exercise(browser, width, locale)
    browser.close()
print('PASS: real paper key creation, account cash/debt editing, settings save; desktop zh/en and mobile zh')
