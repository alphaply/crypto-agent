"""Isolated UI regression. Run Vite first, then:
uv run --with playwright python tests/browser_dashboard.py
No backend, credentials, scheduler or exchange calls are used.
"""
import json
import os
import re
from pathlib import Path

from playwright.sync_api import sync_playwright


CHART_INSTRUMENTATION = """
window.__testChart = chart;
window.__testSeries = [];
window.__chartCalls = { series: [] };
const testAddSeries = chart.addSeries.bind(chart);
chart.addSeries = (...args) => {
  const series = testAddSeries(...args);
  window.__testSeries.push(series);
  const calls = { setData: 0, update: 0 };
  window.__chartCalls.series.push(calls);
  if (window.__chartCalls.series.length === 1) window.__testCandleSeries = series;
  for (const method of ['setData', 'update']) {
    const original = series[method].bind(series);
    series[method] = (...values) => { calls[method] += 1; return original(...values); };
  }
  return series;
};
let candleData = [];
"""


def instrument_chart(route):
    response = route.fetch()
    source = response.text()
    assert 'let candleData = [];' in source, 'Chart instrumentation anchor changed'
    route.fulfill(response=response, body=source.replace('let candleData = [];', CHART_INSTRUMENTATION, 1))


def visible_range(page):
    return page.evaluate('window.__testChart.timeScale().getVisibleLogicalRange()')


def assert_range(page, expected):
    page.wait_for_function("""expected => {
      const range = window.__testChart.timeScale().getVisibleLogicalRange();
      return range && Math.abs(range.from - expected.from) < 0.05
        && Math.abs(range.to - expected.to) < 0.05;
    }""", arg=expected)


def assert_canvases_preserved(page):
    assert page.evaluate("""window.originalCanvases.every((node, index) =>
      node.isConnected && node === document.querySelectorAll('.trading-chart canvas')[index])
    """), 'Refreshing, changing theme/symbol, or resizing recreated chart canvases'


def assert_chart_fits(page, width):
    page.wait_for_function("""() => {
      const host = document.querySelector('.trading-chart');
      const chart = host?.querySelector('.tv-lightweight-charts');
      return chart && Math.abs(chart.getBoundingClientRect().width - host.clientWidth) <= 1;
    }""")
    bounds = page.get_by_test_id('kline').bounding_box()
    assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width + 1, bounds
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), f'Horizontal overflow at {width}px'
    readout = page.locator('.kline-readout')
    assert readout.is_visible(), f'OHLC readout hidden at {width}px'
    assert readout.evaluate('(element) => element.scrollWidth <= element.clientWidth + 1'), f'OHLC readout overflows at {width}px'
    shell = page.locator('.kline-chart-shell').bounding_box()
    footer = page.locator('.kline-chart-footer').bounding_box()
    assert footer['y'] + footer['height'] <= shell['y'] + shell['height'] + 1, 'Chart footer clipped'
    assert page.locator('.kline-plot').bounding_box()['height'] >= 200, 'Chart plot collapsed'


def screenshot(page, filename):
    Path(filename).parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=filename, full_page=True)


