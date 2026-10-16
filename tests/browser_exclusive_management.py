"""Offline UI acceptance. Start Vite at 127.0.0.1:18973; all APIs are mocked."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

OUTPUT = Path(__file__).resolve().parents[1] / 'docs/screenshots/account-lifecycle'


def exercise(browser, width, locale):
    zh = locale == 'zh'
    page = browser.new_page(viewport={'width': width, 'height': 1000 if width > 500 else 844},
                            reduced_motion='reduce', is_mobile=width < 500)
    page.set_default_timeout(10000)
    errors, calls, writes = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    performance = {'as_of': '2026-10-14T04:00:09Z',
                   'equity': {'available': True, 'start': {'timestamp': '2026-10-14 00:00:18', 'equity': 591.12},
                              'latest': {'timestamp': '2026-10-14 12:00:09', 'equity': 589},
                              'change': -2.12, 'return_pct': -.36, 'currency': 'USDT'},
                   'profit': {'available': False, 'amount': None},
                   'drawdown': {'available': True, 'current_pct': .36, 'max_pct': .36}}
    profile = {'profile_id': 'connection', 'name': 'Connection fixture', 'exchange_profile_id': 'saved',
               'market_type': 'swap', 'symbol_scope': 'all', 'symbols': [], 'enabled': True,
               'max_leverage': 5, 'leverage': 1, 'exit_mode': 'independent_exits'}

    def respond(route, body, status=200):
        route.fulfill(status=status, content_type='application/json', body=json.dumps(body))

    def handle(route):
        path = urlsplit(route.request.url).path
        calls.append(path)
        if path.endswith('/performance/fixture/details'):
            respond(route, {'performance': performance, 'details': {'ledger': {
                'window_complete': False, 'unknown_owner_fill_count': 5, 'missing_pnl_count': 0,
                'missing_fee_count': 0, 'fees_by_currency': {}, 'recent_fills': [],
                'position_cycles': {'completed': [], 'unmatched_fill_count': 7},
                'sync': [{'symbol': 'ETH/USDT:USDT', 'complete': True}],
                'active_protection': [{'state': 'WAITING'}], 'execution_review': {'observation_count': 0}},
                'closed_positions': [], 'equity': {'start_equity': 591.12}}})
        elif path.endswith('/performance/fixture'):
            respond(route, {'performance': performance})
        elif path.endswith('/short-memories/12/details'):
            respond(route, {'short_memory': {'bucket_start': '2026-10-14 12:00:00',
                'market_summary': 'Historical operation_id=null resolution=unresolved independent_position_cycle_missing',
                'position_summary': 'Historical ledger evidence'}, 'historical': True})
        elif path == '/api/config/market-symbols':
            respond(route, {'symbols': [{'symbol': 'ETH/USDT:USDT', 'display_name': 'ETH perpetual'}],
                'selected_symbols': [{'symbol': 'ETH/USDT:USDT'}], 'invalid_symbols': [], 'total': 1})
        elif path == '/api/mcp/profiles/connection':
            writes.append(route.request.post_data_json)
            respond(route, {'success': True})
        elif path == '/api/mcp':
            respond(route, {'settings': {'enabled': True, 'public_url': 'https://fixture.invalid'},
                            'connection': {'url': 'https://fixture.invalid/mcp'}, 'profiles': [profile],
                            'keys': [], 'oauth_connections': [], 'tools': [], 'audit': []})
        else:
            respond(route, {})

    page.route('**/api/**', handle)
    page.goto(f'http://127.0.0.1:18973/tests/management-fixture.html?locale={locale}')
    expect(page.get_by_text('-2.12 USDT', exact=True)).to_be_visible()
    expect(page.get_by_text('暂不可统计' if zh else 'Unavailable', exact=True)).to_be_visible()
    assert not any(path.endswith('/details') for path in calls), calls
    compact_text = page.locator('.ant-card').filter(has=page.get_by_text('收益与回撤' if zh else 'Performance', exact=True)).inner_text()
    compact_text += page.locator('.ant-card').filter(has=page.get_by_text('策略记忆' if zh else 'Strategy memory', exact=True)).inner_text()
    for hidden in ('operation_id', 'resolution', 'active_protection', 'execution_review', 'window_complete'):
        assert hidden not in compact_text, hidden

    expect(page.get_by_text('授权范围共享管理' if zh else 'Shared management within authorized scope', exact=True)).to_be_visible()
    assert page.get_by_role('combobox', name='仓位管理策略' if zh else 'Position management policy').count() == 0
    page.get_by_role('button', name='模拟人工减仓及改单' if zh else 'Simulate external reduction and edit').click()
    expect(page.get_by_text('0.01 @ 2415', exact=True)).to_be_visible()
    expect(page.get_by_text('TP 2580 · SL 2458 · Limit 2385', exact=True)).to_be_visible()

    page.get_by_text('查看账本详情' if zh else 'View ledger details', exact=True).click()
    expect(page.get_by_text('同步、归属与完整审计证据' if zh else 'Synchronization, attribution and full audit evidence', exact=True)).to_be_visible()
    assert any(path.endswith('/performance/fixture/details') for path in calls)
    page.get_by_text('同步、归属与完整审计证据' if zh else 'Synchronization, attribution and full audit evidence', exact=True).click()
    expect(page.get_by_label('历史账本证据' if zh else 'Historical ledger evidence').last).to_contain_text('active_protection')
    page.get_by_text('历史原文与审计详情' if zh else 'Historical text and audit details', exact=True).click()
    expect(page.get_by_label('历史记忆原文' if zh else 'Original historical memory')).to_contain_text('operation_id=null')
    page.get_by_text('历史原文与审计详情' if zh else 'Historical text and audit details', exact=True).click()
    page.get_by_text('查看账本详情' if zh else 'View ledger details', exact=True).click()
    screenshot = OUTPUT / f'exclusive-management-{width}-{locale}.png'
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshot), full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 2')

    page.get_by_role('button', name=re.compile(r'^编\s*辑$') if zh else 'Edit', exact=not zh).click()
    dialog = page.get_by_role('dialog')
    expect(dialog.get_by_text('授权范围共享管理' if zh else 'Shared management within authorized scope', exact=True)).to_be_visible()
    assert dialog.get_by_role('combobox', name='仓位管理策略' if zh else 'Position management policy').count() == 0
    dialog.get_by_role('button', name='OK').click()
    expect(dialog).not_to_be_visible()
    assert writes[-1]['symbols'] == []
    assert writes[-1]['position_management_policy'] == 'visible_first_claim'
    assert writes[-1]['management_scopes'] == []
    page.screenshot(path=str(OUTPUT / f'exclusive-management-settings-{width}-{locale}.png'), full_page=True)
    assert not errors, errors
    page.close()


def task_editor(browser, width, locale):
    zh = locale == 'zh'
    page = browser.new_page(viewport={'width': width, 'height': 1000 if width > 500 else 844}, reduced_motion='reduce')
    page.set_default_timeout(10000)
    writes, errors = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    payload = {'globals': {'secrets': {}}, 'llm_providers': [], 'prompts': {'files': []}, 'options': {},
        'agents': [{'config_id': 'task-fixture', 'title': 'Existing exposure', 'symbol': 'ETH/USDT:USDT',
                    'mode': 'REAL', 'enabled': True, 'llm_provider_id': 'provider-fixture', 'exchange_profile_id': 'saved', 'market_type': 'swap',
                    'exit_mode': 'independent_exits', 'leverage': 1, 'interval': 60}],
        'exchange_profiles': [{'profile_id': 'saved', 'name': 'Binance', 'exchange': 'binance', 'market_type': 'swap', 'secrets': {}}]}

    def handle(route):
        if route.request.method == 'PUT':
            writes.append(route.request.post_data_json)
            route.fulfill(content_type='application/json', body=json.dumps({'success': True}))
        else:
            route.fulfill(content_type='application/json', body=json.dumps(payload))

    page.route('**/api/**', handle)
    page.goto(f'http://127.0.0.1:18973/tests/management-fixture.html?locale={locale}&surface=tasks')
    page.get_by_role('button', name='Edit', exact=True).click()
    dialog = page.get_by_role('dialog')
    expect(dialog.get_by_text('授权范围共享管理' if zh else 'Shared management within authorized scope', exact=True)).to_be_visible()
    dialog.get_by_role('button', name='save', exact=True).click()
    expect(dialog).not_to_be_visible()
    assert writes[-1]['agents'][0]['position_management_policy'] == 'visible_first_claim'
    assert writes[-1]['agents'][0]['management_scopes'] == []
    assert not errors, errors
    page.close()


def partial_order_warning(browser, width, locale):
    zh = locale == 'zh'
    page = browser.new_page(viewport={'width': width, 'height': 900}, reduced_motion='reduce')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(f'http://127.0.0.1:18973/tests/management-fixture.html?locale={locale}&surface=partial')
    page.get_by_role('tab', name='行情与账户' if zh else 'Market and account').click()
    expect(page.get_by_text('部分订单通道不可用，以下只展示已读取的挂单。' if zh else 'Some order channels are unavailable. Only retrieved orders are shown.', exact=True)).to_be_visible()
    expect(page.get_by_text('挂单读取不完整，不能确认无挂单。' if zh else 'Order data is incomplete; no-orders status is unconfirmed.', exact=True)).to_be_visible()
    expect(page.get_by_text('仓位读取不完整，不能确认空仓。' if zh else 'Position data is incomplete; flat exposure is not confirmed.', exact=True)).to_be_visible()
    assert 'noActivePositions' not in page.locator('body').inner_text()
    assert not errors, errors
    OUTPUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUTPUT / f'exclusive-management-partial-{width}-{locale}.png'), full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 2')
    page.close()


if __name__ == '__main__':
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        import sys
        partial_only = '--partial-only' in sys.argv
        for dimensions in [(1440, 'zh'), (1440, 'en'), (390, 'zh'), (390, 'en')]:
            if not partial_only:
                exercise(browser, *dimensions)
                task_editor(browser, *dimensions)
            partial_order_warning(browser, *dimensions)
            print(f'PASS: {dimensions[0]}px {dimensions[1]}')
        browser.close()
