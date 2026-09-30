import { useCallback, useState } from 'react';
import { isChartTimeframe, readChartTimeframe, saveChartTimeframe } from '../lib/dashboard';

export function useChartTimeframe() {
  const [timeframe, setTimeframe] = useState(readChartTimeframe);
  const selectTimeframe = useCallback((value) => {
    if (!isChartTimeframe(value)) return;
    setTimeframe(value);
    saveChartTimeframe(value);
  }, []);
  return [timeframe, selectTimeframe];
}
