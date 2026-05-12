import React, { useEffect, useRef } from 'react';
import {
  CandlestickSeries,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
} from 'lightweight-charts';
import { Empty } from 'antd';
import { usePreferences } from '../app/preferences';

const EMA_COLORS = {
  '20': '#2563eb',
  '50': '#14b8a6',
  '100': '#f59e0b',
  '200': '#ef4444',
};

function asChartTime(unixSeconds) {
  return Number(unixSeconds);
}

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

export default function KlineChart({ payload }) {
  const containerRef = useRef(null);
  const tooltipRef = useRef(null);
  const { isDark, t } = usePreferences();

  useEffect(() => {
    if (!containerRef.current || !payload?.candles?.length) {
      return undefined;
    }

    const container = containerRef.current;
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

    const candleData = payload.candles.map((item) => ({
      time: asChartTime(item.time),
      open: Number(item.open),
      high: Number(item.high),
      low: Number(item.low),
      close: Number(item.close),
    }));

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#16a34a',
      downColor: '#dc2626',
      borderVisible: false,
      wickUpColor: '#16a34a',
      wickDownColor: '#dc2626',
      lastValueVisible: true,
      priceLineVisible: true,
    });
    candleSeries.setData(candleData);

    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: '',
      lastValueVisible: false,
      priceLineVisible: false,
    });
    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.78, bottom: 0 },
    });
    volumeSeries.setData(
      (payload.volume || []).map((item) => ({
        time: asChartTime(item.time),
        value: Number(item.value),
        color: item.color,
      })),
    );

    const emaSeriesMap = [];
    Object.entries(payload.emas || {}).forEach(([span, values]) => {
      const color = EMA_COLORS[span] || '#64748b';
      const series = chart.addSeries(LineSeries, {
        color,
        lineWidth: 2,
        lastValueVisible: false,
        priceLineVisible: false,
      });
      series.setData(
        values.map((item) => ({
          time: asChartTime(item.time),
          value: Number(item.value),
        })),
      );
      emaSeriesMap.push({ span, series, color });
    });

    const lastTime = payload.candles[payload.candles.length - 1]?.time;
    const staticMarkers = [];

    (payload.positions || []).forEach((position) => {
      if (!lastTime) {
        return;
      }
      staticMarkers.push({
        time: asChartTime(lastTime),
        position: position.side === 'SHORT' ? 'aboveBar' : 'belowBar',
        color: position.side === 'SHORT' ? '#ef4444' : '#22c55e',
        shape: position.side === 'SHORT' ? 'arrowDown' : 'arrowUp',
        text: '',
      });
    });

    // pending_orders circle markers intentionally omitted

    const markerApi = createSeriesMarkers(candleSeries, staticMarkers);

    const priceLines = [];
    let visibleExtremaPriceLines = [];

    const clearVisibleExtremaPriceLines = () => {
      visibleExtremaPriceLines.forEach((line) => candleSeries.removePriceLine(line));
      visibleExtremaPriceLines = [];
    };

    const updateVisibleExtrema = (logicalRange) => {
      if (!logicalRange) return;
      clearVisibleExtremaPriceLines();

      const from = Math.max(0, Math.floor(logicalRange.from));
      const to = Math.min(candleData.length - 1, Math.ceil(logicalRange.to));
      const visibleCandles = candleData.slice(from, to + 1);
      if (!visibleCandles.length) return;

      const highest = visibleCandles.reduce((best, item) => (!best || item.high > best.high ? item : best), null);
      const lowest = visibleCandles.reduce((best, item) => (!best || item.low < best.low ? item : best), null);
      const extremaMarkers = [];

      // 仅 PC 端显示 High/Low price lines（移动端已在上面 return 了）
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

      markerApi.setMarkers([...extremaMarkers, ...staticMarkers]);
    };

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

    const rightPaddingBars = isMobile() ? 2 : 4;
    chart.timeScale().setVisibleLogicalRange({
      from: 0,
      to: candleData.length - 1 + rightPaddingBars,
    });
    updateVisibleExtrema(chart.timeScale().getVisibleLogicalRange());
    chart.timeScale().subscribeVisibleLogicalRangeChange(updateVisibleExtrema);

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

    const resizeObserver = new ResizeObserver(([entry]) => {
      const width = entry.contentRect.width;
      const height = entry.contentRect.height || 420;
      chart.applyOptions({ width, height });
    });
    resizeObserver.observe(container);

    return () => {
      resizeObserver.disconnect();
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(updateVisibleExtrema);
      chart.unsubscribeCrosshairMove(crosshairHandler);
      clearVisibleExtremaPriceLines();
      priceLines.forEach((line) => candleSeries.removePriceLine(line));
      chart.remove();
    };
  }, [isDark, payload]);

  if (!payload?.candles?.length) {
    return <Empty description={t('noData')} />;
  }

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%' }}>
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
