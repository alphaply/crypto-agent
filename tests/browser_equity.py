"""Mobile equity chart regression using the isolated Vite fixture.

Start Vite on port 5179; run with:
uv run --with playwright python tests/browser_equity.py
"""
import os

from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(os.getenv('UI_TEST_URL', 'http://127.0.0.1:5179/tests/dashboard-fixture.html'))
        chart = page.locator('.equity-line-chart')
        chart.locator('svg path').first.wait_for(state='attached')
        page.evaluate("""() => {
          const container = document.querySelector('.equity-line-chart');
          window.originalEquitySvg = container.querySelector('svg');
          window.originalEquityId = container.getAttribute('_echarts_instance_');
          window.removedEquitySvgs = 0;
          new MutationObserver(records => {
            for (const record of records) for (const node of record.removedNodes) {
              if (node === window.originalEquitySvg || node.contains?.(window.originalEquitySvg)) window.removedEquitySvgs++;
            }
          }).observe(container, { childList: true, subtree: true });
        }""")
        before = chart.text_content()
        for _ in range(20):
            page.get_by_role('button', name='Refresh', exact=True).click()
        page.wait_for_function("before => document.querySelector('.equity-line-chart').textContent !== before", arg=before)
        assert chart.text_content() != before, 'Equity values did not update'
        page.get_by_text('起点收益率', exact=True).click()
        page.wait_for_function("document.querySelector('.equity-line-chart').textContent.includes('%')")
        page.get_by_role('button', name='Theme', exact=True).click()
        # Mobile browser chrome changes viewport height without changing the chart box.
        for height in [760, 844, 720, 844]:
            page.set_viewport_size({'width': 390, 'height': height})
        page.set_viewport_size({'width': 700, 'height': 390})
        page.wait_for_function("Math.abs(Number(document.querySelector('.equity-line-chart svg').getAttribute('width')) - document.querySelector('.equity-line-chart').clientWidth) < 2")
        assert page.evaluate("""() => {
          const container = document.querySelector('.equity-line-chart');
          return container.querySelector('svg') === window.originalEquitySvg
            && container.getAttribute('_echarts_instance_') === window.originalEquityId
            && window.removedEquitySvgs === 0;
        }"""), 'Equity chart was recreated'
        assert chart.locator('svg path').count() > 0
        page.set_viewport_size({'width': 390, 'height': 844})
        page.get_by_test_id('equity').screenshot(path='frontend/node_modules/.cache/equity-mobile.png')
        assert not errors, errors
        browser.close()
        print('PASS: equity data updates, return mode, theme, mobile resize; same SVG and chart instance throughout; no browser errors.')


if __name__ == '__main__':
    main()
