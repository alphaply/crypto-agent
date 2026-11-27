import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api';
import { createKlineCache, klineCacheKey } from '../lib/kline';

const CHART_CACHE = createKlineCache({ maxEntries: 64, staleMs: 10000 });
const INFLIGHT_CHART_REQUESTS = new Map();

function fetchChartSection(configId, timeframe, symbol) {
  const cacheKey = klineCacheKey(configId, symbol, timeframe);
  const existing = INFLIGHT_CHART_REQUESTS.get(cacheKey);
  if (existing) return existing;

  const request = api.get(`/public/workspace/${encodeURIComponent(configId)}`, {
    params: { timeframe, symbol, section: 'chart' },
    silent: true,
    timeout: 30000,
  }).then(({ data }) => {
    const entry = CHART_CACHE.set(cacheKey, data, Date.now());
    return entry;
  }).finally(() => {
    if (INFLIGHT_CHART_REQUESTS.get(cacheKey) === request) {
      INFLIGHT_CHART_REQUESTS.delete(cacheKey);
    }
  });

  INFLIGHT_CHART_REQUESTS.set(cacheKey, request);
  return request;
}

function useSection(configId, section, key, params) {
  const [cache, setCache] = useState({});
  useEffect(() => {
    if (!configId) return undefined;
    const controller = new AbortController();
    api.get(`/public/workspace/${encodeURIComponent(configId)}`, {
      params: { ...JSON.parse(params), section }, signal: controller.signal, silent: true, timeout: 30000,
    }).then(({ data }) => {
      if (!controller.signal.aborted) setCache(previous => ({ ...previous, [configId]: { data, key, loading: false, error: '', updatedAt: Date.now() } }));
    }).catch(error => {
      if (!controller.signal.aborted) setCache(previous => ({ ...previous, [configId]: { ...previous[configId], key, loading: false, error: error.message } }));
    });
    return () => controller.abort();
  }, [configId, section, key, params]);
  const result = cache[configId] || {};
  const staleKey = result.key !== key;
  return { ...result, error: staleKey ? '' : result.error, loading: Boolean(configId) && (staleKey || result.loading) };
}

function useChartSection(configId, refreshKey, timeframe, symbol) {
  const cacheKey = klineCacheKey(configId, symbol, timeframe);
  const lastRefreshRef = useRef({ cacheKey: '', refreshKey: '' });
  const [state, setState] = useState(() => {
    const cached = configId ? CHART_CACHE.get(cacheKey) : null;
    return cached
      ? { cacheKey, configId, symbol, data: cached.data, updatedAt: cached.updatedAt, loading: false, refreshing: false, error: '' }
      : { cacheKey, configId, symbol, data: undefined, updatedAt: undefined, loading: Boolean(configId), refreshing: false, error: '' };
  });

  useEffect(() => {
    if (!configId) return undefined;
    let cancelled = false;
    const cached = CHART_CACHE.get(cacheKey);
    const refreshChanged = lastRefreshRef.current.cacheKey === cacheKey
      && lastRefreshRef.current.refreshKey !== refreshKey;
    lastRefreshRef.current = { cacheKey, refreshKey };

    if (cached) {
      setState({
        cacheKey,
        configId,
        symbol,
        data: cached.data,
        updatedAt: cached.updatedAt,
        loading: false,
        refreshing: !CHART_CACHE.isFresh(cached) || refreshChanged,
        error: '',
      });
      if (CHART_CACHE.isFresh(cached) && !refreshChanged) {
        return undefined;
      }
    } else {
      setState(previous => {
        const sameMarket = previous.configId === configId && previous.symbol === symbol;
        return {
          cacheKey,
          configId,
          symbol,
          data: sameMarket ? previous.data : undefined,
          updatedAt: sameMarket ? previous.updatedAt : undefined,
          loading: true,
          refreshing: Boolean(sameMarket && previous.data),
          error: '',
        };
      });
    }

    fetchChartSection(configId, timeframe, symbol)
      .then((entry) => {
        if (cancelled) return;
        setState({
          cacheKey,
          configId,
          symbol,
          data: entry.data,
          updatedAt: entry.updatedAt,
          loading: false,
          refreshing: false,
          error: '',
        });
      })
      .catch((error) => {
        if (cancelled) return;
        setState(previous => {
          const sameMarket = previous.configId === configId && previous.symbol === symbol;
          return {
            cacheKey,
            configId,
            symbol,
            data: sameMarket ? previous.data : undefined,
            updatedAt: sameMarket ? previous.updatedAt : undefined,
            loading: false,
            refreshing: false,
            error: error.message,
          };
        });
      });

    return () => { cancelled = true; };
  }, [configId, cacheKey, refreshKey, timeframe, symbol]);

  const cachedNow = configId ? CHART_CACHE.get(cacheKey) : null;
  if (state.cacheKey !== cacheKey) {
    if (cachedNow) {
      return {
        data: cachedNow.data,
        updatedAt: cachedNow.updatedAt,
        loading: false,
        refreshing: !CHART_CACHE.isFresh(cachedNow),
        error: '',
      };
    }
    const sameMarket = state.configId === configId && state.symbol === symbol;
    return {
      data: sameMarket ? state.data : undefined,
      updatedAt: sameMarket ? state.updatedAt : undefined,
      loading: Boolean(configId),
      refreshing: Boolean(sameMarket && state.data),
      error: '',
    };
  }
  return state;
}

export function useWorkspaceSections(configId, revision, timeframe, symbol, marketRefresh) {
  const context = useSection(configId, 'context', String(revision), '{}');
  const account = useSection(configId, 'account', `${revision}:${marketRefresh}`, '{}');
  const chart = useChartSection(configId, `${revision}:${marketRefresh}`, timeframe, symbol);
  return { workspace: { ...context.data, position: account.data?.position, kline: chart.data?.kline,
    timeframe: chart.data?.timeframe, market_timeframes: context.data?.market_timeframes,
    sections: { context, account, chart }, refreshKey: revision }, context, account, chart };
}
