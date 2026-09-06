"""Isolated UI regression. Run Vite first, then:
uv run --with playwright python tests/browser_dashboard.py
No backend, credentials, scheduler or exchange calls are used.
"""
import json
import os
import re

from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def instrument_chart(route):
            response = route.fetch()
            body = response.text().replace('let candleData = [];', 'window.__testChart = chart; let candleData = [];')
            route.fulfill(response=response, body=body)
        page.route('**/src/components/KlineChart.jsx', instrument_chart)
        page.goto(os.getenv('UI_TEST_URL', 'http://127.0.0.1:5179/tests/dashboard-fixture.html'))
        page.locator('.trading-chart canvas').first.wait_for()
        page.evaluate("window.originalCanvases = [...document.querySelectorAll('.trading-chart canvas')]")
        page.evaluate('window.__testChart.timeScale().setVisibleLogicalRange({from: 10, to: 35})')
        for _ in range(20):
            page.get_by_role('button', name='Refresh', exact=True).click()
        assert page.get_by_test_id('tick').inner_text() == '20'
        visible = page.evaluate('window.__testChart.timeScale().getVisibleLogicalRange()')
        assert abs(visible['from'] - 10) < 0.01 and abs(visible['to'] - 35) < 0.01, visible
        page.get_by_role('button', name='Theme', exact=True).click()
        page.get_by_role('button', name='Symbol', exact=True).click()
        assert page.evaluate("window.originalCanvases.every((node, i) => node === document.querySelectorAll('.trading-chart canvas')[i])")
        page.get_by_role('button', name='应用预设', exact=False).click()
        config = json.loads(page.get_by_test_id('config').inner_text())
        assert config['run_interval'] == 30
        assert [rule['interval'] for rule in config['run_schedule']] == [30, 20, 30]
        page.get_by_role('button', name='提高优先级', exact=True).nth(1).click()
        config = json.loads(page.get_by_test_id('config').inner_text())
        assert config['run_schedule'][0]['timezone'] == 'America/New_York'
        page.get_by_role('button', name='删除时段', exact=True).nth(0).click()
        page.get_by_role('button', name=re.compile('添.*加.*时.*段')).click()
        assert len(json.loads(page.get_by_test_id('config').inner_text())['run_schedule']) == 3
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile horizontal overflow'
        page.screenshot(path=os.getenv('UI_TEST_SCREENSHOT', 'frontend/node_modules/.cache/dashboard-mobile.png'), full_page=True)
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.get_by_role('button', name='Refresh', exact=True).click()
        assert page.evaluate("window.originalCanvases.every(node => node.isConnected)")
        assert not errors, errors
        browser.close()
        print('PASS: mobile refresh/theme/symbol preserve canvases; schedule preset/reorder/add/delete; responsive layout; no browser errors.')


if __name__ == '__main__':
    main()
