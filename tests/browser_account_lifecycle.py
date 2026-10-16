"""UI acceptance against local Vite fixtures; no live backend is accessed."""
from pathlib import Path
from playwright.sync_api import expect, sync_playwright

OUTPUT = Path('docs/screenshots/account-lifecycle')


def receipts(browser, width, locale):
    page = browser.new_page(viewport={'width': width, 'height': 900}, reduced_motion='reduce')
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(f'http://127.0.0.1:18973/tests/receipt-fixture.html?locale={locale}')
    cards = page.locator('.tool-result-card')
    expect(cards).to_have_count(3)
    expect(cards.nth(1).locator('.tool-result-card-summary')).to_contain_text('Insufficient available cash')
    assert not page.locator('.tool-result-disclosure[open]').count()
    assert not page.locator('.tool-result-section details[open]').count()
    assert not page.get_by_text('private-order-detail-only', exact=True).is_visible()
    cards.first.locator('.tool-result-disclosure > summary').click()
    expect(cards.first.locator('.tool-result-disclosure')).to_have_attribute('open', '')
    cards.first.locator('.tool-result-disclosure > summary').click()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors, errors
    OUTPUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUTPUT / f'receipts-{width}-{locale}.png'), full_page=True)
    page.close()


if __name__ == '__main__':
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        for width, locale in ((1440, 'zh'), (1440, 'en'), (390, 'zh'), (390, 'en')):
            receipts(browser, width, locale)
        browser.close()
    print('PASS: concise cards, errors, disclosure, zh/en desktop/mobile, no viewport overflow')
