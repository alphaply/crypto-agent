"""Mocked frontend regression. Run Vite on 18973; all API requests are intercepted."""
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    rule = {"rule_id": "rule-1", "config_id": "fixture", "content": "已有规则", "enabled": True,
            "locked": False, "revision": 1, "created_by": "model", "updated_by": "model",
            "created_at": "2026-10-01 09:00:00", "updated_at": "2026-10-01 09:00:00"}
    submissions = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def api(route):
            request = route.request
            status = 200
            if "/trading-rules/rule-1/history" in request.url:
                response = {"success": True, "history": [{**rule, "actor": "human", "reason": "保留修改原因"}]}
            elif "/trading-rules" in request.url:
                if request.method == "PATCH":
                    payload = request.post_data_json
                    submissions.append(payload)
                    if payload["expected_revision"] == 1:
                        rule.update(content="模型刚更新的规则", revision=2)
                        status, response = 409, {"detail": "revision conflict"}
                    else:
                        rule.update(**{key: payload[key] for key in ("content", "enabled", "locked")}, revision=3, updated_by="human")
                        response = {"success": True, "rule": rule}
                elif request.method == "POST":
                    submissions.append(request.post_data_json)
                    response = {"success": True, "rule": {**request.post_data_json, "rule_id": "new-rule", "revision": 1}}
                else:
                    response = {"success": True, "rules": [rule]}
            else:
                response = {"positions": [], "daily_summaries": [], "short_memories": []}
            route.fulfill(status=status, content_type="application/json", body=json.dumps(response))

        page.route("**/api/**", api)
        page.goto("http://127.0.0.1:18973/tests/trading-controls-fixture.html")
        page.get_by_text("已有规则", exact=True).wait_for()
        assert page.get_by_role("button", name="adjustTpSl", exact=True).count() == 0
        assert page.get_by_text("独立退出单", exact=True).is_visible()
        assert page.get_by_text("LONG: 0.1", exact=True).is_visible()
        assert page.get_by_text("2,600", exact=True).is_visible()
        assert page.get_by_text("2,400", exact=True).is_visible()

        page.get_by_role("button", name="编辑 / 状态", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("textbox", name="规则内容", exact=True).fill("我的未保存草稿")
        dialog.get_by_role("button", name="保存规则", exact=True).click()
        dialog.get_by_text("规则已被其他操作更新。草稿已保留，请复制需要保留的内容，关闭后重新打开最新规则。", exact=True).wait_for()
        assert dialog.get_by_role("textbox", name="规则内容", exact=True).input_value() == "我的未保存草稿"
        assert dialog.get_by_role("button", name="保存规则", exact=True).is_disabled()
        dialog.get_by_role("button", name=re.compile(r"取\s*消")).click()
        page.get_by_role("button", name="编辑 / 状态", exact=True).click()
        assert dialog.get_by_role("textbox", name="规则内容", exact=True).input_value() == "模型刚更新的规则"
        dialog.get_by_role("textbox", name="规则内容", exact=True).fill("人工维护的新规则")
        dialog.get_by_role("switch").nth(1).check()
        dialog.get_by_role("button", name="保存规则", exact=True).click()
        dialog.wait_for(state="hidden")
        page.locator("#root").get_by_text("人工维护的新规则", exact=True).wait_for()
        assert submissions[-1]["expected_revision"] == 2
        assert submissions[-1]["locked"] is True

        page.get_by_role("button", name=re.compile("添加规则")).click()
        assert dialog.get_by_role("switch").nth(1).is_checked()
        dialog.get_by_role("textbox", name="规则内容", exact=True).fill("人工新增默认锁定")
        dialog.get_by_role("button", name="保存规则", exact=True).click()
        dialog.wait_for(state="hidden")
        assert submissions[-1]["locked"] is True
        assert submissions[-1]["config_id"] == "fixture"

        page.get_by_role("button", name="版本记录", exact=True).click()
        dialog.get_by_text("原因：保留修改原因", exact=True).wait_for()
        assert dialog.get_by_text("v3", exact=True).is_visible()
        dialog.get_by_role("button", name="Close", exact=True).click()
        dialog.wait_for(state="hidden")
        output = Path("frontend/node_modules/.cache")
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / "trading-controls-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "mobile overflow"
        page.screenshot(path=str(output / "trading-controls-mobile.png"), full_page=True)
        assert not errors, errors
        browser.close()
    print("PASS: independent exits; rule locks/history; stale revision preserves draft; desktop/mobile layout.")


if __name__ == "__main__":
    main()
