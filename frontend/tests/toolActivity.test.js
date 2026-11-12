import test from 'node:test';
import assert from 'node:assert/strict';
import { pendingDecisionTools, updateToolActivity } from '../src/lib/toolActivity.js';

test('trading calls stay visible until their own receipt arrives', () => {
  const calls = [{ id: 'a', name: 'cancel_order' }, { id: 'b', name: 'open_position' }];
  const pending = pendingDecisionTools(calls, [{ role: 'tool', tool_call_id: 'a' }], [{ id: 'b', status: 'running' }], { active: true });
  assert.deepEqual(pending, [{ ...calls[1], status: 'running' }]);
  assert.equal(pendingDecisionTools(calls, [], [], { active: false })[0].status, 'unknown');
});

test('partial tool names are merged, and only execution events indicate running', () => {
  let calls = updateToolActivity([], { type: 'tool_calls', tool_calls: [{ index: 0, id: 'a', name: 'cancel_order' }] });
  calls = updateToolActivity(calls, { type: 'tool_calls', tool_calls: [{ index: 0, id: null, name: null }] });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].name, 'cancel_order');
  assert.equal(calls[0].status, 'preparing');
  calls = updateToolActivity(calls, { type: 'model_complete' });
  assert.equal(calls[0].status, 'planned');
  calls = updateToolActivity(calls, { type: 'tool_start', tool_call_id: 'a', tool: 'cancel_order' });
  assert.equal(calls[0].status, 'running');
  assert.equal(updateToolActivity(calls, { type: 'tool_result', tool_call_id: 'other' }).length, 1);
  assert.deepEqual(updateToolActivity(calls, { type: 'tool_result', tool_call_id: 'a' }), []);
});