def test_chart(page):
    page.locator('.trading-chart canvas').first.wait_for()
    page.wait_for_function('window.__testCandleSeries?.data().length === 500')

    # The production preference hook must restore the selected interval on reload.
    page.get_by_label('Chart timeframe').select_option('4h')
    page.wait_for_function("localStorage.getItem('crypto-agent-chart-timeframe') === '4h'")
    page.reload()
    page.wait_for_function('window.__testCandleSeries?.data().length === 500')
    assert page.get_by_label('Chart timeframe').input_value() == '4h'
    page.evaluate("window.originalCanvases = [...document.querySelectorAll('.trading-chart canvas')]")

    initial = visible_range(page)
    assert initial['from'] > 300 and 30 <= initial['to'] - initial['from'] <= 180, initial
    assert initial['to'] >= 499, initial
    assert page.evaluate('new Set(window.__testSeries[1].data().map(bar => bar.color)).size >= 2'), 'Volume lost its up/down colors'
    initial_calls = page.evaluate('window.__chartCalls.series[0]')
    initial_close = page.evaluate('window.__testCandleSeries.data().at(-1).close')
    for _ in range(20):
        page.get_by_role('button', name='Refresh', exact=True).click()
    page.wait_for_function('value => window.__testCandleSeries.data().at(-1).close === value', arg=initial_close + 20)
    assert page.get_by_test_id('tick').inner_text() == '20'
    calls = page.evaluate('window.__chartCalls.series[0]')
    assert calls['setData'] == initial_calls['setData'], calls
    assert calls['update'] - initial_calls['update'] == 20, calls
    assert_range(page, initial)

    # Appending a bar follows the live edge without changing the zoom level.
    page.get_by_role('button', name='Append', exact=True).click()
    page.wait_for_function('window.__testCandleSeries.data().length === 501')
    assert_range(page, {'from': initial['from'] + 1, 'to': initial['to'] + 1})

    # Reading history must survive both tail updates and a rolling server window.
    history = {'from': 310, 'to': 350}
    page.evaluate('range => window.__testChart.timeScale().setVisibleLogicalRange(range)', history)
    assert_range(page, history)
    page.get_by_role('button', name='Refresh', exact=True).click()
    assert_range(page, history)
    page.get_by_role('button', name='Append', exact=True).click()
    page.wait_for_function('window.__testCandleSeries.data().length === 502')
    assert_range(page, history)
    first_time = page.evaluate('window.__testCandleSeries.data()[0].time')
    page.get_by_role('button', name='Rolling', exact=True).click()
    page.wait_for_function('time => window.__testCandleSeries.data()[0].time === time + 3600', arg=first_time)
    assert_range(page, {'from': history['from'] - 1, 'to': history['to'] - 1})

    page.get_by_role('button', name='回到最新', exact=True).click()
    page.wait_for_function('window.__testChart.timeScale().getVisibleLogicalRange().to >= 501')
    live = visible_range(page)
    last_time = page.evaluate('window.__testCandleSeries.data().at(-1).time')
    page.get_by_role('button', name='Rolling', exact=True).click()
    page.wait_for_function('time => window.__testCandleSeries.data().at(-1).time === time + 3600', arg=last_time)
    assert_range(page, live)

    # A tiny token price remains legible on the scale as well as in the OHLC readout.
    page.get_by_role('button', name='Micro price', exact=True).click()
    page.wait_for_function('window.__testCandleSeries.data().at(-1).close < 0.00001')
    price = page.evaluate("""() => {
      const series = window.__testCandleSeries;
      return { format: series.options().priceFormat,
        text: series.priceFormatter().format(series.data().at(-1).close) };
    }""")
    assert price['format']['precision'] >= 6 and price['format']['minMove'] < 0.00001, price
    assert float(price['text'].replace(',', '')) > 0, price
    assert re.search(r'0\.0000\d*[1-9]', page.locator('.kline-readout').inner_text()), page.locator('.kline-readout').inner_text()

    # Programmatic crosshair positioning also verifies local time display.
    expected_time = page.evaluate("""() => {
      const series = window.__testCandleSeries;
      const bar = series.data().at(-1);
      window.__testChart.setCrosshairPosition(bar.close, bar.time, series);
      const date = new Date(bar.time * 1000);
      return String(date.getHours()).padStart(2, '0') + ':' + String(date.getMinutes()).padStart(2, '0');
    }""")
    assert expected_time in page.locator('.kline-readout').inner_text()
    page.evaluate('window.__testChart.clearCrosshairPosition()')

    page.get_by_role('button', name='Theme', exact=True).click()
    page.get_by_role('button', name='Symbol', exact=True).click()
    assert_canvases_preserved(page)
    assert_chart_fits(page, 1440)
    return calls


def test_schedule(page):
    page.get_by_role('button', name='应用预设', exact=False).click()
    config = json.loads(page.get_by_test_id('config').inner_text())
    assert config['run_interval'] == 30
    assert [rule['interval'] for rule in config['run_schedule']] == [30, 20, 30]
    page.get_by_role('button', name='提高优先级', exact=True).nth(1).click()
    config = json.loads(page.get_by_test_id('config').inner_text())
    assert config['run_schedule'][0]['timezone'] == 'America/New_York'
    page.get_by_role('button', name='删除时段', exact=True).nth(0).click()
    page.get_by_role('button', name=re.compile('添.*加.*时.*段')).click()
    assert len(json.loads(page.get_by_test_id('config').inner_text())['run_schedule']) == 3


