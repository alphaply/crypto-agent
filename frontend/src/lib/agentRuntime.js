// One view of scheduler state for the directory, filters and workspace.
export function queuedRunCount(agent) {
  const count = agent?.runtime?.queued_count;
  if (Number.isInteger(count) && count >= 0) return count;
  return agent?.execution?.status === 'QUEUED' ? 1 : 0;
}

export function currentExecution(agent) {
  return agent?.runtime?.running || agent?.execution || null;
}

export function agentRuntimeStatus(agent) {
  const execution = currentExecution(agent);
  if (execution?.status === 'RUNNING') return 'RUNNING';
  if (queuedRunCount(agent) > 0) return 'QUEUED';
  if (agent?.enabled === false) return 'PAUSED';
  if (['FAILED', 'INTERRUPTED'].includes(execution?.status)) return execution.status;
  return 'SCHEDULED';
}

export function isAgentActive(agent) {
  return ['RUNNING', 'QUEUED'].includes(agentRuntimeStatus(agent));
}
