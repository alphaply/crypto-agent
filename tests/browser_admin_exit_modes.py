"""Admin exit settings with mocked APIs; Vite must run on 18973. No backend required."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    modes = {
        "attached_required": "必填止盈止损",
        "attached_optional": "可选止盈止损",
        "independent_exits": "独立退出单",
    }
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        for width, height in [(1440, 1000), (390, 844)]:
            page = browser.new_page(viewport={"width": width, "height": height}, reduced_motion="reduce")
            page.set_default_timeout(7000)
            errors, submissions = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            base_agent = {"symbol": "ETH/USDT", "enabled": True, "prompt_file": "fixture.txt",
                          "llm_provider_id": "fixture-model", "mode": "STRATEGY", "run_interval": 60}
            payload = {
                "globals": {"secrets": {}}, "exchange_profiles": [], "prompts": {"files": ["fixture.txt"]},
                "agents": [
                    {**base_agent, "config_id": "legacy-real", "title": "旧实盘", "mode": "REAL"},
                    {**base_agent, "config_id": "legacy-strategy", "title": "旧模拟"},
                    {**base_agent, "config_id": "explicit-independent", "title": "独立退出任务", "exit_mode": "independent_exits"},
                ],
                "llm_providers": [{"provider_id": "fixture-model", "name": "Mock model", "model": "test-model", "secrets": {}}],
                "options": {"modes": ["REAL", "STRATEGY", "SPOT_DCA"], "prompt_files": ["fixture.txt"]},
            }

            def api(route):
                request = route.request
                if request.url.endswith("/api/config"):
                    if request.method == "PUT":
                        submissions.append(request.post_data_json)
                        payload.update(request.post_data_json)
                    response = payload
                elif "/config/prompts/content" in request.url:
                    response = {"content": "Mock prompt {symbol}"}
                else:
                    response = {}
                route.fulfill(status=200, content_type="application/json", body=json.dumps(response))

            page.route("**/api/**", api)
            page.goto("http://127.0.0.1:18973/tests/admin-exit-fixture.html")
            page.get_by_role("button", name="addAgent", exact=True).wait_for()
            drawer = page.get_by_role("dialog")

            def edit(config_id):
                rows = page.locator(".admin-mobile-card") if width < 500 else page.get_by_role("row")
                rows.filter(has_text=config_id).get_by_role("button", name="Edit", exact=True).click()
                drawer.wait_for()
                return drawer.locator(".ant-select").filter(has=page.get_by_role("combobox", name="退出管理方式", exact=True))

            for config_id, expected in [("legacy-real", "attached_optional"), ("legacy-strategy", "attached_required"), ("explicit-independent", "independent_exits")]:
                control = edit(config_id)
                assert control.inner_text().strip() == modes[expected]
                drawer.get_by_role("button", name="Close", exact=True).click()
                drawer.wait_for(state="hidden")

            page.get_by_role("button", name="addAgent", exact=True).click()
            control = drawer.locator(".ant-select").filter(has=page.get_by_role("combobox", name="退出管理方式", exact=True))
            assert control.inner_text().strip() == modes["attached_required"]
            control.click()
            options = page.locator(".ant-select-dropdown:visible .ant-select-item-option-content")
            assert options.all_text_contents() == list(modes.values())
            output = Path("frontend/node_modules/.cache")
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / f"admin-exit-modes-{width}.png"))
            page.keyboard.press("Escape")
            drawer.get_by_role("button", name="Close", exact=True).click()
            drawer.wait_for(state="hidden")

            for value, label in modes.items():
                control = edit("legacy-real")
                control.click()
                page.locator(".ant-select-dropdown:visible .ant-select-item-option-content").get_by_text(label, exact=True).click()
                assert control.inner_text().strip() == label
                with page.expect_response(lambda response: response.url.endswith("/api/config") and response.request.method == "PUT"):
                    drawer.get_by_role("button", name="save", exact=True).click()
                saved = next(agent for agent in submissions[-1]["agents"] if agent["config_id"] == "legacy-real")
                assert saved["exit_mode"] == value
                page.get_by_text("已保存", exact=True).wait_for()
            page.reload()
            assert edit("legacy-real").inner_text().strip() == modes["independent_exits"]
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "mobile overflow"
            assert not errors, errors
            page.close()
        browser.close()
    print("PASS: full AdminPage desktop/mobile; new required default; legacy REAL/STRATEGY defaults; all three exit modes persist and reload.")


if __name__ == "__main__":
    main()
