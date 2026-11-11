"""Actual task workbench with synthetic responses; no production access."""
import json
import re
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.tmp/position-workbench-ui'
DATA = json.loads((ROOT / 'frontend/tests/position-workbench-data.json').read_text(encoding='utf-8'))
URL = 'http://127.0.0.1:18979/tests/position-workbench-fixture.html'


def install_routes(page, state):
    def route(request):
        parsed = urlparse(request.request.url)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        path = parsed.path
        state.setdefault('requests', []).append(path)
        if path.endswith('/history/short-memories') and request.request.method == 'PUT':
            state['saved_memory'] = request.request.post_data_json
            data = {'success': True}
        elif path.endswith('/public/dashboard'):
            data = DATA['dashboard']
        elif '/public/workspace/' in path:
            section = query.get('section', 'context')
            data = deepcopy(DATA[section])
            if section == 'context':
                selected = path.split('/')[-1]
                data['agent'] = next(a for a in DATA['dashboard']['agent_summaries'] if a['config_id'] == selected)
        elif '/stats/task-history/' in path:
            data = DATA['statistics']
        elif path.endswith('/history/positions'):
            if state.get('history_fail'):
                request.fulfill(status=503, json={'detail':'Synthetic history unavailable'})
                return
            items = [] if state.get('empty') else [r for r in DATA['positions'] if (not query.get('symbol') or r['symbol']==query['symbol']) and (not query.get('status') or r['status']==query['status'])]
            current = int(query.get('page', 1))
            data = dict(items=items[(current-1)*20:current*20], total=len(items), symbols=DATA['dashboard']['agent_summaries'][0]['symbols'],
                        complete=False, errors={}, as_of='2026-11-11T09:30:00+08:00')
        elif '/history/positions/' in path:
            row = next(r for r in DATA['positions'] if r['position_id']==path.split('/')[-1])
            timeline = [{**event, 'event_id':f"{event['event_id']}-{index}"} for index,event in enumerate(DATA['events']*10)] if state.get('long') else DATA['events']
            data = dict(position={**row, 'events': timeline})
        elif path.endswith('/history/trade-calls'):
            data = DATA['calls']
        else:
            data = {}
        request.fulfill(json=data)
    page.route('**/api/**', route)


def no_overflow(page):
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), page.url


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        try:
            for width in (390, 768, 1440):
                for locale in ('zh', 'en'):
                    for theme in ('light', 'dark'):
                        page = browser.new_page(viewport={'width':width,'height':1000}, timezone_id='Asia/Singapore')
                        errors, state = [], {}
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        install_routes(page, state)
                        page.goto(f'{URL}?locale={locale}&theme={theme}&agent=spot', wait_until='networkidle')
                        expect(page.locator('.allocation-row')).to_have_count(3)
                        no_overflow(page)
                        page.screenshot(path=str(OUT/f'overview-{width}-{locale}-{theme}.png'), full_page=True)
                        for view, label in [('decisions','决策与操作' if locale=='zh' else 'Decisions'),('memory','策略与记忆' if locale=='zh' else 'Strategy & memory'),('history','历史记录' if locale=='zh' else 'History')]:
                            page.get_by_role('tab',name=label,exact=True).click()
                            if view=='history':
                                expect(page.locator('.position-history-row')).to_have_count(20)
                                page.get_by_role('button',name='查看持仓过程' if locale=='zh' else 'View timeline',exact=True).first.click()
                                expect(page.locator('.position-timeline li')).to_have_count(3)
                            no_overflow(page)
                            page.screenshot(path=str(OUT/f'{view}-{width}-{locale}-{theme}.png'),full_page=True)
                            if view == 'history':
                                page.screenshot(path=str(OUT/f'history-top-{width}-{locale}-{theme}.png'))
                        assert not errors, errors
                        page.close()
            page = browser.new_page(viewport={'width':390,'height':950})
            state, errors = {}, []
            page.on('pageerror', lambda error: errors.append(str(error)))
            install_routes(page,state)
            page.goto(URL+'?agent=spot',wait_until='networkidle')
            page.get_by_text('全部历史',exact=True).click()
            assert '520.30' in page.locator('.spot-metric-grid').inner_text()
            # Cost calibration remains reachable from each mobile holding.
            page.locator('.spot-holding-detail button').first.click()
            expect(page.locator('.ant-modal:visible')).to_be_visible()
            page.locator('.ant-modal:visible .ant-modal-close').click()
            page.get_by_role('tab',name='策略与记忆',exact=True).click()
            page.get_by_role('button',name='编辑短期记忆',exact=True).click()
            page.locator('.ant-modal:visible textarea').fill('合成测试：保留记忆编辑与保存入口。')
            page.locator('.ant-modal:visible').get_by_role('button',name=re.compile(r'保\s*存')).click()
            expect(page.locator('.ant-modal:visible')).to_have_count(0)
            assert state['saved_memory']['market_summary'] == '合成测试：保留记忆编辑与保存入口。'
            page.get_by_role('tab',name='历史记录',exact=True).click()
            expect(page.locator('.position-history-row')).to_have_count(20)
            page.locator('.position-history .ant-pagination-next').click()
            expect(page.locator('.position-history-row')).to_have_count(3)
            page.get_by_role('combobox',name='持仓状态',exact=True).click()
            page.locator('.ant-select-item-option-content').filter(has_text='持仓中').click()
            expect(page.locator('.position-history-row')).to_have_count(2)
            # Refresh errors preserve known records.
            state['history_fail'] = True
            page.get_by_role('button',name='刷新持仓历史',exact=True).click()
            expect(page.get_by_text('持仓历史更新失败',exact=True)).to_be_visible()
            expect(page.locator('.position-history-row')).to_have_count(2)
            state['history_fail'] = False
            state['empty'] = True
            page.get_by_role('button',name='刷新持仓历史',exact=True).click()
            expect(page.get_by_text('此范围暂无已记录持仓',exact=True)).to_be_visible()
            state['empty'] = False
            state['long'] = True
            page.get_by_role('button',name='刷新持仓历史',exact=True).click()
            page.get_by_role('button',name='查看持仓过程',exact=True).first.click()
            expect(page.locator('.position-timeline li')).to_have_count(30)
            no_overflow(page)
            assert not errors,errors
            page.close()
            page=browser.new_page(viewport={'width':1440,'height':1000})
            state={}
            install_routes(page,state)
            page.goto(URL+'?auth=0',wait_until='networkidle')
            page.get_by_role('tab',name='历史记录',exact=True).click()
            assert not any('/history/positions' in path or '/stats/task-history' in path for path in state['requests'])
            page.close()
        finally:
            browser.close()
    print('48 screenshots: 390/768/1440, Chinese/English, light/dark; filters, pagination, details, refresh errors, empty, long timeline, calibration and authentication passed.')


if __name__ == '__main__':
    main()
