import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Empty, Select, Space, Spin, Typography } from 'antd';
import { api } from '../lib/api';

const PAGE_SIZE = 50;

// The parent keys this component by the selected profile and exchange so that
// results from a previous exchange never become choices for the new exchange.
export default function SpotSymbolPicker({ exchange, profileId, value, onChange, locale = 'zh' }) {
  const zh = locale === 'zh';
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [quoteFilter, setQuoteFilter] = useState('');
  const [page, setPage] = useState(0);
  const [retry, setRetry] = useState(0);
  const [result, setResult] = useState(null);
  const [failure, setFailure] = useState(null);
  const symbolsKey = value.join(',');
  const selectedQuote = value[0]?.split('/')[1] || '';
  const quote = selectedQuote || quoteFilter;
  const requestKey = `${exchange}|${profileId || ''}|${symbolsKey}|${query}|${quote}|${page}|${retry}`;
  const currentResult = result?.requestKey === requestKey ? result : null;
  const error = failure?.requestKey === requestKey ? failure.message : '';
  const loading = Boolean(exchange && !currentResult && !error);

  useEffect(() => {
    const timer = window.setTimeout(() => { setQuery(search.trim()); setPage(0); }, 250);
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    if (!exchange) return undefined;
    let active = true;
    const controller = new AbortController();
    api.get('/config/market-symbols', {
      params: {
        exchange_profile_id: profileId || undefined,
        exchange,
        market_type: 'spot',
        keyword: query || undefined,
        quote: quote || undefined,
        symbols: symbolsKey || undefined,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      },
      signal: controller.signal,
      silent: true,
    }).then(({ data }) => {
      if (active) setResult({ ...data, requestKey });
    }).catch((err) => {
      if (active && err.code !== 'ERR_CANCELED') setFailure({ requestKey, message: err.message || (zh ? '交易所标的加载失败' : 'Unable to load exchange markets') });
    });
    return () => { active = false; controller.abort(); };
  }, [exchange, profileId, symbolsKey, query, quote, page, retry, requestKey, zh]);

  const options = useMemo(() => {
    const records = new Map((currentResult?.symbols || []).map((item) => [item.symbol, item]));
    // Selected values render as tags even when absent from this page. Keeping
    // them out of unrelated search results also makes empty searches explicit.
    return [...records.values()].map((item) => ({
      value: item.symbol,
      label: item.symbol,
      disabled: Boolean(!value.includes(item.symbol) && (value.length >= 10 || (selectedQuote && item.quote !== selectedQuote))),
    }));
  }, [currentResult, value, selectedQuote]);
  const invalid = currentResult?.invalid_symbols || [];
  const isSearching = search.trim() !== query;

  return <Space direction="vertical" size="small" style={{ width: '100%', minWidth: 0 }}>
    {!exchange && <Alert type="info" showIcon title={zh ? '请先选择现货交易所配置，再从接口选择标的。' : 'Choose a spot exchange profile before selecting markets.'} />}
    {exchange && <Space wrap size="small">
      <Typography.Text type="secondary">{zh ? '计价币' : 'Quote'}</Typography.Text>
      <Select
        aria-label={zh ? '现货计价币' : 'Spot quote currency'}
        value={quote || undefined}
        placeholder={zh ? '全部' : 'All'}
        style={{ minWidth: 115 }}
        allowClear={!selectedQuote}
        disabled={Boolean(selectedQuote)}
        options={[...new Set([quote, ...(currentResult?.quote_currencies || result?.quote_currencies || [])].filter(Boolean))].map((item) => ({ value: item, label: item }))}
        onChange={(next) => { setQuoteFilter(next || ''); setPage(0); }}
      />
      <Typography.Text type="secondary">{value.length} / 10</Typography.Text>
    </Space>}
    <Select
      mode="multiple"
      aria-label={zh ? '现货标的' : 'Spot symbols'}
      value={value}
      disabled={!exchange}
      showSearch
      filterOption={false}
      searchValue={search}
      onSearch={setSearch}
      autoClearSearchValue={false}
      loading={loading || isSearching}
      options={isSearching ? [] : options}
      placeholder={zh ? '搜索交易所现货标的，例如 BTC、ETH' : 'Search exchange spot markets, e.g. BTC or ETH'}
      status={invalid.length ? 'error' : undefined}
      onChange={(next) => {
        if (next.length <= 10) { onChange(next); setSearch(''); setQuery(''); setPage(0); }
      }}
      style={{ width: '100%' }}
      notFoundContent={loading || isSearching ? <Spin size="small" /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={zh ? '没有匹配的可交易现货标的' : 'No matching tradable spot markets'} />}
    />
    <Typography.Text type="secondary">{zh ? '标的来自交易所接口；同一任务选择 1–10 个相同计价币的现货标的，共享定投预算。' : 'Markets come from the exchange API. Choose 1–10 spot markets sharing a quote currency and one DCA budget.'}</Typography.Text>
    {error && <Alert type="error" showIcon title={zh ? '无法加载交易所标的，已有选择已保留' : 'Market lookup failed; your selection is preserved'} description={error} action={<Button size="small" aria-label={zh ? '重试' : 'Retry'} onClick={() => setRetry((v) => v + 1)}>{zh ? '重试' : 'Retry'}</Button>} />}
    {invalid.length > 0 && <Alert type="error" showIcon title={zh ? `以下标的不可交易，请移除后重新选择：${invalid.join('、')}` : `Remove unavailable markets and choose again: ${invalid.join(', ')}`} />}
    {currentResult && (currentResult.total > PAGE_SIZE || page > 0) && <Space wrap>
      <Button size="small" disabled={page === 0 || loading} onClick={() => setPage((v) => v - 1)}>{zh ? '上一页' : 'Previous'}</Button>
      <Typography.Text type="secondary">{zh ? `第 ${page + 1} 页，共 ${currentResult.total} 个标的` : `Page ${page + 1} · ${currentResult.total} markets`}</Typography.Text>
      <Button size="small" disabled={!currentResult.has_more || loading} onClick={() => setPage((v) => v + 1)}>{zh ? '下一页' : 'Next'}</Button>
    </Space>}
  </Space>;
}
