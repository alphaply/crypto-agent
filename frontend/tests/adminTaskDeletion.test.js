import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../src/pages/AdminPage.jsx', import.meta.url), 'utf8');
const handler = source.slice(source.indexOf('  const deleteAgentData = async'), source.indexOf('  // --- Provider CRUD ---'));

function fixture(overrides = {}) {
  const events = [];
  const payload = { agents: [{ config_id: 'a/b' }, { config_id: 'keep' }], globals: { draft: true } };
  const context = {
    taskSaving: false, editingTaskId: 'a/b', locale: 'zh',
    deletingTaskRef: { current: false }, autosaveTimerRef: { current: 1 },
    payloadRef: { current: payload }, revisionRef: { current: 2 }, lastSavedRevisionRef: { current: 1 },
    saveQueueRef: { current: Promise.resolve() },
    window: { clearTimeout: () => events.push('cancel-timer') },
    api: { delete: async (url) => events.push(url) },
    message: { success: () => events.push('success'), error: () => events.push('error') },
    setDeletingTaskId: () => {}, setError: (error) => { context.error = error; },
    setPayload: (next) => { context.payload = next; },
    setPersistedAgents: (update) => { context.persisted = update(payload.agents); },
    setSaveState: (state) => { context.saveState = state; },
    setTaskDrawerOpen: (open) => { context.drawerOpen = open; },
    setEditingTask: () => {}, setEditingTaskId: () => {},
    ...overrides,
  };
  vm.createContext(context);
  vm.runInContext(`${handler}\nglobalThis.remove = deleteAgentData;`, context);
  return { context, events, payload };
}

test('deletion waits for pending saves and prevents duplicate requests', async () => {
  let finishSave;
  const pending = new Promise((resolve) => { finishSave = resolve; });
  const { context, events } = fixture({ saveQueueRef: { current: pending } });
  const deletion = context.remove('a/b');
  await context.remove('a/b');
  assert.deepEqual(events, ['cancel-timer']);
  finishSave();
  await deletion;
  assert.deepEqual(events, ['cancel-timer', '/config/a%2Fb', 'success']);
  assert.equal(context.payloadRef.current.agents.length, 1);
  assert.equal(context.payloadRef.current.agents[0].config_id, 'keep');
  assert.equal(context.payloadRef.current.globals.draft, true);
  assert.equal(context.saveState, 'unsaved');
  assert.equal(context.drawerOpen, false);
  assert.equal(context.deletingTaskRef.current, false);
});

test('failed deletion keeps the task and exposes the server error', async () => {
  const { context, events, payload } = fixture({ api: { delete: async () => { throw new Error('409: pending orders'); } } });
  await context.remove('a/b');
  assert.equal(context.payloadRef.current, payload);
  assert.match(context.error, /409: pending orders/);
  assert.ok(events.includes('error'));
  assert.ok(!events.includes('success'));
  assert.equal(context.drawerOpen, undefined);
  assert.equal(context.deletingTaskRef.current, false);
});

test('deletion of a saved task does not schedule another full configuration save', async () => {
  const { context } = fixture({ lastSavedRevisionRef: { current: 2 } });
  await context.remove('a/b');
  assert.equal(context.saveState, 'saved');
  assert.equal(context.lastSavedRevisionRef.current, context.revisionRef.current);
});
