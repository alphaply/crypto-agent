import test from 'node:test';
import assert from 'node:assert/strict';
import { agentRuntimeStatus, currentExecution, isAgentActive, queuedRunCount } from '../src/lib/agentRuntime.js';

test('pending runs never hide a currently running decision', () => {
  const running = { status: 'RUNNING', reasoning_content: 'current decision' };
  const agent = { execution: { status: 'CANCELLED' }, runtime: { running, queued_count: 3 } };
  assert.equal(currentExecution(agent), running);
  assert.equal(agentRuntimeStatus(agent), 'RUNNING');
  assert.equal(queuedRunCount(agent), 3);
});

test('waiting runs are visible even after the previous run finished', () => {
  const agent = { execution: { status: 'FINISHED' }, runtime: { running: null, queued_count: 2 } };
  assert.equal(agentRuntimeStatus(agent), 'QUEUED');
  assert.equal(isAgentActive(agent), true);
});

test('paused and interrupted states do not look scheduled', () => {
  assert.equal(agentRuntimeStatus({ enabled: false, runtime: { queued_count: 0 } }), 'PAUSED');
  assert.equal(agentRuntimeStatus({ execution: { status: 'INTERRUPTED' } }), 'INTERRUPTED');
  assert.equal(isAgentActive({ enabled: false }), false);
});

test('historical payloads still express their running state', () => {
  assert.equal(agentRuntimeStatus({ execution: { status: 'QUEUED' } }), 'QUEUED');
  assert.equal(queuedRunCount({ execution: { status: 'QUEUED' } }), 1);
  assert.equal(queuedRunCount({ execution: { status: 'FINISHED' } }), 0);
});
