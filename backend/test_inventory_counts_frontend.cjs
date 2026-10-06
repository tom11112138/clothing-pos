const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, 'static/index.html'), 'utf8');
const script = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));
const vueContext = vm.createContext({ console });
vm.runInContext(fs.readFileSync(path.join(__dirname, 'static/vendor/vue.global.prod.js'), 'utf8'), vueContext);
const vue = vueContext.Vue;

function sheet(id = 1, version = 0, status = 'draft') {
  return { id, count_no: `COUNT-${id}`, version, status, items: [
    { id: id * 10, system_qty: 10, actual_qty: 8 },
    { id: id * 10 + 1, system_qty: 10, actual_qty: 12 },
  ] };
}

function response(status = 200, body = { ok: true, count: sheet(1, 1, 'completed') }) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness() {
  let config;
  const requests = [], flashes = [];
  const network = { send: async () => response() };
  vm.runInNewContext(script, {
    Vue: { createApp(value) { config = value; return { mount() {} }; } },
    document: { cookie: '' },
    localStorage: { getItem() { return null; }, removeItem() {}, setItem() {} },
    setTimeout() {},
    fetch(url, options) {
      const request = { url, method: options.method || 'GET', body: options.body && JSON.parse(options.body) };
      requests.push(request);
      return network.send(request);
    },
  });
  const state = vue.reactive(config.data());
  for (const [name, method] of Object.entries(config.methods)) state[name] = method.bind(state);
  Object.defineProperty(state, 'isManager', { get: () => state.me?.role === 'manager' });
  state.flash = message => flashes.push(message);
  state.loadStock = () => {};
  state.loadCounts = () => {};
  for (const [name, definition] of Object.entries(config.watch)) {
    const handler = typeof definition === 'function' ? definition : definition.handler;
    vue.watch(() => state[name], handler.bind(state), { flush: definition.flush || 'sync' });
  }
  state.me = { id: 1, role: 'manager' };
  state.openCountData(sheet());
  return { state, requests, flashes, network };
}

test('completion sends one immutable whole-sheet request with the displayed version', async () => {
  const app = harness();
  const pending = deferred();
  app.network.send = () => pending.promise;
  const saving = app.state.completeInventoryCount();
  await app.state.completeInventoryCount();
  await app.state.openInventoryCount(2);
  app.state.closeInventoryCount();
  app.state.cancelInventoryCount(sheet());
  app.state.countActual[10] = 99;
  assert.equal(app.requests.length, 1);
  assert.equal(app.state.countDetail.id, 1);
  assert.equal(app.state.countSubmitting, true);
  assert.equal(app.state.confirmBox, null);
  assert.deepEqual(app.requests[0], {
    url: '/api/stock/counts/1/complete', method: 'POST',
    body: { expected_version: 0, items: [{ item_id: 10, actual_qty: 8 }, { item_id: 11, actual_qty: 12 }] },
  });
  pending.resolve(response());
  await saving;
  assert.equal(app.state.countDetail.status, 'completed');
  assert.equal(app.state.countSubmitting, false);
  await app.state.completeInventoryCount();
  assert.equal(app.requests.length, 1);
});

test('missing, fractional and negative actuals are rejected before any request', async () => {
  const app = harness();
  for (const value of [null, undefined, '', -1, 1.5, NaN, Infinity]) {
    app.state.countActual[10] = value;
    await app.state.completeInventoryCount();
  }
  assert.equal(app.requests.length, 0);
  app.state.countActual[10] = 0;
  await app.state.completeInventoryCount();
  assert.equal(app.requests[0].body.items[0].actual_qty, 0);
});

test('version conflict keeps local values and requires explicit reload before resubmission', async () => {
  const app = harness();
  app.network.send = async () => response(409, { detail: 'Sheet changed' });
  await app.state.completeInventoryCount();
  assert.equal(app.state.countActual[10], 8);
  assert.equal(app.state.countReloadRequired, true);
  assert.equal(app.state.countError, 'Sheet changed');
  await app.state.completeInventoryCount();
  assert.equal(app.requests.length, 1);
  app.state.reloadInventoryCount();
  assert.equal(app.requests.length, 1);
  const latest = sheet(1, 2);
  latest.items[0].actual_qty = 6;
  app.network.send = async () => response(200, latest);
  await app.state.confirmBox.onOk();
  assert.equal(app.state.countActual[10], 6);
  assert.equal(app.state.countDetail.version, 2);
  assert.equal(app.state.countReloadRequired, false);
  assert.equal(app.state.countError, '');
  app.network.send = async () => response();
  await app.state.completeInventoryCount();
  assert.equal(app.requests.at(-1).body.expected_version, 2);
});

