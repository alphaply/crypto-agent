"""Spot task editor smoke test with mocked APIs; Vite must run on port 18973."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        for width, height in [(1440, 1000), (390, 844)]:
            page = browser.new_page(viewport={"width": width, "height": height}, reduced_motion="reduce")
            page.set_default_timeout(7000)
            errors, submissions = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            payload = {
                "globals": {"secrets": {}}, "prompts": {"files": ["dca.txt"]},
                "agents": [{"config_id": "spot-portfolio", "mode": "SPOT_DCA", "symbol": "BTC/USDT",
                            "enabled": True, "prompt_file": "dca.txt", "llm_provider_id": "fixture-model",
                            "initial_qty": 1, "initial_cost": 100, "dca_amount": 100, "exchange_profile_id": "spot"}],
                "llm_providers": [{"provider_id": "fixture-model", "name": "Mock", "model": "test", "secrets": {}}],
                "exchange_profiles": [{"profile_id": "spot", "name": "Spot", "exchange": "binance", "market_type": "spot"}],
                "options": {"modes": ["REAL", "STRATEGY", "SPOT_DCA"], "prompt_files": ["dca.txt"]},
            }

            def api(route):
                request = route.request
                if "/config/market-symbols" in request.url:
                    params = parse_qs(urlparse(request.url).query)
                    available = [{"symbol": symbol, "base": symbol.split('/')[0], "quote": symbol.split('/')[1]}
                                 for symbol in ["BTC/USDT", "ETH/USDT", "SOL/USDC"]]
                    selected = params.get("symbols", [""])[0].split(',')
                    matches = [item for item in available if params.get("keyword", [""])[0].upper() in item["symbol"]
                               and (not params.get("quote") or item["quote"] == params["quote"][0])]
                    response = {"symbols": matches, "selected_symbols": [item for item in available if item["symbol"] in selected],
                                "invalid_symbols": [], "total": len(matches), "has_more": False, "quote_currencies": ["USDT", "USDC"]}
                elif request.url.endswith("/api/config"):
                    if request.method == "PUT":
                        submissions.append(request.post_data_json)
                        payload.update(request.post_data_json)
                    response = payload
                else:
                    response = {}
                route.fulfill(status=200, content_type="application/json", body=json.dumps(response))

            page.route("**/api/**", api)
            page.goto("http://127.0.0.1:18973/tests/admin-exit-fixture.html")
            rows = page.locator(".admin-mobile-card") if width < 500 else page.get_by_role("row")
            rows.filter(has_text="spot-portfolio").get_by_role("button", name="Edit", exact=True).click()
            drawer = page.get_by_role("dialog")
            symbols = drawer.get_by_role("combobox", name="现货标的", exact=True)
            symbols.fill("eth/usdt")
            page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').get_by_text('ETH/USDT', exact=True).click()
            page.keyboard.press("Escape")
            page.get_by_text('当前任务有手工初始持仓或成本，请先核对、清理并保存初始持仓设置，再添加多个标的。', exact=True).wait_for()
            selected = drawer.locator('.ant-select').filter(has=page.get_by_role('combobox', name='现货标的', exact=True)).locator('.ant-select-selection-item')
            assert selected.all_text_contents() == ['BTC/USDT']
            for label, expected in (("Initial Qty", "1"), ("Initial Cost", "100"), ("Manual Avg Cost", "100")):
                control = drawer.locator(".form-field").filter(has=page.get_by_text(label, exact=True)).get_by_role("spinbutton")
                assert control.input_value() == expected, (label, control.input_value())
            for label in ("Initial Qty", "Initial Cost", "Manual Avg Cost"):
                control = drawer.locator(".form-field").filter(has=page.get_by_text(label, exact=True)).get_by_role("spinbutton")
                control.fill('0')
                control.press('Tab')
            symbols.fill('ETH/USDT')
            page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').get_by_text('ETH/USDT', exact=True).click()
            page.keyboard.press('Escape')
            assert selected.all_text_contents() == ['BTC/USDT'], 'Clear and save costs before switching to a portfolio'
            with page.expect_response(lambda response: response.url.endswith('/api/config') and response.request.method == 'PUT'):
                drawer.get_by_role('button', name='save', exact=True).click()
            drawer.wait_for(state='hidden')
            assert submissions[-1]['agents'][0]['symbols'] == ['BTC/USDT']
            rows.filter(has_text='spot-portfolio').get_by_role('button', name='Edit', exact=True).click()
            symbols.fill('ETH/USDT')
            page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').get_by_text('ETH/USDT', exact=True).click()
            page.keyboard.press('Escape')
            for label in ("Initial Qty", "Initial Cost", "Manual Avg Cost"):
                control = drawer.locator(".form-field").filter(has=page.get_by_text(label, exact=True)).get_by_role("spinbutton")
                assert control.is_disabled()
                assert control.input_value() == "0"
            timeframes = drawer.locator(".form-field").filter(has=page.get_by_text("市场分析周期", exact=True))
            assert timeframes.locator(".ant-select-selection-item").all_text_contents() == ["4h", "1d", "1w"]
            with page.expect_response(lambda response: response.url.endswith("/api/config") and response.request.method == "PUT"):
                drawer.get_by_role("button", name="save", exact=True).click()
            saved = submissions[-1]["agents"][0]
            assert saved["symbols"] == ["BTC/USDT", "ETH/USDT"], saved
            assert saved["symbol"] == "BTC/USDT" and saved["market_type"] == "spot"
            assert saved["dca_amount"] == 100 and saved["initial_qty"] == saved["initial_cost"] == 0
            page.reload()
            rows.filter(has_text="spot-portfolio").get_by_role("button", name="Edit", exact=True).click()
            assert drawer.get_by_text("ETH/USDT", exact=True).is_visible()
            symbols = drawer.get_by_role("combobox", name="现货标的", exact=True)
            symbols.fill("SOL/USDC")
            page.get_by_text("没有匹配的可交易现货标的", exact=True).wait_for()
            symbols.press("Enter")
            page.keyboard.press("Escape")
            assert drawer.locator('.ant-select-selection-item[title="SOL/USDC"]').count() == 0
            assert drawer.is_visible()
            assert not errors, errors
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            output = Path("frontend/node_modules/.cache")
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / f"spot-portfolio-admin-{width}.png"))
            page.close()
        for width, height in [(1440, 1000), (390, 844)]:
            page = browser.new_page(viewport={"width": width, "height": height}, reduced_motion="reduce")
            page.set_default_timeout(7000)
            errors, chart_requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            agent = {"config_id": "portfolio", "symbol": "BTC/USDT", "symbols": ["BTC/USDT", "ETH/USDT"],
                     "is_portfolio": True, "mode": "SPOT_DCA", "enabled": True, "market_timeframes": ["4h", "1d", "1w"]}
            assets = [{"symbol": symbol, "base_asset": symbol.split('/')[0], "market_value": 120,
                       "total_invested": 100, "actual_balance": qty, "total_qty": qty, "avg_cost": 100 / qty,
                       "current_price": 120 / qty, "unrealized_pnl": 20, "return_pct": 20, "buy_count": 1, "pending_orders": 1}
                      for symbol, qty in [("BTC/USDT", .001), ("ETH/USDT", .01)]]

            def dashboard_api(route):
                url = urlparse(route.request.url)
                if url.path.endswith('/public/dashboard'):
                    response = {"agent_summaries": [agent], "current_symbol": "BTC/USDT", "symbols": agent["symbols"],
                                "overview_metrics": {"agent_count": 1}, "compare_candidates": [], "default_compare_ids": []}
                elif '/public/workspace/' in url.path:
                    params = parse_qs(url.query)
                    symbol = params.get('symbol', ['BTC/USDT'])[0]
                    timeframe = params.get('timeframe', ['1h'])[0]
                    chart_requests.append(symbol)
                    response = {"agent": agent, "timeframe": timeframe, "market_timeframes": agent["market_timeframes"],
                                "position": {"mode": "SPOT_DCA", "dca_stats": {"is_portfolio": True, "by_symbol": assets,
                                    "market_value": 240, "total_invested": 200, "unrealized_pnl": 40, "return_pct": 20,
                                    "buy_count": 2, "pending_orders": 2, "dca_amount_per": 100}},
                                "kline": {"symbol": symbol, "candles": [{"time": 1788710400, "open": 100, "high": 110, "low": 90, "close": 105}],
                                          "volume": [], "emas": {}, "pending_orders": []}}
                else:
                    response = {}
                route.fulfill(status=200, content_type="application/json", body=json.dumps(response))

            page.route("**/api/**", dashboard_api)
            page.goto("http://127.0.0.1:18973/tests/spot-portfolio-fixture.html")
            page.locator('.spot-account-summary .ant-card-head-title').filter(has_text='ETH/USDT').wait_for()
            assert page.locator('.spot-account-summary .ant-card-head-title').all_text_contents() == ['BTC/USDT', 'ETH/USDT']
            page.get_by_role('combobox', name='图表标的', exact=True).click()
            page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').get_by_text('ETH/USDT', exact=True).click()
            page.wait_for_function("document.querySelector('.market-chart-heading').textContent.includes('ETH/USDT')")
            assert 'ETH/USDT' in chart_requests, chart_requests
            assert not errors, errors
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.screenshot(path=f'frontend/node_modules/.cache/spot-portfolio-dashboard-{width}.png', full_page=True)
            page.close()
        browser.close()
    print("PASS: desktop/mobile spot editor save/reload and validation; portfolio stats and chart symbol selection with API symbol propagation.")


if __name__ == "__main__":
    main()
