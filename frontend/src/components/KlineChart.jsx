import React, { useEffect, useMemo, useRef } from 'react';
import {
  CandlestickSeries,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
} from 'lightweight-charts';
import { Empty } from 'antd';
import { usePreferences } from '../app/usePreferences';

const EMA_COLORS = {
  '20': '#2563eb',
  '50': '#14b8a6',
  '100': '#f59e0b',
  '200': '#ef4444',
};

function formatChartPrice(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '-';
  return numeric >= 100 ? numeric.toFixed(2) : numeric.toFixed(4);
}

function formatTooltipTime(time) {
  if (!time) return '';
  const date = new Date(Number(time) * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export default function KlineChart({ payload, chartKey }) {
  const containerRef = useRef(null);
  const tooltipRef = useRef(null);
  const updateRef = useRef(null);
  const themeRef = useRef(null);
  const { isDark, t } = usePreferences();
  const payloadFingerprint = useMemo(
    () => JSON.stringify({
      candles: payload?.candles || [],
      volume: payload?.volume || [],
      emas: payload?.emas || {},
      positions: payload?.positions || [],
      pending_orders: payload?.pending_orders || [],
      risk_lines: payload?.risk_lines || [],
    }),
    [payload],
  );

  useEffect(() => {
    if (!containerRef.current) {
      return undefined;
    }

    const container = containerRef.current;
    const isDark = false; // Theme is applied by the separate effect without replacing the chart.
    const isMobile = () => window.matchMedia('(max-width: 768px)').matches;
    const chart = createChart(container, {
      width: container.clientWidth,
      height: container.clientHeight || 420,
      layout: {
        background: { type: 'solid', color: isDark ? '#08111f' : '#f8fbff' },
        textColor: isDark ? '#dbeafe' : '#1e293b',
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: isDark ? 'rgba(148, 163, 184, 0.08)' : 'rgba(148, 163, 184, 0.15)' },
        horzLines: { color: isDark ? 'rgba(148, 163, 184, 0.08)' : 'rgba(148, 163, 184, 0.15)' },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
      },
      rightPriceScale: {
        borderColor: isDark ? 'rgba(148, 163, 184, 0.2)' : 'rgba(148, 163, 184, 0.24)',
        scaleMargins: { top: 0.08, bottom: 0.28 },
      },
      timeScale: {
        borderColor: isDark ? 'rgba(148, 163, 184, 0.2)' : 'rgba(148, 163, 184, 0.24)',
        timeVisible: true,
        secondsVisible: false,
        rightOffset: isMobile() ? 2 : 4,
        barSpacing: isMobile() ? 5 : 6,
        minBarSpacing: isMobile() ? 2.5 : 3,
      },
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: false,
      },
      handleScale: {
        axisPressedMouseMove: true,
        mouseWheel: true,
        pinch: true,
      },
    });

    let candleData = [];
    let updating = false;
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#16a34a',
      downColor: '#dc2626',
      borderVisible: false,
      wickUpColor: '#16a34a',
      wickDownColor: '#dc2626',
      lastValueVisible: true,
      priceLineVisible: true,
    });


    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: '',
      lastValueVisible: false,
      priceLineVisible: false,
    });
    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.78, bottom: 0 },
    });
    const emaSeriesMap = [];
    let staticMarkers = [];
    const markerApi = createSeriesMarkers(candleSeries, []);
    const priceLines = [];
    let visibleExtremaPriceLines = [];

    const clearVisibleExtremaPriceLines = () => {
      visibleExtremaPriceLines.forEach((line) => candleSeries.removePriceLine(line));
      visibleExtremaPriceLines = [];
    };

    const updateVisibleExtrema = (logicalRange) => {
      if (updating || !logicalRange) return;
      clearVisibleExtremaPriceLines();

      const from = Math.max(0, Math.floor(logicalRange.from));
      const to = Math.min(candleData.length - 1, Math.ceil(logicalRange.to));
      const visibleCandles = candleData.slice(from, to + 1);
      if (!visibleCandles.length) {
        markerApi.setMarkers(staticMarkers);
        return;
      }

      const highest = visibleCandles.reduce((best, item) => (!best || item.high > best.high ? item : best), null);
      const lowest = visibleCandles.reduce((best, item) => (!best || item.low < best.low ? item : best), null);
      const extremaMarkers = [];

      if (highest) {
        extremaMarkers.push({
          time: highest.time,
          position: 'aboveBar',
          color: '#ef4444',
          shape: 'arrowDown',
          text: '',
        });
        visibleExtremaPriceLines.push(
          candleSeries.createPriceLine({
            price: highest.high,
            color: '#ef4444',
            lineWidth: 1,
            lineStyle: 2,
            axisLabelVisible: true,
            title: '',
          }),
        );
      }

      if (lowest) {
        extremaMarkers.push({
          time: lowest.time,
          position: 'belowBar',
          color: '#16a34a',
          shape: 'arrowUp',
          text: '',
        });
        visibleExtremaPriceLines.push(
          candleSeries.createPriceLine({
            price: lowest.low,
            color: '#16a34a',
            lineWidth: 1,
            lineStyle: 2,
            axisLabelVisible: true,
            title: '',
          }),
        );
      }

      markerApi.setMarkers([...extremaMarkers, ...staticMarkers].sort((a, b) => a.time - b.time));
    };

    let previousKey;
    let initialized = false;
    updateRef.current = (nextPayload, nextKey) => {
      updating = true;
      if (tooltipRef.current) tooltipRef.current.style.display = 'none';
      const range = chart.timeScale().getVisibleLogicalRange();
      const oldFirst = candleData[0]?.time;
      candleData = (nextPayload?.candles || []).map(item => ({
        time: Number(item.time), open: Number(item.open), high: Number(item.high),
        low: Number(item.low), close: Number(item.close),
      }));
      clearVisibleExtremaPriceLines();
      markerApi.setMarkers([]);
      candleSeries.setData(candleData);
      volumeSeries.setData((nextPayload?.volume || []).map(item => ({ ...item, time: Number(item.time), value: Number(item.value) })));
      const emas = nextPayload?.emas || {};
      for (let index = emaSeriesMap.length - 1; index >= 0; index -= 1) {
        if (!(emaSeriesMap[index].span in emas)) {
          chart.removeSeries(emaSeriesMap[index].series);
          emaSeriesMap.splice(index, 1);
        }
      }
      Object.entries(emas).forEach(([span, values]) => {
        let entry = emaSeriesMap.find(item => item.span === span);
        if (!entry) {
          const color = EMA_COLORS[span] || '#64748b';
          entry = { span, color, series: chart.addSeries(LineSeries, { color, lineWidth: 2, lastValueVisible: false, priceLineVisible: false }) };
          emaSeriesMap.push(entry);
        }
        entry.series.setData(values.map(item => ({ time: Number(item.time), value: Number(item.value) })));
      });
      const lastTime = candleData.at(-1)?.time;
      staticMarkers = lastTime ? (nextPayload?.positions || []).map(position => ({
        time: lastTime, position: position.side === 'SHORT' ? 'aboveBar' : 'belowBar',
        color: position.side === 'SHORT' ? '#ef4444' : '#22c55e',
        shape: position.side === 'SHORT' ? 'arrowDown' : 'arrowUp', text: '',
      })) : [];
      priceLines.splice(0).forEach(line => candleSeries.removePriceLine(line));
      const payload = nextPayload || {};
      (payload.positions || []).forEach((position) => {
        if (!position.entry_price) {
          return;
        }
        priceLines.push(
          candleSeries.createPriceLine({
            price: Number(position.entry_price),
            color: position.side === 'SHORT' ? '#f97316' : '#22c55e',
            lineWidth: 2,
            lineStyle: 2,
            axisLabelVisible: true,
            title: '',
          }),
        );
      });

      (payload.pending_orders || []).forEach((order) => {
        if (!order.price) {
          return;
        }
        priceLines.push(
          candleSeries.createPriceLine({
            price: Number(order.price),
            color: '#38bdf8',
            lineWidth: 1,
            lineStyle: 1,
            axisLabelVisible: true,
            title: '',
          }),
        );
      });

      (payload.risk_lines || []).forEach((line) => {
        if (!line.price) {
          return;
        }
        priceLines.push(
          candleSeries.createPriceLine({
            price: Number(line.price),
            color: line.type === 'take_profit' ? '#16a34a' : '#ef4444',
            lineWidth: 1,
            lineStyle: 3,
            axisLabelVisible: true,
            title: '',
          }),
        );
      });


      if (candleData.length) {
        if (!initialized || previousKey !== nextKey) {
          chart.timeScale().setVisibleLogicalRange({ from: 0, to: candleData.length - 1 + (isMobile() ? 2 : 4) });
        } else if (range) {
          // Rolling candle windows remove bars at the left; retain the same timestamps and zoom.
          const step = candleData[1]?.time - candleData[0]?.time;
          const shift = step > 0 && oldFirst ? (oldFirst - candleData[0].time) / step : 0;
          chart.timeScale().setVisibleLogicalRange({ from: range.from + shift, to: range.to + shift });
        }
        initialized = true;
      }
      previousKey = nextKey;
      updating = false;
      updateVisibleExtrema(chart.timeScale().getVisibleLogicalRange());
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(updateVisibleExtrema);
    themeRef.current = (dark) => chart.applyOptions({
      layout: { background: { type: 'solid', color: dark ? '#08111f' : '#f8fbff' }, textColor: dark ? '#dbeafe' : '#1e293b' },
      grid: { vertLines: { color: dark ? 'rgba(148,163,184,0.08)' : 'rgba(148,163,184,0.15)' }, horzLines: { color: dark ? 'rgba(148,163,184,0.08)' : 'rgba(148,163,184,0.15)' } },
    });

    // OHLC crosshair tooltip
    const tooltipEl = tooltipRef.current;

    const crosshairHandler = (param) => {
      if (!tooltipEl) return;
      if (!param.point || !param.time || param.point.x < 0 || param.point.y < 0) {
        tooltipEl.style.display = 'none';
        return;
      }
      const bar = param.seriesData.get(candleSeries);
      if (!bar) {
        tooltipEl.style.display = 'none';
        return;
      }
      const isUp = bar.close >= bar.open;
      const color = isUp ? '#16a34a' : '#dc2626';
      let emaRows = '';
      emaSeriesMap.forEach(({ span, series, color: emaColor }) => {
        const emaVal = param.seriesData.get(series);
        if (emaVal !== undefined && emaVal !== null) {
          const v = typeof emaVal === 'object' ? emaVal.value : emaVal;
          if (v !== undefined && v !== null) {
            emaRows += `<div class="kline-tooltip-row"><span class="kline-tooltip-label" style="color:${emaColor}">EMA${span}</span><span style="color:${emaColor}">${formatChartPrice(v)}</span></div>`;
          }
        }
      });
      tooltipEl.innerHTML = `
        <div class="kline-tooltip-time">${formatTooltipTime(param.time)}</div>
        <div class="kline-tooltip-row"><span class="kline-tooltip-label">O</span><span style="color:${color}">${formatChartPrice(bar.open)}</span></div>
        <div class="kline-tooltip-row"><span class="kline-tooltip-label">H</span><span style="color:#ef4444">${formatChartPrice(bar.high)}</span></div>
        <div class="kline-tooltip-row"><span class="kline-tooltip-label">L</span><span style="color:#16a34a">${formatChartPrice(bar.low)}</span></div>
        <div class="kline-tooltip-row"><span class="kline-tooltip-label">C</span><span style="color:${color}">${formatChartPrice(bar.close)}</span></div>
        ${emaRows ? `<div class="kline-tooltip-divider"></div>${emaRows}` : ''}
      `;
      tooltipEl.style.display = 'block';
      if (isMobile()) {
        // 移动端固定在图表顶部
        tooltipEl.style.left = '8px';
        tooltipEl.style.top = '8px';
      } else {
        const tooltipWidth = 140;
        const tooltipHeight = 90 + emaSeriesMap.length * 20;
        const containerWidth = container.clientWidth;
        const containerHeight = container.clientHeight || 420;
        let left = param.point.x + 16;
        let top = param.point.y - 20;
        if (left + tooltipWidth > containerWidth) left = param.point.x - tooltipWidth - 8;
        if (top + tooltipHeight > containerHeight) top = containerHeight - tooltipHeight - 8;
        if (top < 0) top = 8;
        tooltipEl.style.left = `${left}px`;
        tooltipEl.style.top = `${top}px`;
      }
    };

    chart.subscribeCrosshairMove(crosshairHandler);

    let resizeFrame = null;
    let lastWidth = Math.round(container.clientWidth);
    let lastHeight = Math.round(container.clientHeight || 420);
    const resizeObserver = new ResizeObserver(([entry]) => {
      const width = Math.round(entry.contentRect.width);
      const height = Math.round(entry.contentRect.height || 420);
      if (width < 1 || height < 1 || (width === lastWidth && height === lastHeight)) return;
      lastWidth = width;
      lastHeight = height;
      if (resizeFrame) window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(() => {
        chart.applyOptions({ width, height });
        resizeFrame = null;
      });
    });
    resizeObserver.observe(container);

    return () => {
      if (resizeFrame) window.cancelAnimationFrame(resizeFrame);
      resizeObserver.disconnect();
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(updateVisibleExtrema);
      chart.unsubscribeCrosshairMove(crosshairHandler);
      clearVisibleExtremaPriceLines();
      priceLines.forEach((line) => candleSeries.removePriceLine(line));
      updateRef.current = null;
      themeRef.current = null;
      chart.remove();
    };
  }, []);

  useEffect(() => { updateRef.current?.(JSON.parse(payloadFingerprint), chartKey); }, [payloadFingerprint, chartKey]);
  useEffect(() => { themeRef.current?.(isDark); }, [isDark]);

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%' }}>
      {!payload?.candles?.length && <div style={{ position: 'absolute', inset: 0, zIndex: 1 }}><Empty description={t('noData')} /></div>}
      <div ref={containerRef} className="trading-chart" />
      <div ref={tooltipRef} className="kline-tooltip" style={{ display: 'none' }} />
      {payload?.emas && Object.keys(payload.emas).length > 0 && (
        <div className="kline-ema-legend">
          {Object.entries(EMA_COLORS).filter(([span]) => payload.emas[span]?.length > 0).map(([span, color]) => (
            <span key={span} className="kline-ema-legend-item">
              <span className="kline-ema-legend-line" style={{ backgroundColor: color }} />
              <span>EMA{span}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
