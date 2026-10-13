"""Offline snapshot cleanup UI checks; run Vite on port 18971."""
import json
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect


def exercise(browser, width, locale, workspace=False):
    page = browser.new_page(viewport={'width': width, 'height': 960}, reduced_motion='reduce')
    page.set_default_timeout(8000)
    errors, writes = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    row = {'id': 1, 'config_id': 'cfg', 'symbol': 'ETH/USDT', 'bucket_start': '2026-10-13 00:00:00',
           'bucket_end': '2026-10-13 04:00:00', 'market_summary': '保留策略正文', 'source_count': 3,
           'position_summary': '历史原始回执\n\n' + '\n\n'.join(json.dumps([{
               'tool': 'Edit', 'result': 'Error: Tool not allowed.', 'current_evidence': {'resolution': 'unresolved'},
           }]) for _ in range(80)) + '\n\n完整尾部证据'}
    page.add_init_script('window.__memorySnapshotFixture = ' + json.dumps(row) + ';')
    offline = {'value': True}
    def handle(route):
        path = urlsplit(route.request.url).path
        if route.request.method == 'DELETE':
            assert path == '/api/history/short-memories/execution-snapshot'
            writes.append(route.request.post_data_json)
            if offline['value']:
                route.fulfill(status=503, content_type='application/json', body=json.dumps({'detail': 'snapshot offline'}))
                return
            row['position_summary'] = ''
            payload = {'success': True, 'updated': 1}
        elif route.request.method == 'PUT':
            assert 'position_summary' not in route.request.post_data_json
            writes.append(route.request.post_data_json)
            row['market_summary'] = route.request.post_data_json['market_summary']
            payload = {'success': True}
        else:
            payload = {'success': True, 'short_memories': [row]}
        route.fulfill(content_type='application/json', body=json.dumps(payload))
    page.route('**/api/**', handle)
    suffix = '&workspace=true' if workspace else ''
    page.goto(f'http://127.0.0.1:18971/tests/decision-memory-fixture.html?locale={locale}{suffix}')
    if not workspace:
        page.locator('.ant-table-row-expand-icon').first.click()
    snapshot = page.locator('.memory-execution-snapshot')
    expect(snapshot).to_have_count(1)
    expect(page.locator('.memory-snapshot-content')).to_have_count(0)
    snapshot.locator('summary').click()
    content = page.locator('.memory-snapshot-content')
    expect(content).to_contain_text('完整尾部证据')
    box = content.evaluate('(el) => ({height: el.clientHeight, total: el.scrollHeight})')
    assert box['height'] <= 360 and box['total'] > box['height']
    label = '清除快照' if locale == 'zh' else 'Clear snapshot'
    snapshot.get_by_role('button', name=label, exact=True).click()
    page.locator('.ant-popconfirm').get_by_role('button', name=re.compile(r'取\s*消|Cancel')).click()
    assert not writes
    snapshot.get_by_role('button', name=label, exact=True).click()
    page.locator('.ant-popconfirm').get_by_role('button', name=label, exact=True).click()
    expect(page.get_by_text('snapshot offline', exact=True)).to_be_visible()
    expect(content).to_contain_text('完整尾部证据')
    offline['value'] = False
    screenshot = Path(tempfile.gettempdir()) / f'memory-snapshot-{width}-{locale}-{workspace}.png'
    page.screenshot(path=str(screenshot))
    snapshot.get_by_role('button', name=label, exact=True).click()
    page.locator('.ant-popconfirm').get_by_role('button', name=label, exact=True).click()
    expect(snapshot).to_have_count(0)
    expect(page.get_by_text('保留策略正文', exact=True)).to_be_visible()
    identity = {'config_id': 'cfg', 'bucket_start': row['bucket_start']}
    assert writes == [identity, identity]
    if not workspace:
        page.get_by_role('button', name='edit', exact=True).click()
        page.locator('.ant-modal textarea').fill('编辑后的策略正文')
        page.locator('.ant-modal').get_by_role('button', name='save', exact=True).click()
        expect(page.locator('.ant-modal')).not_to_be_visible()
        assert writes[-1] == {**identity, 'market_summary': '编辑后的策略正文'}
    assert not errors, errors
    # Public view retains the full snapshot but never exposes a write action.
    row['position_summary'] = '公开历史快照'
    page.goto(f'http://127.0.0.1:18971/tests/decision-memory-fixture.html?public=true&locale={locale}{suffix}')
    if not workspace:
        page.locator('.ant-table-row-expand-icon').first.click()
    page.locator('.memory-execution-snapshot summary').click()
    expect(page.locator('.memory-snapshot-content')).to_be_visible()
    expect(page.get_by_role('button', name=label, exact=True)).to_have_count(0)
    assert not errors, errors
    page.close()
    print(f'Snapshot UI passed: {width}px {locale}, workspace={workspace}; {screenshot}')


if __name__ == '__main__':
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        for width, locale, workspace in [(1440, 'zh', False), (390, 'zh', False), (1440, 'en', False), (1440, 'zh', True), (390, 'zh', True)]:
            exercise(browser, width, locale, workspace)
        browser.close()
