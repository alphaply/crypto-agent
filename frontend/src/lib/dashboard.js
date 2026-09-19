// Polling execution tokens should not restart market-data requests or charts.
export function workspaceSignature(agents = []) {
  return JSON.stringify(agents.map((agent) => [agent.config_id, agent.timestamp, agent.market_timeframes]));
}

export function selectDashboardTab(agents = [], requested) {
  if (requested === 'compare') return 'compare';
  if (agents.some((agent) => agent.config_id === requested)) return requested;
  return agents.length === 1 ? agents[0].config_id : 'compare';
}
