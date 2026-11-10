"""Synthetic browser checks for the six metrics and automatic trigger states."""
from pathlib import Path

from playwright.sync_api import sync_playwright


OUT = Path(__file__).parents[1] / '.tmp/task-statistics-verification'


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    row = dict(currency='USDT',fill_count=2,closed_orders=2,winning_orders=0,losing_orders=2,
               breakeven_orders=0,unknown_orders=0,long_count=1,short_count=0,
               cancelled_order_count=1,accepted_order_count=2,fees_by_currency={'USDT':.2},
               realized_pnl_before_fees=-19.87,realized_pnl_after_fees=-20.07)
    row['metrics'] = {key:dict(value=value,status='ready') for key,value in
                      [('realized_pnl_after_fees',-20.07),('open_count',1),('win_rate',0),
                       ('profit_loss_ratio',None),('long_short_ratio','1:0'),('cancel_rate',50)]}
    row['metrics']['profit_loss_ratio']['reason']='no_win_samples'
    calls = [dict(operation_id=f'auto-{i}',source='automatic',tool_name='automatic_exit',symbol='ETH/USDT:USDT',
                  status=status,action='close',created_at='2026-11-10T02:00:00+00:00',
                  arguments=dict(source='automatic',trigger_kind='take_profit',time_source='observation'),
                  result=dict(action='close',symbol='ETH/USDT:USDT',side='SHORT',order_type='take_profit_market',
                              trigger_price=2055,amount=.1,status=status,filled_amount=filled))
             for i,(status,filled) in enumerate([('pending',None),('filled',.1)])]
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        try:
            for width in [390,1440]:
                page=browser.new_page(viewport={'width':width,'height':1000},timezone_id='Asia/Singapore')
                errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                def route(request):
                    if '/stats/task-history/' in request.request.url:
                        data={'statistics':dict(mode='REAL',seven_days=row,all_history=row,as_of='2026-11-10T10:00:00+08:00')}
                    else:
                        data={'items':calls,'total':2}
                    request.fulfill(json=data)
                page.route('**/api/**',route)
                page.goto('http://127.0.0.1:18975/tests/task-statistics-fixture.html',wait_until='networkidle')
                assert page.locator('.task-stat').count()==6
                assert '0.00%' in page.locator('.task-stat').nth(2).inner_text()
                assert '1:0' in page.locator('.task-stat').nth(4).inner_text()
                assert '50.00%' in page.locator('.task-stat').nth(5).inner_text()
                assert page.get_by_text('自动触发 · 止盈 · 发现时间',exact=True).count()==2
                assert '已确认成交' not in page.locator('.trade-call').nth(0).inner_text()
                assert '已确认成交' in page.locator('.trade-call').nth(1).inner_text()
                page.get_by_text('全部历史',exact=True).click()
                assert '0.00%' in page.locator('.task-stat').nth(2).inner_text()
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                assert not errors,errors
                page.screenshot(path=str(OUT/f'task-statistics-{width}.png'),full_page=True)
                page.close()
        finally:
            browser.close()
    print('390px / 1440px: six metrics, zero win rate, 1:0 direction, cancel rate and auto states passed.')


if __name__=='__main__':
    main()
