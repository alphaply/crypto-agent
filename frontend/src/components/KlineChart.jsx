import React, { useEffect, useMemo, useRef } from 'react';
import { CandlestickSeries, CrosshairMode, HistogramSeries, LineSeries, TickMarkType, createChart, createSeriesMarkers } from 'lightweight-charts';
import { Empty } from 'antd';
import { usePreferences } from '../app/usePreferences';
import { klinePriceFormat, normalizeKlineData, preserveKlineRange, recentKlineRange, syncSeriesData } from '../lib/kline';

const EMA_COLORS = { 20: '#2563eb', 50: '#14b8a6', 100: '#d97706', 200: '#ef4444' };
const UP = '#16a34a';
const DOWN = '#dc2626';

function localTime(time, dateOnly = false) {
  const date = new Date(Number(time) * 1000);
  const pad = value => String(value).padStart(2, '0');
  const day = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  return dateOnly ? day : `${day} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function tickTime(time, type) {
  const date = new Date(Number(time) * 1000);
  if (type === TickMarkType.Year) return String(date.getFullYear());
  if (type === TickMarkType.Month) return `${date.getFullYear()}/${date.getMonth() + 1}`;
  if (type === TickMarkType.DayOfMonth) return `${date.getMonth() + 1}/${date.getDate()}`;
  return localTime(time).slice(11);
}

export default function KlineChart({ payload, chartKey, timeframe = '1h' }) {
  const containerRef = useRef(null);
  const readoutRef = useRef(null);
  const updateRef = useRef(null);
  const preferencesRef = useRef(null);
  const resetRef = useRef(null);
  const { isDark, locale, t } = usePreferences();
  const data = useMemo(() => normalizeKlineData(payload), [payload]);
  const daily = ['1d', '1w', '1M'].includes(timeframe);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;
    let compact = container.clientWidth < 600;
    let dark = false;
    let language = 'zh';
    let dateOnly = false;
    let priceFormat = { precision: 2 };
    let hoveredTime = null;
    let readoutFrame = null;
    let extremaFrame = null;
    let resizeFrame = null;
    let updating = false;
    let previousKey;
    let volumeData = [];
    let positionMarkers = [];
    let markerSignature = '';
    let extremaSignature = '';
    const overlays = new Map();
    const emaSeriesMap = new Map();
    const chart = createChart(container, {
      width: Math.max(1, container.clientWidth), height: Math.max(1, container.clientHeight),
      layout: { background: { type: 'solid', color: '#f8fbff' }, textColor: '#475569', fontSize: compact ? 10 : 12, attributionLogo: false },
      grid: { vertLines: { visible: false }, horzLines: { color: 'rgba(148,163,184,0.13)' } },
      crosshair: { mode: CrosshairMode.Magnet },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.12, bottom: 0.24 } },
      timeScale: {
        borderVisible: false, timeVisible: true, secondsVisible: false,
        rightOffset: compact ? 3 : 5, barSpacing: 8, minBarSpacing: 3,
        shiftVisibleRangeOnNewBar: false, tickMarkFormatter: tickTime,
      },
      localization: { timeFormatter: time => localTime(time, dateOnly) },
      handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
    });
    let candleData = [];
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: UP, downColor: DOWN, borderVisible: false, wickUpColor: UP, wickDownColor: DOWN,
    });
    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' }, priceScaleId: '', lastValueVisible: false, priceLineVisible: false,
    });
    volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.83, bottom: 0 } });
    const markerApi = createSeriesMarkers(candleSeries, []);
    const highLine = candleSeries.createPriceLine({ price: 0, color: DOWN, lineWidth: 1, lineStyle: 2, lineVisible: false, axisLabelVisible: false });
    const lowLine = candleSeries.createPriceLine({ price: 0, color: UP, lineWidth: 1, lineStyle: 2, lineVisible: false, axisLabelVisible: false });

    const renderReadout = () => {
      const element = readoutRef.current;
      if (!element) return;
      const selected = hoveredTime && candleData.find(bar => bar.time === hoveredTime);
      const bar = selected || candleData.at(-1);
      if (!bar) { element.replaceChildren(); return; }
      const color = bar.close >= bar.open ? UP : DOWN;
      const change = bar.open ? (bar.close / bar.open - 1) * 100 : 0;
      const price = value => Number(value).toFixed(priceFormat.precision);
      const labels = language === 'zh' ? ['开', '高', '低', '收'] : ['O', 'H', 'L', 'C'];
      // Only formatted numbers and fixed labels are inserted, never raw API strings.
      element.innerHTML = `
        <div class="kline-readout-summary">
          <strong style="color:${color}">${price(bar.close)}</strong>
          <span class="kline-readout-change" style="color:${color}">${change >= 0 ? '+' : ''}${change.toFixed(2)}%</span>
          <span class="kline-readout-time">${localTime(bar.time, dateOnly)} · ${language === 'zh' ? '本地时间' : 'Local time'}</span>
        </div>
        <div class="kline-ohlc-grid">${['open', 'high', 'low', 'close'].map((field, index) => `
          <span><span class="kline-readout-label">${labels[index]}</span><b>${price(bar[field])}</b></span>`).join('')}
        </div>`;
    };
    const scheduleReadout = () => {
      if (readoutFrame !== null) return;
      readoutFrame = window.requestAnimationFrame(() => { readoutFrame = null; renderReadout(); });
    };
    const updateVisibleExtrema = () => {
      extremaFrame = null;
      if (updating) return;
      const range = chart.timeScale().getVisibleLogicalRange();
      let highest;
      let lowest;
      if (range) {
        const from = Math.max(0, Math.ceil(range.from));
        const to = Math.min(candleData.length - 1, Math.floor(range.to));
        for (let index = from; index <= to; index += 1) {
          const bar = candleData[index];
          if (!highest || bar.high > highest.high) highest = bar;
          if (!lowest || bar.low < lowest.low) lowest = bar;
        }
      }
      const signature = `${highest?.time}:${highest?.high}:${lowest?.time}:${lowest?.low}:${compact}`;
      if (signature !== extremaSignature) {
        highLine.applyOptions({ price: highest?.high || 0, lineVisible: !!highest, axisLabelVisible: !!highest && !compact });
        lowLine.applyOptions({ price: lowest?.low || 0, lineVisible: !!lowest, axisLabelVisible: !!lowest && !compact });
        extremaSignature = signature;
      }
      const markers = [...positionMarkers];
      if (highest) markers.push({ time: highest.time, position: 'aboveBar', color: DOWN, shape: 'arrowDown', text: '' });
      if (lowest) markers.push({ time: lowest.time, position: 'belowBar', color: UP, shape: 'arrowUp', text: '' });
      markers.sort((a, b) => a.time - b.time);
      const nextSignature = JSON.stringify(markers);
      if (markerSignature !== nextSignature) { markerApi.setMarkers(markers); markerSignature = nextSignature; }
    };
    const scheduleExtrema = () => {
      if (!updating && extremaFrame === null) extremaFrame = window.requestAnimationFrame(updateVisibleExtrema);
    };
    const resetView = () => {
      if (!candleData.length) return;
      chart.priceScale('right').applyOptions({ autoScale: true });
      chart.timeScale().setVisibleLogicalRange(recentKlineRange(candleData.length, container.clientWidth));
      hoveredTime = null;
      scheduleReadout();
    };
    resetRef.current = resetView;
    const syncOverlays = nextPayload => {
      const desired = new Map();
      const addLines = (rows, prefix, options) => (rows || []).forEach((row, index) => {
        const price = Number(prefix === 'position' ? row.entry_price : row.price);
        if (Number.isFinite(price) && price > 0) desired.set(`${prefix}:${index}`, { price, ...options(row), axisLabelVisible: true, title: '' });
      });
      addLines(nextPayload?.positions, 'position', row => ({ color: row.side === 'SHORT' ? '#f97316' : '#22c55e', lineWidth: 2, lineStyle: 2 }));
      addLines(nextPayload?.pending_orders, 'order', () => ({ color: '#38bdf8', lineWidth: 1, lineStyle: 1 }));
      addLines(nextPayload?.risk_lines, 'risk', row => ({ color: row.type === 'take_profit' ? UP : DOWN, lineWidth: 1, lineStyle: 3 }));
      for (const [key, entry] of overlays) {
        if (!desired.has(key)) { candleSeries.removePriceLine(entry.line); overlays.delete(key); }
      }
      for (const [key, options] of desired) {
        const signature = JSON.stringify(options);
        const entry = overlays.get(key);
        if (!entry) overlays.set(key, { line: candleSeries.createPriceLine(options), signature });
        else if (entry.signature !== signature) { entry.line.applyOptions(options); entry.signature = signature; }
      }
    };
    updateRef.current = (next, nextPayload, nextKey) => {
      updating = true;
      const previous = candleData;
      const range = chart.timeScale().getVisibleLogicalRange();
      const reset = previousKey !== nextKey || !previous.length;
      candleData = next.candles;
      const format = klinePriceFormat(candleData);
      const precisionChanged = priceFormat.precision !== format.precision;
      priceFormat = format;
      if (precisionChanged || reset) candleSeries.applyOptions({ priceFormat });
      const changed = syncSeriesData(candleSeries, previous, candleData, reset);
      syncSeriesData(volumeSeries, volumeData, next.volume, reset);
      volumeData = next.volume;
      for (const [span, entry] of emaSeriesMap) {
        if (!(span in next.emas)) { chart.removeSeries(entry.series); emaSeriesMap.delete(span); }
      }
      for (const [span, values] of Object.entries(next.emas)) {
        let entry = emaSeriesMap.get(span);
        if (!entry) {
          entry = { data: [], series: chart.addSeries(LineSeries, {
            color: EMA_COLORS[span] || '#64748b', lineWidth: 1, lastValueVisible: false,
            priceLineVisible: false, crosshairMarkerVisible: false, priceFormat,
            // Distant EMA200 values should not flatten the visible candle range.
            autoscaleInfoProvider: () => null,
          }) };
          emaSeriesMap.set(span, entry);
        } else if (precisionChanged) entry.series.applyOptions({ priceFormat });
        syncSeriesData(entry.series, entry.data, values, reset);
        entry.data = values;
      }
      syncOverlays(nextPayload);
      const lastTime = candleData.at(-1)?.time;
      positionMarkers = lastTime ? (nextPayload?.positions || []).map(position => ({
        time: lastTime, position: position.side === 'SHORT' ? 'aboveBar' : 'belowBar',
        color: position.side === 'SHORT' ? DOWN : UP,
        shape: position.side === 'SHORT' ? 'arrowDown' : 'arrowUp', text: '',
      })) : [];
      if (reset) {
        hoveredTime = null;
        resetView();
      } else if (changed && candleData.length) {
        const nextRange = preserveKlineRange(range, previous, candleData) || recentKlineRange(candleData.length, container.clientWidth);
        // Price-only ticks do not need to touch the horizontal viewport.
        const axisChanged = previous.length !== candleData.length || previous.some((bar, index) => bar.time !== candleData[index]?.time);
        if (axisChanged) chart.timeScale().setVisibleLogicalRange(nextRange);
      }
      previousKey = nextKey;
      updating = false;
      scheduleReadout();
      scheduleExtrema();
    };
    const applyAppearance = () => {
      chart.applyOptions({
        layout: { background: { type: 'solid', color: dark ? '#08111f' : '#f8fbff' }, textColor: dark ? '#cbd5e1' : '#475569', fontSize: compact ? 10 : 12 },
        grid: { horzLines: { color: dark ? 'rgba(148,163,184,0.10)' : 'rgba(148,163,184,0.13)' } },
        crosshair: { vertLine: { labelBackgroundColor: dark ? '#334155' : '#475569' }, horzLine: { labelBackgroundColor: dark ? '#334155' : '#475569' } },
        timeScale: { timeVisible: !dateOnly, tickMarkFormatter: tickTime },
        localization: { locale: language === 'zh' ? 'zh-CN' : 'en-US', timeFormatter: time => localTime(time, dateOnly) },
      });
    };
    preferencesRef.current = (nextDark, nextLocale, nextDaily) => {
      dark = nextDark;
      language = nextLocale;
      dateOnly = nextDaily;
      applyAppearance();
      scheduleReadout();
    };
    const crosshairHandler = param => {
      hoveredTime = param.point && param.point.x >= 0 && param.point.y >= 0
        && param.point.x <= container.clientWidth && param.point.y <= container.clientHeight
        && param.seriesData.get(candleSeries) ? Number(param.time) : null;
      scheduleReadout();
    };
    chart.subscribeCrosshairMove(crosshairHandler);
    chart.timeScale().subscribeVisibleLogicalRangeChange(scheduleExtrema);
    let lastWidth = container.clientWidth;
    let lastHeight = container.clientHeight;
    const resizeObserver = new ResizeObserver(([entry]) => {
      const width = Math.round(entry.contentRect.width);
      const height = Math.round(entry.contentRect.height);
      if (width < 1 || height < 1 || (width === lastWidth && height === lastHeight)) return;
      if (resizeFrame !== null) window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(() => {
        resizeFrame = null;
        const range = chart.timeScale().getVisibleLogicalRange();
        const wasRecent = range && candleData.length && Math.abs(range.from - recentKlineRange(candleData.length, lastWidth).from) < 1
          && Math.abs(range.to - recentKlineRange(candleData.length, lastWidth).to) < 1;
        compact = width < 600;
        chart.resize(width, height);
        applyAppearance();
        if (wasRecent) resetView();
        else if (range) chart.timeScale().setVisibleLogicalRange(range);
        lastWidth = width;
        lastHeight = height;
        scheduleExtrema();
      });
    });
    resizeObserver.observe(container);
    return () => {
      [readoutFrame, extremaFrame, resizeFrame].forEach(frame => { if (frame !== null) window.cancelAnimationFrame(frame); });
      resizeObserver.disconnect();
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(scheduleExtrema);
      chart.unsubscribeCrosshairMove(crosshairHandler);
      updateRef.current = null;
      preferencesRef.current = null;
      resetRef.current = null;
      chart.remove();
    };
  }, []);

  useEffect(() => { updateRef.current?.(data, payload, chartKey); }, [data, payload, chartKey]);
  useEffect(() => { preferencesRef.current?.(isDark, locale, daily); }, [isDark, locale, daily]);

  return (
    <div className="kline-chart-shell">
      <div ref={readoutRef} className="kline-readout" aria-label={locale === 'zh' ? 'K线价格详情' : 'Candle price details'} />
      <div className="kline-plot">
        <div ref={containerRef} className="trading-chart" role="img" aria-label={locale === 'zh' ? '价格与成交量K线图' : 'Candlestick and volume chart'} />
        {!data.candles.length && <div className="kline-empty"><Empty description={t('noData')} /></div>}
      </div>
      <div className="kline-chart-footer">
        <div className="kline-ema-legend" aria-label={locale === 'zh' ? '均线图例' : 'EMA legend'}>
          {Object.keys(data.emas).filter(span => data.emas[span].length).map(span => (
            <span key={span} className="kline-ema-legend-item"><span className="kline-ema-legend-line" style={{ backgroundColor: EMA_COLORS[span] || '#64748b' }} />EMA{span}</span>
          ))}
        </div>
        <button className="kline-reset-view" type="button" disabled={!data.candles.length} onClick={() => resetRef.current?.()}>
          {locale === 'zh' ? '回到最新' : 'Latest candles'} <span aria-hidden="true">↗</span>
        </button>
      </div>
    </div>
  );
}
