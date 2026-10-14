"""Real drawer interactions, mocked network only. Start Vite on 127.0.0.1:18971."""
import copy
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect


OUTPUT = Path('docs/screenshots')


def exercise(browser, width, locale):
    zh = locale == 'zh'
    page = browser.new_page(viewport={'width': width, 'height': 1000 if width > 500 else 844},
                            is_mobile=width < 500, has_touch=width < 500, reduced_motion='reduce')
    page.set_default_timeout(8000)
    errors, discoveries, writes, held, api_calls = [], [], [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    provider = {'provider_id': 'fixture', 'name': 'Fixture supplier', 'model': 'private-alias',
                'api_base': 'https://fixture.invalid/v1', 'api_protocol': 'chat', 'compatibility_mode': 'openai',
                'thinking_enabled': True, 'reasoning_effort': 'medium', 'temperature': .7, 'extra_body': {'fixture': True},
                'pricing_mode': 'manual', 'input_price_per_m': 1.25, 'output_price_per_m': 2.5,
                'secrets': {'api_key': {'configured': True, 'value': 'saved-fixture-key'}}}
    payload = {'globals': {'secrets': {}}, 'agents': [], 'exchange_profiles': [], 'prompts': {'files': []},
               'llm_providers': [copy.deepcopy(provider)], 'options': {'reasoning_efforts': ['none', 'low', 'medium', 'high']}}
    mode = {'value': 'ok'}

    def reply(route, data, status=200):
        route.fulfill(status=status, content_type='application/json', body=json.dumps(data))

    def catalog(body, model_id='Exact/Model'):
        return {'success': True, 'models': [{'id': model_id, 'name': 'Friendly Catalog Model'}],
                'complete': True, 'warning': None, 'fetched_at': '2026-10-13T04:00:00Z',
                'source': {'api_base': body['api_base'], 'url': body['api_base'] + '/models', 'protocol': 'openai'}}

    def handle(route):
        path = urlsplit(route.request.url).path
        method = route.request.method
        api_calls.append((method, path))
        if path == '/api/config/provider-models/discover':
            body = route.request.post_data_json
            discoveries.append(body)
            if mode['value'] == 'hold':
                held.append((route, body))
            elif mode['value'] == 'error':
                reply(route, {'detail': {'code': 'credentials_rejected', 'retryable': False}}, 502)
            elif mode['value'] == 'partial':
                reply(route, {**catalog(body), 'complete': False, 'warning': {'code': 'pagination_error'}})
            elif mode['value'] == 'empty':
                reply(route, {**catalog(body), 'models': []})
            else:
                reply(route, catalog(body, 'jev-latest' if body['api_protocol'] == 'decisions' else 'Exact/Model'))
        elif path == '/api/config' and method == 'PUT':
            writes.append(route.request.post_data_json)
            reply(route, {'success': True})
        else:
            reply(route, payload)

    page.route('**/api/**', handle)
    page.goto(f'http://127.0.0.1:18971/tests/provider-fixture.html?locale={locale}')
    page.get_by_role('button', name=re.compile(r'编\s*辑') if zh else 'Edit', exact=not zh).click()
    drawer = page.get_by_role('dialog')
    model = drawer.get_by_role('combobox', name='Model', exact=True)
    section = drawer.get_by_test_id('provider-model-discovery')
    refresh = drawer.get_by_role('button', name='刷新模型' if zh else 'Refresh models')
    expect(section.get_by_role('status')).to_contain_text('获取时间' if zh else 'Fetched')
    assert len(discoveries) == 1 and discoveries[0]['credential'] == {'mode': 'saved', 'provider_id': 'fixture'}
    expect(model).to_have_value('private-alias')
    assert drawer.get_by_test_id('provider-model-discovery').count() == 1

    # Name search, keyboard choice and exact case-sensitive ID round trip.
    section.evaluate("element => element.scrollIntoView({block: 'center'})")
    model.fill('friendly')
    page.locator('.ant-select-dropdown:visible').get_by_text('Friendly Catalog Model', exact=True).wait_for()
    if zh:
        OUTPUT.mkdir(exist_ok=True, parents=True)
        page.screenshot(path=str(OUTPUT / f'provider-discovery-options-{width}.png'))
    model.press('ArrowDown')
    model.press('Enter')
    expect(model).to_have_value('Exact/Model')
    model.fill('my-unlisted-alias')
    model.press('Tab')
    assert len(discoveries) == 1

    # A failed refresh keeps the draft and dated old list; error is local.
    mode['value'] = 'error'
    refresh.click()
    expect(section).to_contain_text('已过期' if zh else 'stale')
    expect(section).to_contain_text('供应商拒绝' if zh else 'supplier rejected')
    expect(model).to_have_value('my-unlisted-alias')
    assert not writes
    mode['value'] = 'partial'
    refresh.click()
    expect(section).to_contain_text('部分模型列表' if zh else 'Partial model list')
    mode['value'] = 'empty'
    refresh.click()
    expect(section).to_contain_text('未返回可用模型' if zh else 'No models returned')

    # Prefilled secrets cannot silently travel to a changed endpoint.
    base = drawer.get_by_label('API Base *', exact=True)
    key = drawer.get_by_label('API Key', exact=True)
    base.fill('https://new-fixture.invalid/Custom/v1')
    base.press('Tab')
    previous = len(discoveries)
    page.wait_for_timeout(450)
    assert len(discoveries) == previous
    expect(refresh).to_be_disabled()
    expect(key).to_have_value('saved-fixture-key')
    key.fill('new-fixture-key')
    page.wait_for_timeout(400)
    assert len(discoveries) == previous  # Typing alone does not send.
    mode['value'] = 'hold'
    key.press('Tab')
    page.wait_for_timeout(450)
    assert len(held) == 1 and discoveries[-1]['credential']['api_key'] == 'new-fixture-key'

    # A slow A is cancelled by a new key; late completion cannot overwrite B.
    key.fill('newer-fixture-key')
    mode['value'] = 'ok'
    key.press('Tab')
    expect(section.get_by_role('status')).to_contain_text('获取时间' if zh else 'Fetched')
    held_route, held_body = held.pop()
    reply(held_route, catalog(held_body, 'obsolete-model'))
    page.wait_for_timeout(150)
    model.fill('obsolete')
    assert page.locator('.ant-select-dropdown:visible').get_by_text('obsolete-model', exact=True).count() == 0
    model.fill('manual-after-discovery')
    model.press('Tab')
    section.scroll_into_view_if_needed()
    OUTPUT.mkdir(exist_ok=True, parents=True)
    page.screenshot(path=str(OUTPUT / f'provider-discovery-{width}-{locale}.png'))
    assert not writes
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')

    # Only Save enters the pre-existing autosave contract, with other settings intact.
    drawer.get_by_role('button', name='save', exact=True).click()
    page.wait_for_timeout(1100)
    assert len(writes) == 1
    saved = writes[0]['llm_providers'][0]
    assert saved['model'] == 'manual-after-discovery'
    for field in ['api_protocol', 'compatibility_mode', 'thinking_enabled', 'reasoning_effort', 'temperature',
                  'extra_body', 'pricing_mode', 'input_price_per_m', 'output_price_per_m']:
        assert saved[field] == provider[field], field

    # A copied channel has neither a borrowed secret nor an automatic request.
    previous = len(discoveries)
    page.locator('.provider-group-heading').click()
    page.get_by_role('button', name=re.compile(r'复\s*制') if zh else 'Copy', exact=not zh).click()
    expect(drawer.get_by_label('API Key', exact=True)).to_have_value('')
    expect(drawer.get_by_role('button', name='刷新模型' if zh else 'Refresh models')).to_be_disabled()
    page.wait_for_timeout(400)
    assert len(discoveries) == previous
    drawer.get_by_role('button', name='Close', exact=True).click()

    # A brand-new ordinary draft discovers while its model is still blank.
    page.get_by_role('button', name='新建服务端点 / 模型' if zh else 'New endpoint / model').click()
    base.fill('https://first-time.invalid/v1')
    key.fill('first-time-draft-key')
    model.focus()
    expect(section.get_by_role('status')).to_contain_text('获取时间' if zh else 'Fetched')
    expect(model).to_have_value('')
    assert discoveries[-1]['credential'] == {'mode': 'draft', 'api_key': 'first-time-draft-key'}
    assert discoveries[-1]['api_protocol'] == 'chat' and len(writes) == 1
    drawer.get_by_role('button', name='Close', exact=True).click()

    # First-time Jev has no placeholder model requirement and one shared selector.
    page.get_by_role('button', name='新建服务端点 / 模型' if zh else 'New endpoint / model').click()
    drawer.get_by_role('button', name='TypeSafe · Jev 官方', exact=True).click()
    model.fill('')
    drawer.get_by_label('API Key', exact=True).fill('jev-draft-key')
    model.focus()
    expect(section.get_by_role('status')).to_contain_text('获取时间' if zh else 'Fetched')
    expect(model).to_have_value('')
    assert discoveries[-1]['api_protocol'] == 'decisions' and discoveries[-1]['decisions_api'] == 'typesafe'
    assert 'model' not in discoveries[-1]
    assert drawer.get_by_test_id('provider-model-discovery').count() == 1
    assert len(writes) == 1
    model.press('Escape')
    section.scroll_into_view_if_needed()
    page.screenshot(path=str(OUTPUT / f'provider-discovery-jev-{width}-{locale}.png'))
    # Closing a pending request leaves the page stable.
    mode['value'] = 'hold'
    refresh.click()
    page.wait_for_timeout(100)
    drawer.get_by_role('button', name='Close', exact=True).click()
    for route, body in held:
        reply(route, catalog(body, 'after-close'))
    page.wait_for_timeout(100)
    assert not errors, errors
    assert all(method == 'GET' or path in {'/api/config/provider-models/discover', '/api/config'} for method, path in api_calls)
    print(f'PASS: discovery drawer {width}px {locale}; {len(discoveries)} discoveries; 1 explicit-save PUT; no generation')
    page.close()


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        for width, locale in [(1440, 'zh'), (390, 'zh'), (1440, 'en')]:
            exercise(browser, width, locale)
        browser.close()


if __name__ == '__main__':
    main()
