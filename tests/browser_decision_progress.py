"""Mocked decision loop UI checks; run Vite on 18973. Never calls trading APIs."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    messages = [
        {'role': 'assistant', 'content': '检查已有订单', 'reasoning_tokens': 123,
         'tool_calls': [{'id': 'call-one', 'name': 'cancel_orders_real', 'args': {'order_id': 'order-one', 'reason': '已失效'}}]},
        {'role': 'tool', 'tool': 'cancel_orders_real', 'tool_call_id': 'call-one', 'status': 'completed', 'result': '{"status":"completed","order_id":"order-one"}'},
        {'role': 'assistant', 'content': '等待核验', 'reasoning_content': '服务商返回的推理摘要', 'tool_calls': [
            {'id': 'call-two', 'name': 'close_position_real', 'args': {'orders': [{'amount': 0.5, 'pos_side': 'LONG', 'exit_type': 'market'}]}},
        ]},
    ]
    agent = {'config_id': 'test', 'symbol': 'ETH/USDT', 'mode': 'REAL', 'model': 'test', 'enabled': True,
             'exit_mode': 'independent_exits', 'market_timeframes': ['1h'],
             'execution': {'status': 'RUNNING', 'phase': 'tool_running', 'progress_message': '正在执行工具 close_position_real',
                           'tool_calls': [{'id': 'call-two', 'name': 'close_position_real', 'status': 'running'}],
                           'decision': {'messages': messages}}}
    dashboard = {'agent_summaries': [agent], 'symbols': ['ETH/USDT'], 'current_symbol': 'ETH/USDT'}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        for width, height in [(1440, 1100), (390, 844)]:
            page = browser.new_page(viewport={'width': width, 'height': height}, reduced_motion='reduce')
            page.set_default_timeout(8000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def api(route):
                url = route.request.url
                data = dashboard if '/public/dashboard' in url else (
                    {'agent': agent, 'position': {}, 'kline': {}, 'orders': {}, 'short_memories': {}}
                    if '/public/workspace/' in url else {})
                route.fulfill(status=200, content_type='application/json', body=json.dumps(data))

            page.route('**/api/**', api)
            page.add_init_script("localStorage.setItem('crypto-agent-locale','zh'); localStorage.setItem('crypto-agent-theme','light');")
            page.goto('http://127.0.0.1:18973/agents')
            panel = page.locator('.task-execution-panel')
            panel.get_by_text('第 2 轮 · 模型输出', exact=True).wait_for()
            panel.get_by_role('button', name='推理过程 123 tokens').click()
            panel.get_by_text('服务商仅返回推理 token 用量，未提供可展示的推理文本。', exact=True).wait_for()
            panel.locator('.decision-tool-call summary').first.click()
            assert 'order-one' in panel.locator('.decision-tool-call').first.inner_text()
            panel.get_by_text('工具回执', exact=True).click()
            assert 'order-one' in panel.locator('.decision-tool-result pre').inner_text()
            assert '执行中' in panel.locator('.decision-tool-call').last.inner_text()
            page.reload()
            panel.get_by_text('第 1 轮 · 模型输出', exact=True).wait_for()
            panel.get_by_text('第 2 轮 · 模型输出', exact=True).wait_for()
            panel.locator('.reasoning-block__trigger').first.click()
            panel.locator('.reasoning-block__trigger').last.click()
            panel.get_by_text('服务商返回的推理摘要', exact=True).wait_for()
            output = Path('frontend/node_modules/.cache')
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / f'decision-progress-{width}.png'), full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'horizontal overflow'
            assert not errors, errors
            page.close()
        browser.close()


if __name__ == '__main__':
    main()
