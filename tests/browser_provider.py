"""Provider drawer regression using mocked APIs; Vite must run on port 18971."""
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        for width, height in [(1440, 900), (1366, 768), (390, 844)]:
            page = browser.new_page(viewport={'width': width, 'height': height},
                                    is_mobile=width < 500, has_touch=width < 500)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            payload = {
                'globals': {'secrets': {}}, 'agents': [], 'exchange_profiles': [], 'prompts': {'files': []},
                'llm_providers': [{'provider_id': 'fixture', 'name': 'Test provider', 'model': 'test-model',
                                   'api_base': 'https://example.invalid/v1', 'thinking_enabled': True,
                                   'reasoning_effort': 'medium', 'extra_body': {}}],
                'options': {'reasoning_efforts': ['none', 'low', 'medium', 'high', 'xhigh', 'max']},
            }
            page.route('**/api/**', lambda route: route.fulfill(status=200, content_type='application/json', body=json.dumps(payload)))
            page.goto('http://127.0.0.1:18971/tests/provider-fixture.html')
            page.get_by_role('button', name=re.compile(r'编\s*辑')).click()
            drawer = page.get_by_role('dialog')
            field = drawer.locator('.form-field').filter(has=page.locator('label', has_text='reasoningEffort'))
            select = field.locator('.ant-select')
            select.scroll_into_view_if_needed()
            box = select.bounding_box()
            # A desktop user aims at the dropdown arrow, not the middle of the field.
            if width < 500:
                page.touchscreen.tap(box['x'] + box['width'] - 18, box['y'] + box['height'] / 2)
            else:
                page.mouse.move(box['x'] + box['width'] - 18, box['y'] + box['height'] / 2)
                page.wait_for_timeout(250)
                page.mouse.click(box['x'] + box['width'] - 18, box['y'] + box['height'] / 2)
            page.locator('.ant-select-dropdown:visible').wait_for(timeout=3000)
            page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').get_by_text('high', exact=True).click()
            assert select.inner_text().strip() == 'high', select.inner_text()
            page.locator('.ant-select-dropdown:visible').wait_for(state='hidden')
            page.get_by_label('reasoningEffort', exact=True).focus()
            page.keyboard.press('ArrowDown')
            page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').get_by_text('跟随模型默认', exact=True).click()
            assert '跟随模型默认' in select.inner_text()
            # Default is distinct from the explicit "none" effort and leaves the arrow usable.
            page.locator('.ant-select-dropdown:visible').wait_for(state='hidden')
            select.click()
            page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').get_by_text('none', exact=True).click()
            assert select.inner_text().strip() == 'none'
            page.locator('.ant-select-dropdown:visible').wait_for(state='hidden')
            output = Path('frontend/node_modules/.cache')
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / f'provider-{width}.png'))
            assert not errors, errors
            page.close()
        browser.close()
    print('PASS: desktop/laptop arrow hover+click, mobile touch, keyboard, default and explicit none')


if __name__ == '__main__':
    main()
