"""Mock-only memory management checks; Vite fixture on port 18971."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from playwright.sync_api import sync_playwright, expect


def exercise(browser, width, locale):
    page = browser.new_page(viewport={'width': width, 'height': 960}, reduced_motion='reduce')
    page.set_default_timeout(8000)
    errors, writes, reads = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    failed = {'value': True, 'network': False}
    def handle(route):
        path = urlsplit(route.request.url).path
        if 'projections' in path:
            reads.append(path)
            if failed['network']:
                route.fulfill(status=503, content_type='application/json', body=json.dumps({'detail': 'status offline'}))
                return
            cid = parse_qs(urlsplit(route.request.url).query).get('config_id', ['cfg'])[0]
            if route.request.method == 'POST':
                writes.append(route.request.post_data_json)
                failed['value'] = False
            payload = {'success': True, 'enabled': True, 'config_id': cid, 'pending': 0 if failed['value'] else 1,
                       'failed': int(failed['value']), 'not_generated': 0, 'read_errors': [],
                       'published': {'short_memory': [{'as_of': '2026-10-13 04:00:00'}],
                                     'strategy_summary': [{'as_of': '2026-10-13 03:30:00'}]},
                       'errors': [{'source_kind': 'short_memory', 'source_id': '1', 'as_of': '2026-10-13 05:00:00', 'error': 'model offline'}] if failed['value'] else []}
        else:
            payload = {'success': True, 'short_memories': [{'id': 1, 'config_id': 'cfg', 'symbol': 'ETH/USDT',
                        'bucket_start': '2026-10-13 00:00:00', 'bucket_end': '2026-10-13 04:00:00', 'market_summary': '历史原文保留', 'source_count': 3}]}
        route.fulfill(content_type='application/json', body=json.dumps(payload))
    page.route('**/api/**', handle)
    page.goto(f'http://127.0.0.1:18971/tests/decision-memory-fixture.html?locale={locale}')
    expect(page.get_by_test_id('decision-memory-status')).to_have_count(0)
    page.get_by_role('combobox').nth(1).click()
    page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').get_by_text('cfg', exact=True).click()
    status = page.get_by_test_id('decision-memory-status')
    expect(status).to_contain_text('2026-10-13 04:00:00')
    expect(status).to_contain_text('失败 1' if locale == 'zh' else 'Failed 1')
    status.locator('summary').click()
    expect(status).to_contain_text('model offline')
    Path('docs/screenshots').mkdir(parents=True, exist_ok=True)
    page.screenshot(path=f'docs/screenshots/decision-memory-status-{width}-{locale}.png')
    status.get_by_role('button', name='重试失败整理' if locale == 'zh' else 'Retry failed consolidation').click()
    expect(status).to_contain_text('待整理 1' if locale == 'zh' else 'Queued 1')
    expect(status).to_contain_text('2026-10-13 04:00:00')
    assert writes == [{'config_id': 'cfg'}]
    failed['network'] = True
    status.get_by_role('button', name='刷新状态' if locale == 'zh' else 'Refresh status').click()
    expect(status.get_by_role('alert')).to_contain_text('status offline')
    expect(status).to_contain_text('2026-10-13 04:00:00')
    assert not errors, errors
    # Public users never fetch admin status or expose a retry action.
    reads.clear()
    page.goto(f'http://127.0.0.1:18971/tests/decision-memory-fixture.html?public=true&locale={locale}')
    page.get_by_role('combobox').nth(1).click()
    page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').get_by_text('cfg', exact=True).click()
    expect(page.locator('.ant-table-tbody').get_by_text('cfg', exact=True)).to_be_visible()
    expect(page.get_by_test_id('decision-memory-status')).to_have_count(0)
    assert not reads and not errors
    page.close()


if __name__ == '__main__':
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        for width, locale in [(1440, 'zh'), (390, 'zh'), (1440, 'en')]:
            exercise(browser, width, locale)
        browser.close()
    print('3 memory management UI cases passed')