test('an unknown result preserves entered values and retries the same unchanged sheet', async () => {
  const app = harness();
  app.network.send = async () => { throw new Error('Lost response'); };
  await app.state.completeInventoryCount();
  assert.equal(app.state.countActual[10], 8);
  assert.equal(app.state.countSubmitting, false);
  assert.ok(app.state.countError.includes('Lost response'));
  app.network.send = async () => response();
  await app.state.completeInventoryCount();
  assert.deepEqual(app.requests[0], app.requests[1]);
});

test('a previous accounts completion cannot overwrite or unlock a new accounts submission', async () => {
  const app = harness();
  const old = deferred();
  app.network.send = () => old.promise;
  const savingOld = app.state.completeInventoryCount();
  app.state.me = null;
  app.state.me = { id: 2, role: 'manager' };
  assert.equal(app.state.countDetail, null);
  app.state.openCountData(sheet(2));
  const current = deferred();
  app.network.send = () => current.promise;
  const savingCurrent = app.state.completeInventoryCount();
  old.resolve(response());
  await savingOld;
  assert.equal(app.state.countDetail.id, 2);
  assert.equal(app.state.countSubmitting, true);
  assert.equal(app.flashes.length, 0);
  current.resolve(response(200, { ok: true, count: sheet(2, 1, 'completed') }));
  await savingCurrent;
  assert.equal(app.state.countSubmitting, false);
  assert.equal(app.state.countDetail.status, 'completed');
});

test('late load results do not replace a more recently opened sheet', async () => {
  const app = harness();
  const first = deferred(), second = deferred();
  app.network.send = () => first.promise;
  const loadingFirst = app.state.openInventoryCount(2);
  app.network.send = () => second.promise;
  const loadingSecond = app.state.openInventoryCount(3);
  second.resolve(response(200, sheet(3)));
  await loadingSecond;
  first.resolve(response(200, sheet(2)));
  await loadingFirst;
  assert.equal(app.state.countDetail.id, 3);
  assert.equal(app.state.countLoading, false);
});

test('closing while loading prevents the late response reopening the modal', async () => {
  const app = harness();
  const pending = deferred();
  app.network.send = () => pending.promise;
  const loading = app.state.openInventoryCount(2);
  app.state.closeInventoryCount();
  pending.resolve(response(200, sheet(2)));
  await loading;
  assert.equal(app.state.countDetail, null);
  assert.equal(app.state.countLoading, false);
});

test('cancellation uses the confirmed version and blocks completion while awaiting its response', async () => {
  const app = harness();
  const count = sheet(1, 3);
  app.state.cancelInventoryCount(count);
  count.version = 4;
  const pending = deferred();
  app.network.send = () => pending.promise;
  const cancelling = app.state.confirmBox.onOk();
  await app.state.completeInventoryCount();
  await app.state.confirmBox.onOk();
  assert.equal(app.requests.length, 1);
  assert.equal(app.requests[0].body.expected_version, 3);
  pending.resolve(response(200, { ok: true, count: sheet(1, 4, 'cancelled') }));
  await cancelling;
  assert.equal(app.state.countDetail.status, 'cancelled');
  assert.equal(app.state.countSubmitting, false);
});

test('stale cancellation confirmation cannot be executed by another login', async () => {
  const app = harness();
  app.state.cancelInventoryCount(sheet());
  const confirm = app.state.confirmBox.onOk;
  app.state.me = { id: 2, role: 'manager' };
  await confirm();
  assert.equal(app.requests.length, 0);
});

test('failed cancellation exposes its conflict on the open sheet', async () => {
  const app = harness();
  app.state.cancelInventoryCount(sheet());
  app.network.send = async () => response(409, { detail: 'Already completed' });
  await app.state.confirmBox.onOk();
  assert.equal(app.state.countReloadRequired, true);
  assert.equal(app.state.countError, 'Already completed');
  assert.equal(app.state.countSubmitting, false);
});

test('creation cannot be double clicked', async () => {
  const app = harness();
  const pending = deferred();
  app.network.send = () => pending.promise;
  const creating = app.state.startInventoryCount();
  await app.state.startInventoryCount();
  assert.equal(app.requests.length, 1);
  pending.resolve(response(200, { ok: true, count: sheet(2) }));
  await creating;
  assert.equal(app.state.countDetail.id, 2);
  assert.equal(app.state.countLoading, false);
});

test('count modal controls are disabled during submission or a stale revision', () => {
  assert.match(html, /@click.self="closeInventoryCount"/);
  assert.match(html, /v-model.number="countActual\[item.id\]" :disabled="countDetail.status!=='draft' \|\| countSubmitting \|\| countLoading \|\| countReloadRequired"/);
  assert.match(html, /@click="completeInventoryCount" :disabled="countSubmitting \|\| countLoading \|\| countReloadRequired"/);
  assert.doesNotMatch(script, /api\('\/api\/stock\/counts\/'.*\/items\//);
});
