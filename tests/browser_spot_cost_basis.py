"""Holdings correction UI with mocked APIs; Vite must run on port 18973."""
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        for width, height in [(1440, 1000), (390, 844)]:
            for portfolio in (False, True):
                page = browser.new_page(viewport={"width": width, "height": height}, reduced_motion="reduce")
                page.set_default_timeout(7000)
                errors, submissions, reads = [], [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                assets = [{"symbol": symbol, "total_qty": .123457, "avg_cost": 123.4568,
                           "cost_basis_current_quantity": .123456789,
                           "cost_basis_current_avg_cost": 123.456789,
                           "market_value": 30, "total_invested": 15, "unrealized_pnl": 15,
                           "return_pct": 100, "current_price": 240}
                          for symbol in (["BTC/USDT", "ETH/USDT"] if portfolio else ["BTC/USDT"])]
                rejected = [False]
                stats_available = [True]

                def api(route):
                    path = urlparse(route.request.url).path
                    status = 200
                    if path.endswith('/spot-cost-fixture'):
                        reads.append(True)
                        stats = ({"is_portfolio": True, "by_symbol": assets} if portfolio else assets[0]) if stats_available[0] else {}
                        if portfolio and stats_available[0] == 'partial':
                            stats = {"is_portfolio": True, "by_symbol": assets[:1], "missing_symbols": ['ETH/USDT']}
                        response = {"agent": {"config_id": "spot-task", "symbol": "BTC/USDT", "mode": "SPOT_DCA",
                                              "symbols": [item["symbol"] for item in assets]},
                                    "position": {"mode": "SPOT_DCA", "dca_stats": stats}, "timeframe": "1h",
                                    "market_timeframes": ["1h"],
                                    "kline": {"symbol": "BTC/USDT", "candles": [], "volume": [], "emas": {}}}
                    elif path.endswith('/config/spot-task/spot-cost-basis'):
                        assert route.request.method == 'PATCH'
                        body = route.request.post_data_json
                        submissions.append(body)
                        if rejected[0]:
                            status, response = 409, {"detail": "成交同步失败，请稍后重试。"}
                        else:
                            asset = next(item for item in assets if item['symbol'] == body['symbol'])
                            asset.update(total_qty=body['quantity'], avg_cost=body['avg_cost'],
                                         cost_basis_current_quantity=body['quantity'],
                                         cost_basis_current_avg_cost=body['avg_cost'])
                            response = {"success": True, "cost_basis": body}
                    else:
                        response = {}
                    route.fulfill(status=status, content_type="application/json", body=json.dumps(response))

                page.route('**/api/**', api)
                base_url = 'http://127.0.0.1:18973/tests/spot-cost-basis-fixture.html'
                page.goto(base_url)
                buttons = page.get_by_role('button', name='修改持仓成本', exact=True)
                try:
                    buttons.first.wait_for()
                except Exception:
                    print('Browser errors:', errors)
                    print('Page text:', page.locator('body').inner_text()[:1200])
                    raise
                assert buttons.count() == len(assets)
                target = buttons.last
                target.click()
                dialog = page.get_by_role('dialog')
                quantity = dialog.get_by_role('spinbutton', name='当前总持有数量')
                average = dialog.get_by_role('spinbutton', name='平均成本价')
                assert quantity.input_value() == '0.123456789'
                assert average.input_value() == '123.456789'
                quantity.fill('')
                dialog.get_by_role('button', name='保存新起点', exact=True).click()
                dialog.get_by_text('请填写大于或等于 0 的有效数字。', exact=True).wait_for()
                assert not submissions
                quantity.fill('1.5')
                average.fill('0')
                dialog.get_by_role('button', name='保存新起点', exact=True).click()
                dialog.get_by_text('持有数量大于 0 时，请填写大于 0 的平均成本价。', exact=True).wait_for()
                assert not submissions
                average.fill('100')
                rejected[0] = True
                dialog.get_by_role('button', name='保存新起点', exact=True).click()
                dialog.get_by_text('成交同步失败，请稍后重试。', exact=True).wait_for()
                assert quantity.input_value() == '1.5'
                rejected[0] = False
                dialog.get_by_role('button', name='保存新起点', exact=True).click()
                dialog.wait_for(state='hidden')
                page.wait_for_timeout(200)
                assert len(reads) == 2, reads
                assert submissions[-1] == {'symbol': assets[-1]['symbol'], 'quantity': 1.5, 'avg_cost': 100}
                if portfolio:
                    assert assets[0]['total_qty'] == .123457
                target.click()
                assert quantity.input_value() == '1.5'
                assert average.input_value() == '100'
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                output = Path('frontend/node_modules/.cache')
                output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output / f'spot-cost-basis-{width}-{portfolio}.png'), full_page=True)
                dialog.screenshot(path=str(output / f'spot-cost-basis-modal-{width}-{portfolio}.png'))
                quantity.fill('0')
                average.fill('0')
                dialog.get_by_role('button', name='保存新起点', exact=True).click()
                dialog.wait_for(state='hidden')
                assert submissions[-1]['quantity'] == submissions[-1]['avg_cost'] == 0
                assets[-1].update(total_qty=None, avg_cost=None, cost_basis_current_quantity=None,
                                  cost_basis_current_avg_cost=None)
                page.reload()
                target.click()
                assert quantity.input_value() == '' and average.input_value() == ''
                dialog.get_by_role('button', name=re.compile(r'取\s*消')).click()
                if portfolio:
                    stats_available[0] = 'partial'
                    page.reload()
                    buttons.first.wait_for()
                    assert buttons.count() == len(assets)
                    buttons.first.click()
                    assert dialog.get_by_text('修改持仓成本 · ETH/USDT', exact=True).is_visible()
                    assert quantity.input_value() == '' and average.input_value() == ''
                    dialog.get_by_role('button', name=re.compile(r'取\s*消')).click()
                stats_available[0] = False
                page.reload()
                buttons.first.wait_for()
                assert buttons.count() == len(assets)
                buttons.first.click()
                assert quantity.input_value() == '' and average.input_value() == ''
                dialog.get_by_role('button', name=re.compile(r'取\s*消')).click()
                page.goto(base_url + '?auth=0')
                page.locator('.spot-account-summary').wait_for()
                assert page.get_by_role('button', name='修改持仓成本', exact=True).count() == 0
                page.goto(base_url + '?locale=en')
                page.get_by_role('button', name='Adjust holdings', exact=True).first.click()
                page.get_by_role('dialog').get_by_role('spinbutton', name='Current total quantity').wait_for()
                assert not errors, errors
                page.close()
        browser.close()
    print('PASS: desktop/mobile, single/multiple symbols, exact defaults, validation, failure/retry, save/refresh, zero holdings, unknown/partial/missing statistics, authentication, English.')


if __name__ == '__main__':
    main()
