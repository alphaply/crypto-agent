"""Exchange-backed spot editor regressions; run Vite on 127.0.0.1:18973 first."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Error, expect, sync_playwright


def market(symbol):
    base, quote = symbol.split('/')
    return {"symbol": symbol, "base": base, "quote": quote, "market_type": "spot"}


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        for width, height in [(1440, 1000), (390, 844)]:
            page = browser.new_page(viewport={"width": width, "height": height}, reduced_motion="reduce")
            page.set_default_timeout(7000)
            errors, requests, submissions, held_search, held_put = [], [], [], [], []
            flags = {"catalog_error": False, "put_error": False, "hold_put": False}
            page.on("pageerror", lambda error: errors.append(str(error)))
            payload = {
                "globals": {"secrets": {}}, "prompts": {"files": ["dca.txt"]},
                "agents": [{"config_id": "spot-picker", "mode": "SPOT_DCA", "symbol": "BTC/USDT",
                            "symbols": ["BTC/USDT", "ZZZ/USDT"], "exchange_profile_id": "spot",
                            "enabled": True, "prompt_file": "dca.txt", "llm_provider_id": "model", "dca_amount": 100}],
                "llm_providers": [{"provider_id": "model", "name": "Mock", "model": "test", "secrets": {}}],
                "exchange_profiles": [
                    {"profile_id": "spot", "name": "Binance Spot", "exchange": "binance", "market_type": "spot"},
                    {"profile_id": "okx", "name": "OKX Spot", "exchange": "okx", "market_type": "spot"},
                    {"profile_id": "swap", "name": "Swap Only", "exchange": "binance", "market_type": "swap"}],
                "options": {"modes": ["REAL", "STRATEGY", "SPOT_DCA"], "prompt_files": ["dca.txt"]},
            }

            def fulfill(route, data, status=200):
                route.fulfill(status=status, content_type="application/json", body=json.dumps(data))

            def api(route):
                request = route.request
                url = urlparse(request.url)
                if url.path.endswith('/config/market-symbols'):
                    params = {k: v[0] for k, v in parse_qs(url.query).items()}
                    requests.append(params)
                    if flags['catalog_error']:
                        fulfill(route, {"detail": "Fixture exchange outage"}, 503)
                        return
                    symbols = (["BTC/USDT", "ETH/USDT", "SOL/USDT", "BTC/USDC"]
                               + [f"TOKEN{i:02}/USDT" for i in range(60)] + ["ZZZ/USDT"])
                    if params.get('exchange') == 'okx':
                        symbols = ['OKB/USDT', 'BTC/USDT']
                    selected = params.get('symbols', '').split(',') if params.get('symbols') else []
                    filtered = [symbol for symbol in symbols if params.get('keyword', '').upper() in symbol
                                and (not params.get('quote') or symbol.endswith('/' + params['quote']))]
                    offset, limit = int(params.get('offset', 0)), int(params.get('limit', 50))
                    response = {"symbols": [market(s) for s in filtered[offset:offset + limit]],
                                "selected_symbols": [market(s) for s in selected if s in symbols],
                                "invalid_symbols": [s for s in selected if s not in symbols],
                                "quote_currencies": ['USDT', 'USDC'], "total": len(filtered),
                                "has_more": offset + limit < len(filtered)}
                    if params.get('keyword') == 'OLD':
                        response['symbols'] = [market('OLD/USDT')]
                        held_search.append((route, response))
                        return
                    fulfill(route, response)
                elif url.path.endswith('/config'):
                    if request.method == 'PUT':
                        submissions.append(request.post_data_json)
                        if flags['put_error']:
                            fulfill(route, {"detail": "Fixture rejected task change"}, 422)
                            return
                        if flags['hold_put']:
                            held_put.append(route)
                            return
                        payload.update(request.post_data_json)
                    fulfill(route, payload)
                else:
                    fulfill(route, {})

            page.route('**/api/**', api)
            page.goto('http://127.0.0.1:18973/tests/admin-exit-fixture.html')
            rows = page.locator('.admin-mobile-card') if width < 500 else page.get_by_role('row')
            drawer = page.get_by_role('dialog')

            def edit():
                rows.filter(has_text='spot-picker').get_by_role('button', name='Edit', exact=True).first.click()
                drawer.wait_for()
                return drawer.get_by_role('combobox', name='现货标的', exact=True)

            def tags():
                return drawer.locator('.ant-select').filter(has=page.get_by_role('combobox', name='现货标的', exact=True)).locator('.ant-select-selection-item')

            def choose(symbol):
                control = drawer.get_by_role('combobox', name='现货标的', exact=True)
                control.fill(symbol)
                page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').get_by_text(symbol, exact=True).click()
                page.keyboard.press('Escape')

            symbols = edit()
            drawer.get_by_text('第 1 页，共 64 个标的', exact=True).wait_for()
            assert tags().all_text_contents() == ['BTC/USDT', 'ZZZ/USDT'], tags().all_text_contents()
            drawer.get_by_role('button', name='下一页', exact=True).click()
            drawer.get_by_text('第 2 页，共 64 个标的', exact=True).wait_for()
            assert tags().all_text_contents() == ['BTC/USDT', 'ZZZ/USDT']

            # A slow earlier search must never replace the latest results.
            with page.expect_request(lambda req: 'keyword=OLD' in req.url):
                symbols.fill('OLD')
            with page.expect_response(lambda res: 'keyword=SOL' in res.url):
                symbols.fill('SOL')
            page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').get_by_text('SOL/USDT', exact=True).wait_for()
            for route, response in held_search:
                try:
                    fulfill(route, response)
                except Error:
                    pass  # AbortController may already have canceled it.
            assert page.locator('.ant-select-dropdown:visible').get_by_text('OLD/USDT', exact=True).count() == 0
            choose('ETH/USDT')
            assert tags().all_text_contents() == ['BTC/USDT', 'ZZZ/USDT', 'ETH/USDT']

            # API filtering and multiple mode prevent arbitrary/mixed-quote tags.
            symbols.fill('BTC/USDC')
            page.get_by_text('没有匹配的可交易现货标的', exact=True).wait_for()
            symbols.press('Enter')
            assert tags().all_text_contents() == ['BTC/USDT', 'ZZZ/USDT', 'ETH/USDT']

            flags['catalog_error'] = True
            symbols.fill('DOWN')
            drawer.get_by_text('无法加载交易所标的，已有选择已保留', exact=True).wait_for()
            assert tags().all_text_contents() == ['BTC/USDT', 'ZZZ/USDT', 'ETH/USDT']
            flags['catalog_error'] = False
            with page.expect_response(lambda res: '/market-symbols' in res.url):
                drawer.get_by_role('button', name='重试', exact=True).click()
            expect(drawer.get_by_text('无法加载交易所标的，已有选择已保留', exact=True)).to_have_count(0)
            drawer.locator('.ant-drawer-title').click()

            # Failed writes keep the editor draft open; successful retry closes it.
            flags['put_error'] = True
            with page.expect_response(lambda res: res.request.method == 'PUT'):
                drawer.get_by_role('button', name='save', exact=True).click()
            page.get_by_text('任务保存失败，修改已保留，请检查错误后重试。', exact=True).wait_for()
            expect(drawer).to_be_visible()
            assert tags().all_text_contents() == ['BTC/USDT', 'ZZZ/USDT', 'ETH/USDT']
            flags['put_error'] = False
            with page.expect_response(lambda res: res.request.method == 'PUT'):
                drawer.get_by_role('button', name='save', exact=True).click()
            expect(drawer).to_be_hidden()
            assert submissions[-1]['agents'][0]['symbols'] == ['BTC/USDT', 'ZZZ/USDT', 'ETH/USDT']

            # Unchanged targets can be renamed/disabled during an exchange
            # outage, while enabling trading again requires catalog validation.
            flags['catalog_error'] = True
            edit()
            drawer.get_by_text('无法加载交易所标的，已有选择已保留', exact=True).wait_for()
            drawer.locator('.form-field').filter(has=page.get_by_text('Title', exact=True)).get_by_role('textbox').fill('Offline title edit')
            drawer.get_by_role('switch').click()
            with page.expect_response(lambda res: res.request.method == 'PUT'):
                drawer.get_by_role('button', name='save', exact=True).click()
            expect(drawer).to_be_hidden()
            assert submissions[-1]['agents'][0]['title'] == 'Offline title edit'
            assert submissions[-1]['agents'][0]['enabled'] is False
            edit()
            drawer.get_by_role('switch').click()
            before = len(submissions)
            with page.expect_response(lambda res: '/market-symbols' in res.url and 'limit=1' in res.url):
                drawer.get_by_role('button', name='save', exact=True).click()
            expect(drawer.get_by_role('switch')).to_be_enabled()
            assert len(submissions) == before
            flags['catalog_error'] = False
            with page.expect_response(lambda res: res.request.method == 'PUT'):
                drawer.get_by_role('button', name='save', exact=True).click()
            expect(drawer).to_be_hidden()

            # Delisted legacy selections stay visible and removable; changing
            # the target set requires removing the delisted selection first.
            payload['agents'][0]['symbols'] = ['BTC/USDT', 'DELISTED/USDT']
            page.reload()
            symbols = edit()
            drawer.get_by_text('以下标的不可交易，请移除后重新选择：DELISTED/USDT', exact=True).wait_for()
            choose('ETH/USDT')
            before = len(submissions)
            drawer.get_by_role('button', name='save', exact=True).click()
            page.get_by_text('存在不可交易的标的，请重新选择：DELISTED/USDT', exact=True).wait_for()
            assert len(submissions) == before
            tags().filter(has_text='DELISTED/USDT').locator('.ant-select-selection-item-remove').click()
            tags().filter(has_text='ETH/USDT').locator('.ant-select-selection-item-remove').click()
            assert tags().all_text_contents() == ['BTC/USDT']

            for index in range(9):
                choose(f'TOKEN{index:02}/USDT')
            assert tags().count() == 10
            symbols.fill('TOKEN09/USDT')
            page.locator('.ant-select-dropdown:visible .ant-select-item-option-disabled').get_by_text('TOKEN09/USDT', exact=True).wait_for()
            symbols.press('Enter')
            assert tags().count() == 10
            drawer.locator('.ant-drawer-title').click()

            # Profiles restrict the catalog; switching accounts clears old targets.
            profile = drawer.get_by_role('combobox', name='现货交易所配置', exact=True)
            profile.click()
            assert page.locator('.ant-select-dropdown:visible').get_by_text('Swap Only', exact=False).count() == 0
            page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').get_by_text('OKX Spot (okx/spot)', exact=True).click()
            expect(tags()).to_have_count(0)
            choose('OKB/USDT')
            assert any(req.get('exchange_profile_id') == 'okx' and req.get('exchange') == 'okx' for req in requests)
            profile.click()
            page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').get_by_text('noProfile', exact=True).click()
            expect(symbols).to_be_disabled()
            drawer.get_by_text('请先选择现货交易所配置，再从接口选择标的。', exact=True).wait_for()
            drawer.get_by_role('button', name='Close', exact=True).click()
            expect(drawer).to_be_hidden()
            payload['agents'][0]['symbols'] = ['BTC/USDT']
            page.reload()

            # Slow autosave followed by another edit must persist the newest copy.
            flags['hold_put'] = True
            before = len(submissions)
            with page.expect_request(lambda req: req.method == 'PUT'):
                rows.filter(has_text='spot-picker').get_by_role('button', name='Copy', exact=True).first.click()
            rows.filter(has_text='spot-picker').get_by_role('button', name='Copy', exact=True).first.click()
            page.wait_for_timeout(1000)  # Allow the second autosave debounce to elapse.
            assert len(submissions) == before + 1, 'writes must be serialized'
            flags['hold_put'] = False
            for route in held_put:
                payload.update(route.request.post_data_json)
                fulfill(route, payload)
            page.get_by_text('已保存', exact=True).wait_for()
            assert len(submissions[-1]['agents']) == 3, submissions

            # A profile edited locally may not exist with its new exchange on
            # the server yet. Look up the current exchange, not the old account.
            def tab(label):
                if width < 500:
                    page.get_by_role('combobox', name='配置分区', exact=True).click()
                    page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').get_by_text(label, exact=True).click()
                else:
                    page.get_by_role('tab', name=label, exact=True).click()

            tab('exchangeProfiles')
            page.get_by_role('row').filter(has_text='Binance Spot').get_by_role('button', name='Edit', exact=True).click()
            drawer.locator('.form-field').filter(has=page.get_by_text('Exchange', exact=True)).get_by_role('combobox').click()
            page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').get_by_text('okx', exact=True).click()
            flags['hold_put'] = True
            held_put.clear()
            with page.expect_request(lambda req: req.method == 'PUT'):
                drawer.get_by_role('button', name='save', exact=True).click()
            tab('任务配置')
            request_start = len(requests)
            with page.expect_request(lambda req: '/market-symbols' in req.url):
                edit()
            assert any(req.get('exchange') == 'okx' and 'exchange_profile_id' not in req for req in requests[request_start:])
            flags['hold_put'] = False
            for route in held_put:
                payload.update(route.request.post_data_json)
                fulfill(route, payload)
            drawer.get_by_role('button', name='Close', exact=True).click()

            # Older single-symbol tasks can keep their inline exchange account.
            legacy = payload['agents'][0]
            legacy.pop('symbols', None)
            legacy.pop('exchange_profile_id', None)
            legacy['exchange'] = 'binance'
            page.reload()
            edit()
            drawer.get_by_text('使用原任务的 binance 账户配置。选择新的账户后需重新选择标的。', exact=True).wait_for()
            assert tags().all_text_contents() == ['BTC/USDT']
            choose('ETH/USDT')
            with page.expect_response(lambda res: res.request.method == 'PUT'):
                drawer.get_by_role('button', name='save', exact=True).click()
            expect(drawer).to_be_hidden()
            assert submissions[-1]['agents'][0]['symbols'] == ['BTC/USDT', 'ETH/USDT']
            assert not errors, errors
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'mobile overflow'
            output = Path('frontend/node_modules/.cache')
            output.mkdir(parents=True, exist_ok=True)
            edit()
            page.screenshot(path=str(output / f'spot-symbol-picker-{width}.png'))
            page.close()
        browser.close()
    print('PASS: desktop/mobile API markets, pagination, stale search, invalid/mixed pairs, max 10, outage recovery, offline edits, re-enable validation, save retry, saved/unsaved profiles and autosave ordering.')


if __name__ == '__main__':
    main()
