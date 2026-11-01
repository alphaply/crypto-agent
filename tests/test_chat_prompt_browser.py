"""Browser regressions against Vite, with all backend requests intercepted.

Run with Playwright installed and Vite serving 127.0.0.1:18971.
"""
import json
from pathlib import Path
from urllib.parse import urlsplit, parse_qs



def exercise(browser, width):
    from playwright.sync_api import expect
    context = browser.new_context(viewport={"width": width, "height": 960}, reduced_motion="reduce")
    page = context.new_page()
    page.set_default_timeout(10000)
    errors, streams, writes = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script("""
        localStorage.setItem('crypto-agent-token', 'fixture-token');
        localStorage.setItem('crypto-agent-locale', 'zh');
        localStorage.setItem('crypto-agent-admin-active-tab', 'prompts');
    """)
    sessions = [dict(session_id='a', config_id='cfg', title='会话甲', symbol='BTC/USDT'),
                dict(session_id='b', config_id='cfg', title='会话乙', symbol='ETH/USDT')]
    history = {sid: [{'role': 'user', 'content': f'问题 {sid}', 'message_index': 0},
                     {'role': 'assistant', 'content': f'已保存回答 {sid}', 'message_index': 1}] for sid in ['a', 'b']}
    history['a'].insert(0, {'role': 'assistant', 'content': '', 'message_index': 0})
    for index, row in enumerate(history['a']):
        row['message_index'] = index
    fail = {'session': False, 'prompt': True}
    prompts = {'one.txt': '请分析 {symbol}\n价格 {current_price}', 'two.txt': '第二份 Prompt'}
    def reply(route, body, status=200):
        route.fulfill(status=status, content_type='application/json', body=json.dumps(body))
    def handle(route):
        url = urlsplit(route.request.url)
        path, method = url.path, route.request.method
        if path == '/api/setup/status': return reply(route, {'required': False})
        if path == '/api/chat/bootstrap': return reply(route, {'sessions': sessions, 'configs': [{'config_id': 'cfg', 'symbol': 'BTC/USDT', 'model': 'fixture', 'mode': 'SWING'}]})
        if path.endswith('/stream'):
            streams.append(route.request.post_data_json)
            sid = path.split('/')[-2]
            history[sid] += [{'role': 'user', 'content': streams[-1]['message'], 'message_index': 2}, {'role': 'assistant', 'content': '断线前已保存的回答', 'message_index': 3}]
            # A truncated event stream has no done event. Recovery must only GET.
            return route.fulfill(content_type='text/event-stream', body='data: {"type":"token","token":"部分"}\n\n')
        if path.startswith('/api/chat/sessions/'):
            sid = path.split('/')[-1]
            if fail['session']: return reply(route, {'detail': 'history unavailable'}, 503)
            return reply(route, {'session': next(x for x in sessions if x['session_id'] == sid), 'messages': history[sid], 'pending_approval': None})
        if path == '/api/config': return reply(route, {'globals': {'secrets': {}}, 'agents': [], 'llm_providers': [], 'exchange_profiles': [], 'prompts': {'files': list(prompts)}, 'options': {}})
        if path == '/api/config/prompts/content':
            name = parse_qs(url.query)['name'][0]
            if name == 'two.txt' and fail['prompt']: return reply(route, {'detail': 'prompt unavailable'}, 503)
            return reply(route, {'content': prompts[name]})
        if path == '/api/config/prompts' and method == 'PUT':
            body = route.request.post_data_json
            writes.append(body)
            prompts[body['name']] = body['content']
        return reply(route, {'success': True})
    page.route('**/api/**', handle)
    page.goto('http://127.0.0.1:18971/console/chat')
    sender = page.locator('.x-chat-sender-wrap textarea')
    expect(page.get_by_text('已保存回答 a', exact=True)).to_be_visible()
    page.locator('.chat-message-actions button').filter(has_text='编辑并分支').click()
    expect(page.locator('.ant-modal textarea')).to_have_value('问题 a')
    page.locator('.ant-modal-close').click()
    sender.fill('甲的草稿\n第二行')
    def select(title):
        if width < 768:
            page.locator('.x-chat-main-heading button').first.click()
            page.locator('.chat-sidebar-drawer').get_by_text(title, exact=True).click()
        else:
            page.locator('.x-chat-sidebar').get_by_text(title, exact=True).click()
    select('会话乙')
    expect(sender).to_have_value('')
    sender.fill('乙的草稿')
    page.reload()
    expect(page.locator('.x-chat-main-titles h4')).to_have_text('会话乙')
    expect(sender).to_have_value('乙的草稿')
    select('会话甲')
    expect(sender).to_have_value('甲的草稿\n第二行')
    select('会话乙')
    expect(sender).to_have_value('乙的草稿')
    sender.press('Shift+Enter' if width < 768 else 'Enter')
    expect(page.get_by_text('断线前已保存的回答', exact=True)).to_be_visible()
    expect(page.get_by_text('已恢复服务器保存的聊天记录。问题不会自动重发。', exact=True)).to_be_visible()
    assert len(streams) == 1
    assert page.evaluate("localStorage.getItem('crypto-agent-chat-draft:b')") is None
    sender.fill('断线后继续写的草稿')
    fail['session'] = True
    page.reload()
    expect(page.get_by_text('会话未能加载', exact=True)).to_be_visible()
    expect(sender).to_be_disabled()
    fail['session'] = False
    page.evaluate("window.dispatchEvent(new Event('online'))")
    expect(sender).to_be_enabled()
    expect(sender).to_have_value('断线后继续写的草稿')
    assert len(streams) == 1
    Path('docs/screenshots').mkdir(exist_ok=True)
    assert page.locator('.chat-composer-hint').evaluate('(el) => el.getBoundingClientRect().bottom <= innerHeight')
    page.screenshot(path=f'docs/screenshots/chat-recovery-{width}.png')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')

    page.evaluate('''() => {
        const original = Storage.prototype.setItem;
        Storage.prototype.setItem = function(key, value) {
            if (key.startsWith('crypto-agent-chat-draft:')) throw new DOMException('full', 'QuotaExceededError');
            original.call(this, key, value);
        };
    }''')
    sender.fill('存储不可用时仍能输入')
    expect(page.locator('.chat-draft-status')).to_contain_text('草稿暂未保存')
    assert len(streams) == 1

    page.goto('http://127.0.0.1:18971/console/config')
    editor = page.get_by_role('textbox', name='Prompt 编辑器', exact=True)
    expect(editor).to_have_value(prompts['one.txt'])
    editor.fill('开头结尾')
    editor.evaluate('(el) => el.setSelectionRange(2, 2)')
    page.get_by_role('button', name='○ symbol', exact=True).click()
    expect(editor).to_have_value('开头{symbol}结尾')
    editor.press('Control+z')
    expect(editor).to_have_value('开头结尾')
    editor.fill('长段中文 ' * 80 + '\n{balance:.2f}')
    expect(page.get_by_role('button', name='✓ balance', exact=True)).to_be_visible()
    assert editor.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
    editor.press('Control+s')
    expect(page.get_by_text('Prompt 已保存', exact=True)).to_be_visible()
    assert len(writes) == 1
    page.locator('.prompt-file-row').filter(has_text='two.txt').click()
    expect(page.get_by_text('Prompt 加载失败', exact=True)).to_be_visible()
    expect(editor).to_be_disabled()
    expect(editor).to_have_value('')
    assert len(writes) == 1
    fail['prompt'] = False
    page.get_by_role('button', name='重新加载', exact=True).click()
    expect(editor).to_have_value('第二份 Prompt')
    editor.fill('第二份未保存草稿')
    page.reload()
    expect(editor).to_have_value('第二份未保存草稿')
    page.get_by_role('button', name='专注编辑', exact=True).click()
    page.screenshot(path=f'docs/screenshots/prompt-editor-{width}.png')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    assert not errors, errors
    context.close()
    print(f'PASS {width}px: session/draft recovery, broken stream, load failure, variable insertion/undo, prompt drafts')


if __name__ == '__main__':
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        try:
            for width in [1440, 390]:
                exercise(browser, width)
        finally:
            browser.close()
