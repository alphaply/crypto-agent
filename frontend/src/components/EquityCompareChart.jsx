import React, { useCallback, useMemo, useState } from 'react';
import { Empty, Segmented, Select, Space, Tag, Typography } from 'antd';
import LineChart from './LineChart';
import { usePreferences } from '../app/usePreferences';

const { Text } = Typography;

function formatNumber(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '-';
  return numeric.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function formatPercent(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '-';
  return `${numeric >= 0 ? '+' : ''}${numeric.toFixed(2)}%`;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

function sourceName(source, locale) {
  const kind = source?.kind;
  if (kind === 'real_equity') return locale === 'zh' ? '实盘账户权益快照' : 'Live account equity snapshots';
  if (kind === 'strategy_equity') return locale === 'zh' ? '策略模拟权益快照' : 'Strategy simulated equity snapshots';
  if (kind === 'dca_invested') return locale === 'zh' ? '定投累计投入快照' : 'DCA invested capital snapshots';
  return source?.display_label || source?.label || '-';
}

export default function EquityCompareChart({ series = [], selectedIds = [], onSelectedIdsChange }) {
  const { locale, t } = usePreferences();
  const [valueMode, setValueMode] = useState('equity');
  const allIds = useMemo(() => series.map((item) => item.config_id).filter(Boolean), [series]);
  const activeIds = selectedIds.length ? selectedIds : allIds;
  const visibleItems = useMemo(
    () => series.filter((item) => activeIds.includes(item.config_id)),
    [activeIds, series],
  );
  const plottedItems = useMemo(() => visibleItems.filter((item) => item.points?.length), [visibleItems]);
  const isReturnMode = valueMode === 'return';

  const chartSeries = useMemo(() => plottedItems.map((item) => {
    const base = Number(item.points?.[0]?.equity || 0);
    return {
      name: item.label || item.config_id,
      data: (item.points || []).map((point) => ({
        name: point.date,
        value: isReturnMode && base ? ((Number(point.equity) - base) / base) * 100 : Number(point.equity),
      })),
    };
  }), [isReturnMode, plottedItems]);

  const tooltipFormatter = useCallback((params = []) => {
    if (!params.length) return '';
    const date = escapeHtml(params[0]?.value?.[0] || '');
    const rows = params.map((item) => (
      `<div>${item.marker}${escapeHtml(item.seriesName)} <strong>${isReturnMode ? formatPercent(item.value?.[1]) : formatNumber(item.value?.[1])}</strong></div>`
    )).join('');
    return `<div class="equity-chart-tooltip"><strong>${date}</strong>${rows}</div>`;
  }, [isReturnMode]);

  const valueFormatter = useCallback(
    (value) => `${value}%`,
    [],
  );

  return (
    <div className="equity-compare-content">
      <div className="equity-compare-toolbar">
        <Segmented
          value={valueMode}
          options={[
            { label: locale === 'zh' ? '绝对权益' : 'Equity', value: 'equity' },
            { label: locale === 'zh' ? '起点收益率' : 'Return', value: 'return' },
          ]}
          onChange={setValueMode}
        />
        <Select
          mode="multiple"
          allowClear
          maxTagCount="responsive"
          className="equity-compare-select"
          value={activeIds}
          options={series.map((item) => ({
            value: item.config_id,
            label: `${item.label || item.config_id} · ${item.mode || '-'}`,
          }))}
          onChange={(values) => onSelectedIdsChange?.(values)}
          placeholder={locale === 'zh' ? '选择对比曲线' : 'Select curves'}
        />
      </div>

      <Text type="secondary" className="equity-compare-help">
        {locale === 'zh'
          ? '权益基于各配置自己的快照。只显示真实记录日期，不会补齐或伪造缺失数据。'
          : 'Each curve uses its own snapshots. Missing dates stay empty and are never fabricated.'}
      </Text>

      <div className="chart-wrap">
        {chartSeries.length ? (
          <LineChart
            series={chartSeries}
            yName={isReturnMode ? '%' : t('equity')}
            valueFormatter={isReturnMode ? valueFormatter : undefined}
            tooltipFormatter={tooltipFormatter}
          />
        ) : (
          <Empty description={t('noData')} />
        )}
      </div>

      <div className="equity-series-list">
        {visibleItems.map((item) => (
          <div className="equity-series-item" key={item.config_id}>
            <div className="equity-series-head">
              <Space size={6} wrap>
                <Tag color={item.mode === 'REAL' ? 'green' : item.mode === 'STRATEGY' ? 'blue' : 'gold'}>{item.mode || '-'}</Tag>
                <Text strong>{item.label || item.config_id}</Text>
              </Space>
              <Tag color={item.data_state === 'ready' ? 'success' : 'default'}>
                {item.data_state === 'ready'
                  ? `${item.point_count || 0} ${locale === 'zh' ? '个快照' : 'snapshots'}`
                  : (locale === 'zh' ? '暂无快照' : 'No snapshots')}
              </Tag>
            </div>
            <Text type="secondary" className="equity-series-source">
              {sourceName(item.data_source, locale)}
            </Text>
            {item.data_state === 'ready' ? (
              <div className="equity-series-meta">
                <span>{item.first_date} → {item.last_date}</span>
                <span>{locale === 'zh' ? '最新' : 'Latest'} {formatNumber(item.latest_equity)}</span>
                <span className={Number(item.return_pct) < 0 ? 'negative' : 'positive'}>{formatPercent(item.return_pct)}</span>
              </div>
            ) : (
              <Text type="secondary">
                {locale === 'zh' ? '该配置在当前标的和时间范围内没有权益快照。' : 'No equity snapshots for this configuration and symbol.'}
              </Text>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
