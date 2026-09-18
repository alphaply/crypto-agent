import test from 'node:test';
import assert from 'node:assert/strict';
import { createSseParser } from '../src/lib/sse.js';

test('SSE decodes CRLF split at every character and ignores keepalives', () => {
  const events = [];
  const consume = createSseParser((event) => events.push(event));
  for (const char of ': ping\r\n\r\ndata: {"type":"token","token":"中文"}\r\n\r\ndata: {"type":"done"}\r\n\r\n') consume(char);
  assert.deepEqual(events, [{ type: 'token', token: '中文' }, { type: 'done' }]);
});

test('SSE supports multiline JSON and an unterminated final frame', () => {
  const events = [];
  const consume = createSseParser((event) => events.push(event));
  consume('data: {"type":\ndata: "status"}\n\ndata: {"type":"done"}', true);
  assert.deepEqual(events, [{ type: 'status' }, { type: 'done' }]);
});
