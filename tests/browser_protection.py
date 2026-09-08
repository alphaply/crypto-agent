"""Mocked UI regression. Run Vite on 18971; no backend or exchange requests."""
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors, submissions = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))

        def api(route):
            url = route.request.url
            if '/position/protection' in url:
                submissions.append(route.request.post_data_json)
                response = {'success': False, 'state': 'ACTIVE', 'error': '保护核验失败，请重试'} if len(submissions) == 1 else {'success': True, 'state': 'ACTIVE'}
            elif '/database/analysis' in url:
                response = {'integrity': 'ok', 'file_bytes': 229421056, 'reclaimable_bytes': 21610496,
                            'unlinked_fills': 17, 'tables': [{'table': 'summaries', 'rows': 15000, 'cleanable': True}]}
            else:
                response = {'positions': [], 'activity': '最近7天：未关联成交17条'}
            route.fulfill(status=200, content_type='application/json', body=json.dumps(response))

        page.route('**/api/**', api)
        page.goto('http://127.0.0.1:18971/tests/protection-fixture.html')
        assert page.get_by_role('button', name='2501.2', exact=True).count() == 1
        assert page.get_by_role('button', name='2515', exact=True).count() == 1
        page.get_by_role('button', name=re.compile('adjustTpSl')).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_role('spinbutton').nth(0).fill('2520')
        dialog.get_by_role('button', name='save', exact=True).click()
        page.get_by_text('保护核验失败，请重试', exact=True).wait_for()
        assert dialog.is_visible()
        assert submissions[0]['symbol'] == 'ETH/USDT:USDT'
        assert submissions[0]['expected_revision'] == 4
        dialog.get_by_role('spinbutton').nth(0).fill('')
        dialog.get_by_role('button', name='save', exact=True).click()
        dialog.wait_for(state='hidden')
        assert submissions[1]['take_profit'] is None
        assert submissions[1]['clear_take_profit'] is False
        page.get_by_role('button', name=re.compile('adjustTpSl')).click()
        dialog.get_by_role('checkbox', name='clearTp', exact=True).check()
        dialog.get_by_role('button', name='save', exact=True).click()
        dialog.wait_for(state='hidden')
        assert submissions[2]['clear_take_profit'] is True
        output = Path('frontend/node_modules/.cache')
        output.mkdir(parents=True, exist_ok=True)
        page.get_by_role('button', name=re.compile('adjustTpSl')).click()
        page.screenshot(path=str(output / 'protection-desktop.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'mobile overflow'
        page.screenshot(path=str(output / 'protection-mobile.png'), full_page=True)
        assert not errors, errors
        browser.close()
        print('PASS: failed update retains dialog; exact symbol/revision; blank preserves; explicit clear; mobile layout.')


if __name__ == '__main__':
    main()
