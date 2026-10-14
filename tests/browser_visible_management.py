"""Offline UI acceptance: Vite on 127.0.0.1:18972; no exchange requests."""
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

OUTPUT = Path(__file__).resolve().parents[1] / 'docs/screenshots'


def exercise(browser, width, locale):
    zh = locale == 'zh'
    page = browser.new_page(viewport={'width': width, 'height': 1000 if width > 500 else 844}, reduced_motion='reduce')
    page.set_default_timeout(10000)
    errors, writes = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    claim = {'claim_id': 'claim-1', 'symbol': 'ETH/USDT:USDT', 'side': 'LONG', 'owner': 'fixture', 'state': 'owned',
             'source': 'baseline', 'sync_status': 'verified', 'claimed_at': 1791952200, 'observed_at': 1791952270}
    transferred = False
    conflict = False

    def respond(route, body, status=200):
        route.fulfill(status=status, content_type='application/json', body=json.dumps(body))

    def handle(route):
        nonlocal transferred, conflict
        path = urlsplit(route.request.url).path
        if path == '/api/management/fixture' and route.request.method == 'GET':
            respond(route, {'management': {'policy': 'visible_first_claim', 'owner': 'fixture', 'scopes': [] if transferred else [claim]},
                            'recovery_targets': [{'recovery_id': 'handoff:accepted', 'operation_id': 'original-unknown', 'source_owner': 'old-task'}]})
        elif path.endswith('/release'):
            writes.append(route.request.post_data_json)
            conflict = True
            respond(route, {'detail': {'status': 'blocked', 'conflicts': ['active_or_unresolved_resources'],
                                      'resources': {'positions': [{'amount': .01}], 'unresolved': [{'operation_id': 'original-unknown'}]}}}, 409)
        elif path.endswith('/transfer'):
            writes.append(route.request.post_data_json)
            transferred = True
            respond(route, {'success': True, 'status': 'transferred', 'owner': 'receiver',
                            'resources': {'positions': [{'amount': .01}]}, 'recovery_targets': [{'operation_id': 'original-unknown'}]})
        elif '/recovery/' in path:
            respond(route, {'success': True, 'query_only': True, 'result': {'operation_id': 'original-unknown', 'status': 'unknown',
                            'query': {'status': 'completed'}, 'results': [{'index': 0, 'action': 'cancel', 'status': 'pending'}]}})
        elif path == '/api/mcp':
            respond(route, {'settings': {'enabled': True, 'public_url': 'https://fixture.invalid'},
                            'connection': {'url': 'https://fixture.invalid/mcp'}, 'profiles': [{'profile_id': 'paused', 'name': 'Paused owner',
                            'exchange_profile_id': 'saved', 'market_type': 'swap', 'symbol_scope': 'all', 'symbols': [], 'enabled': False,
                            'max_leverage': 5, 'leverage': 1, 'position_management_policy': 'visible_first_claim'}], 'tools': [], 'keys': [], 'audit': []})
        elif path == '/api/management/mcp:paused':
            respond(route, {'management': {'policy': 'visible_first_claim', 'owner': 'mcp:paused', 'scopes': []}, 'recovery_targets': []})
        elif '/performance/' in path:
            respond(route, {'performance': {'equity': {'available': False}, 'profit': {'available': False}, 'drawdown': {'available': False}}})
        else:
            respond(route, {})

    page.route('**/api/**', handle)
    page.goto(f'http://127.0.0.1:18972/tests/management-fixture.html?locale={locale}')
    expect(page.get_by_text('可见范围自动管理 · 先到先得' if zh else 'Automatic visible management · First claim wins', exact=True)).to_be_visible()
    expect(page.get_by_text('暂停' if zh else 'Paused', exact=True)).to_be_visible()
    assert page.get_by_role('combobox', name='仓位管理策略' if zh else 'Position management policy').count() == 0
    page.get_by_role('button', name='模拟人工减仓及改单' if zh else 'Simulate external reduction and edit').click()
    expect(page.get_by_text('0.01 @ 2415', exact=True)).to_be_visible()
    assert '2580' in page.locator('body').inner_text()
    assert page.get_by_text('先 adopt', exact=False).count() == 0
    OUTPUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUTPUT / f'visible-management-{width}-{locale}.png'), full_page=True)
    page.get_by_role('button', name='核验并释放' if zh else 'Verify and release', exact=True).click()
    page.get_by_role('button', name='OK', exact=True).click()
    expect(page.get_by_text('管理权保留，操作未完成' if zh else 'Ownership retained; action incomplete', exact=True)).to_be_visible()
    assert conflict and writes[-1] == {'confirmed': True}
    assert 'original-unknown' in page.locator('body').inner_text()
    page.screenshot(path=str(OUTPUT / f'visible-management-conflict-{width}-{locale}.png'), full_page=True)
    target = page.get_by_label('接收任务或配置 ID' if zh else 'Receiving task or profile ID')
    target.fill('receiver')
    transfer = page.get_by_role('button', name='接受并交接' if zh else 'Accept and transfer', exact=True)
    expect(transfer).to_be_disabled()
    page.get_by_role('checkbox').first.check()
    transfer.click()
    page.get_by_role('button', name='OK', exact=True).click()
    expect(page.get_by_text('管理操作详情' if zh else 'Management operation details', exact=True)).to_be_visible()
    assert writes[-1] == {'target_config_id': 'receiver', 'accepted': True}
    expect(page.get_by_role('button', name='核验并释放' if zh else 'Verify and release', exact=True)).to_have_count(0)
    page.get_by_role('button', name='核验原操作' if zh else 'Inspect original operation', exact=True).click()
    expect(page.locator('details pre').last).to_contain_text('query_only')
    expect(page.locator('details pre').last).to_contain_text('original-unknown')
    page.screenshot(path=str(OUTPUT / f'visible-management-transfer-{width}-{locale}.png'), full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 2')
    assert not errors, errors
    page.close()


if __name__ == '__main__':
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        for dimensions in [(1440, 'zh'), (1440, 'en'), (390, 'zh'), (390, 'en')]:
            exercise(browser, *dimensions)
            print(f'PASS: {dimensions[0]}px {dimensions[1]}')
        browser.close()