def test_workspace(page):
    card = page.locator('.market-chart-card')
    card.wait_for()
    assert card.get_by_role('button', name='4h', exact=True).get_attribute('aria-pressed') == 'true'
    assert '当前图表 4h' in card.locator('.market-chart-heading').inner_text()
    card.get_by_role('button', name='1d', exact=True).click()
    assert '正在加载 1d，当前显示 4h' in card.get_by_role('status').inner_text()
    assert card.locator('.chart-wrap').get_attribute('aria-busy') == 'true'
    assert '当前图表 4h' in card.locator('.market-chart-heading').inner_text()
    page.get_by_role('button', name='Fail load', exact=True).click()
    assert '1d 行情加载失败，保留 4h 数据' in card.get_by_role('status').inner_text()
    assert card.locator('.chart-wrap').get_attribute('aria-busy') == 'false'
    card.get_by_role('button', name=re.compile(r'重\s*试')).click()
    assert '正在加载 1d，当前显示 4h' in card.get_by_role('status').inner_text()
    page.get_by_role('button', name='Complete load', exact=True).click()
    assert '当前图表 1d' in card.locator('.market-chart-heading').inner_text()
    assert '自动更新' in card.get_by_role('status').inner_text()
    page.wait_for_function('window.__testChart.options().timeScale.timeVisible === false')
    card.screenshot(path='frontend/node_modules/.cache/dashboard-workspace-desktop.png')

    page.reload()
    card.wait_for()
    assert card.get_by_role('button', name='1d', exact=True).get_attribute('aria-pressed') == 'true'
    assert '当前图表 1d' in card.locator('.market-chart-heading').inner_text()
    for width, height in [(390, 844), (320, 740)]:
        page.set_viewport_size({'width': width, 'height': height})
        page.wait_for_function("""() => {
          const host = document.querySelector('.trading-chart');
          return Math.abs(host.querySelector('.tv-lightweight-charts').getBoundingClientRect().width - host.clientWidth) <= 1;
        }""")
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), f'Workspace overflow at {width}px'
        for button in card.locator('.market-chart-timeframe').all():
            assert button.bounding_box()['height'] >= 44, 'Mobile timeframe target too small'
        shell = card.locator('.kline-chart-shell').bounding_box()
        footer = card.locator('.kline-chart-footer').bounding_box()
        assert footer['y'] + footer['height'] <= shell['y'] + shell['height'] + 1, 'Workspace footer clipped'
        assert card.locator('.kline-plot').bounding_box()['height'] >= 240, 'Workspace plot too short'
        card.get_by_role('button', name='1M', exact=True).click()
        page.get_by_role('button', name='Complete load', exact=True).click()
        assert '当前图表 1M' in card.locator('.market-chart-heading').inner_text()
        selected = card.get_by_role('button', name='1M', exact=True).bounding_box()
        controls = card.locator('.market-chart-timeframes').bounding_box()
        assert selected['x'] >= controls['x'] - 1 and selected['x'] + selected['width'] <= controls['x'] + controls['width'] + 1, 'Selected interval scrolled out of view'
        card.screenshot(path=f'frontend/node_modules/.cache/dashboard-workspace-{width}.png')


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, timezone_id='Asia/Shanghai')
        page = context.new_page()
        errors = []
        unexpected_api_calls = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/src/components/KlineChart.jsx*', instrument_chart)

        def reject_api(route):
            unexpected_api_calls.append(route.request.url)
            route.abort()

        page.route('**/api/**', reject_api)
        fixture_url = os.getenv('UI_TEST_URL', 'http://127.0.0.1:5179/tests/dashboard-fixture.html')
        page.goto(fixture_url)
        calls = test_chart(page)
        screenshot(page, 'frontend/node_modules/.cache/dashboard-desktop.png')

        for width, height in [(390, 844), (320, 740), (844, 390), (1440, 1000)]:
            page.set_viewport_size({'width': width, 'height': height})
            page.get_by_role('button', name='Refresh', exact=True).click()
            assert_chart_fits(page, width)
            assert_canvases_preserved(page)

        page.set_viewport_size({'width': 390, 'height': 844})
        test_schedule(page)
        assert_chart_fits(page, 390)
        screenshot(page, os.getenv('UI_TEST_SCREENSHOT', 'frontend/node_modules/.cache/dashboard-mobile.png'))

        # A fresh mobile mount starts with readable candles, rather than all 500 bars.
        mobile_context = browser.new_context(viewport={'width': 320, 'height': 740}, is_mobile=True,
                                             has_touch=True, timezone_id='Asia/Shanghai')
        mobile = mobile_context.new_page()
        mobile.on('pageerror', lambda error: errors.append(str(error)))
        mobile.route('**/src/components/KlineChart.jsx*', instrument_chart)
        mobile.route('**/api/**', reject_api)
        mobile.goto(fixture_url)
        mobile.wait_for_function('window.__testCandleSeries?.data().length === 500')
        compact = visible_range(mobile)
        assert compact['from'] > 420 and 25 <= compact['to'] - compact['from'] <= 80, compact
        assert_chart_fits(mobile, 320)

        workspace = context.new_page()
        workspace.on('pageerror', lambda error: errors.append(str(error)))
        workspace.route('**/src/components/KlineChart.jsx*', instrument_chart)
        workspace.route('**/api/**', reject_api)
        workspace.goto(f'{fixture_url}?workspace')
        test_workspace(workspace)

        assert not errors, errors
        assert not unexpected_api_calls, unexpected_api_calls
        browser.close()
        print('PASS: remembered timeframe; incremental candle updates; live/history/rolling views; tiny prices; local time; stable canvases; 320/390/844/1440px layouts; workspace toolbar/loading/failure/retry; schedule controls; no browser errors or backend calls.')
        print(f'Tail-refresh candle calls: {json.dumps(calls)}')


if __name__ == '__main__':
    main()
