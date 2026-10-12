"""Offline React verification; start Vite on 18974. No backend or credentials."""
from pathlib import Path

from playwright.sync_api import sync_playwright, expect


def main():
    destination = Path('openspec/changes/redesign-trade-admission-recovery/evidence')
    destination.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        for label, width, height in [('desktop', 1440, 1100), ('mobile', 390, 844)]:
            page = browser.new_page(viewport={'width': width, 'height': height}, reduced_motion='reduce')
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://127.0.0.1:18974/tests/trade-recovery-fixture.html', wait_until='networkidle')
            expect(page.get_by_text('本次已成交', exact=True)).to_have_count(4)
            expect(page.locator('.decision-tool-call summary')).not_to_contain_text('待核验')
            expect(page.locator('.decision-tool-call summary')).to_contain_text('已返回')
            expect(page.get_by_text('清理待核验', exact=True)).to_be_visible()
            expect(page.get_by_text('查询已返回', exact=True)).to_be_visible()
            expect(page.get_by_text('原交易已拒绝；查询返回不代表交易成功。', exact=True)).to_be_visible()
            expect(page.get_by_text('建议待刷新；新动作会在本次调用内重新取证。', exact=True)).to_be_visible()
            expect(page.get_by_text('全面可交易', exact=True)).to_have_count(0)
            expect(page.get_by_text('algo:example-stop · fill_unverified', exact=True)).to_be_visible()
            expect(page.get_by_text('已请求 · 未返回证据', exact=True)).to_be_visible()
            for button in page.locator('.reasoning-block__trigger').all():
                button.click()
            expect(page.get_by_text('请求已携带推理设置，但服务商未返回推理文本或正数推理用量；无法确认实际执行档位。', exact=True)).to_be_visible()
            expect(page.get_by_text('本次请求已显式关闭推理。', exact=True)).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'page overflows viewport'
            assert not errors, errors
            page.screenshot(path=str(destination / f'{label}-trade-reasoning.png'), full_page=True)
            page.close()
        browser.close()
    print('desktop/mobile verification passed; synthetic screenshots saved')


if __name__ == '__main__':
    main()
