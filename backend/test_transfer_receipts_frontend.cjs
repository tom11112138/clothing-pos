const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, 'static/index.html'), 'utf8');
const script = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));
let sequence = 0;

function fixtureBatch() {
  return {
    id: 1, transfer_no: 'TEST-BATCH', status: 'shipped',
    items: [{ id: 10, qty: 10, received_qty: 0, rejected_qty: 0, remaining_qty: 10 }],
  };
}

function response(status = 200, body = { ok: true, duplicate: false }) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function harness({ storage = new Map(), userId = 1, writeFailure = false } = {}) {
  let config;
  const requests = [];
  const flashes = [];
  let reloads = 0;
  const network = { send: async () => response() };
  const context = {
    Vue: { createApp(value) { config = value; return { mount() {} }; } },
    document: { cookie: '' },
    localStorage: { getItem() { return null; }, removeItem() {}, setItem() {} },
    sessionStorage: {
      getItem(key) { return storage.get(key) ?? null; },
      setItem(key, value) {
        if (writeFailure) throw new Error('Storage unavailable');
        storage.set(key, value);
      },
      removeItem(key) { storage.delete(key); },
    },
    crypto: { randomUUID() { return `test-request-${++sequence}`; } },
    setTimeout() {},
    fetch(url, options) {
      requests.push({ url, body: JSON.parse(options.body) });
      return network.send();
    },
  };
  vm.runInNewContext(script, context);
  const state = config.data();
  for (const [name, method] of Object.entries(config.methods)) state[name] = method.bind(state);
  Object.defineProperty(state, 'isManager', { get: () => state.me?.role === 'manager' });
  state.me = { id: userId, role: 'manager' };
  state.flash = message => flashes.push(message);
  state.loadStock = () => { reloads += 1; };
  return { state, storage, network, requests, flashes, reloads: () => reloads };
}

test('lost response, close and reopen all retain the same immutable receipt', async () => {
  const app = harness();
  app.state.openBatchReceive(fixtureBatch());
  app.state.batchReceiveValues[10].received_qty = 3;
  let rejectPending;
  app.network.send = () => new Promise((resolve, reject) => { rejectPending = reject; });
  const pending = app.state.submitBatchReceive();
  await app.state.submitBatchReceive();
  app.state.closeBatchReceive();
  assert.equal(app.requests.length, 1);
  assert.ok(app.state.batchReceive);
  assert.equal(app.storage.size, 1);
  rejectPending(new Error('Response lost'));
  await pending;
  assert.ok(app.state.batchReceiveRequest);
  app.state.closeBatchReceive();
  app.state.openBatchReceive(fixtureBatch());
  assert.equal(app.state.batchReceiveValues[10].received_qty, 3);
  app.state.batchReceiveValues[10].received_qty = 8;
  app.network.send = async () => response(200, { ok: true, duplicate: true });
  await app.state.submitBatchReceive();
  assert.deepEqual(app.requests[1], app.requests[0]);
  assert.equal(app.state.batchReceive, null);
  assert.equal(app.storage.size, 0);
  assert.equal(app.reloads(), 1);
});

test('reload recovers a pending receipt even when the batch is now completed', async () => {
  const first = harness();
  first.state.openBatchReceive(fixtureBatch());
  first.state.batchReceiveValues[10].received_qty = 10;
  first.network.send = async () => { throw new Error('Response lost'); };
  await first.state.submitBatchReceive();
  const restored = harness({ storage: first.storage });
  const completed = fixtureBatch();
  completed.status = 'received';
  completed.items[0].received_qty = 10;
  completed.items[0].remaining_qty = 0;
  assert.equal(restored.state.hasPendingBatchReceive(1), true);
  restored.state.openBatchReceive(completed);
  restored.network.send = async () => response(200, { ok: true, duplicate: true });
  await restored.state.submitBatchReceive();
  assert.deepEqual(restored.requests[0], first.requests[0]);
  assert.equal(restored.storage.size, 0);
});

test('confirmed validation failure allows correction with a new request ID', async () => {
  const app = harness();
  app.state.openBatchReceive(fixtureBatch());
  app.state.batchReceiveValues[10].received_qty = 3;
  app.network.send = async () => response(400, { detail: 'Remaining quantity changed' });
  await app.state.submitBatchReceive();
  assert.equal(app.state.batchReceiveRequest, null);
  assert.equal(app.storage.size, 0);
  app.state.batchReceiveValues[10].received_qty = 2;
  app.network.send = async () => response();
  await app.state.submitBatchReceive();
  assert.notEqual(app.requests[0].body.client_request_id, app.requests[1].body.client_request_id);
  assert.equal(app.requests[1].body.items[0].received_qty, 2);
});

test('unknown server failure retains the original request', async () => {
  const app = harness();
  app.state.openBatchReceive(fixtureBatch());
  app.state.batchReceiveValues[10].received_qty = 3;
  app.network.send = async () => response(500, { detail: 'Server unavailable' });
  await app.state.submitBatchReceive();
  const requestId = app.state.batchReceiveRequest.body.client_request_id;
  app.network.send = async () => response();
  await app.state.submitBatchReceive();
  assert.equal(app.requests[1].body.client_request_id, requestId);
});

test('a new arrival receives a new request ID after confirmation', async () => {
  const app = harness();
  app.state.openBatchReceive(fixtureBatch());
  app.state.batchReceiveValues[10].received_qty = 3;
  await app.state.submitBatchReceive();
  const nextBatch = fixtureBatch();
  nextBatch.items[0].received_qty = 3;
  nextBatch.items[0].remaining_qty = 7;
  app.state.openBatchReceive(nextBatch);
  app.state.batchReceiveValues[10].received_qty = 3;
  await app.state.submitBatchReceive();
  assert.notEqual(app.requests[0].body.client_request_id, app.requests[1].body.client_request_id);
});

test('storage failure prevents submission', async () => {
  const app = harness({ writeFailure: true });
  app.state.openBatchReceive(fixtureBatch());
  app.state.batchReceiveValues[10].received_qty = 3;
  await app.state.submitBatchReceive();
  assert.equal(app.requests.length, 0);
  assert.equal(app.state.batchReceiveRequest, null);
  assert.ok(app.flashes.length);
});

test('another account cannot resume the old accounts receipt', async () => {
  const app = harness();
  app.state.openBatchReceive(fixtureBatch());
  app.state.batchReceiveValues[10].received_qty = 3;
  app.network.send = async () => { throw new Error('Response lost'); };
  await app.state.submitBatchReceive();
  app.state.me = { id: 2, role: 'manager' };
  await app.state.submitBatchReceive();
  assert.equal(app.requests.length, 1);
  const other = harness({ storage: app.storage, userId: 2 });
  assert.equal(other.state.hasPendingBatchReceive(1), false);
  other.state.openBatchReceive(fixtureBatch());
  assert.equal(other.state.batchReceiveRequest, null);
});

test('invalid quantities never reach the server', async () => {
  const app = harness();
  app.state.openBatchReceive(fixtureBatch());
  for (const qty of [-1, 0.5, 11, NaN, Infinity]) {
    app.state.batchReceiveValues[10].received_qty = qty;
    await app.state.submitBatchReceive();
  }
  assert.equal(app.requests.length, 0);
  assert.equal(app.storage.size, 0);
});
