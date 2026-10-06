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
let sequence = 0;

function response(status = 200, body = { order_no: 'TEST-SALE', total: 10 }) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function sku(id = 1) {
  return { id, barcode: `SKU-${id}`, price: 10, qty: 1 };
}

function harness() {
  let config;
  let focused = 0;
  const requests = [];
  const flashes = [];
  const network = { send: async () => response() };
  vm.runInNewContext(script, {
    Vue: { createApp(value) { config = value; return { mount() {} }; } },
    document: { cookie: '', addEventListener() {}, removeEventListener() {} },
    localStorage: { getItem() { return null; }, removeItem() {}, setItem() {} },
    crypto: { randomUUID() { return `checkout-test-${++sequence}`; } },
    setTimeout() {},
    fetch(url, options) {
      const request = { url, method: options.method || 'GET', body: options.body && JSON.parse(options.body) };
      requests.push(request);
      return network.send(request);
    },
  });
  const state = vue.reactive(config.data());
  for (const [name, method] of Object.entries(config.methods)) state[name] = method.bind(state);
  for (const [name, getter] of Object.entries(config.computed)) {
    const value = vue.computed(getter.bind(state));
    Object.defineProperty(state, name, { get: () => value.value });
  }
  state.$refs = { scan: { focus() { focused += 1; } } };
  state.$nextTick = callback => callback();
  state.flash = message => flashes.push(message);
  state.loadStock = () => {};
  for (const [name, definition] of Object.entries(config.watch)) {
    const handler = typeof definition === 'function' ? definition : definition.handler;
    vue.watch(() => state[name], handler.bind(state), { flush: definition.flush || 'sync' });
  }
  const fetchMe = state.fetchMe;
  state.fetchMe = () => {};
  config.mounted.call(state);
  state.fetchMe = fetchMe;
  state.me = { id: 1, role: 'manager' };
  state.tab = 'pos';
  return { state, network, requests, flashes, focused: () => focused };
}

function scan(app, code = 'SKU-1') {
  app.state.scanInput = code;
  return app.state.scan();
}

test('checkout waits for all barcode lookups and includes every scanned item', async () => {
  const app = harness();
  app.state.cart = [sku(1)];
  const lookup = deferred();
  app.network.send = () => lookup.promise;
  const scanning = scan(app, 'SKU-2');
  assert.equal(app.state.pendingScans, 1);
  await app.state.checkout();
  assert.equal(app.requests.length, 1);
  assert.equal(app.state.checkingOut, false);
  lookup.resolve(response(200, sku(2)));
  await scanning;
  assert.equal(app.state.pendingScans, 0);
  assert.equal(app.state.cart.length, 2);
  app.network.send = async () => response();
  await app.state.checkout();
  assert.deepEqual(app.requests[1].body.items, [{ sku_id: 1, qty: 1 }, { sku_id: 2, qty: 1 }]);
  assert.equal(app.state.cart.length, 0);
});

test('out-of-order barcode replies merge quantities without releasing the checkout guard early', async () => {
  const app = harness();
  const first = deferred();
  const second = deferred();
  app.network.send = () => first.promise;
  const scanOne = scan(app);
  app.network.send = () => second.promise;
  const scanTwo = scan(app);
  assert.equal(app.state.pendingScans, 2);
  second.resolve(response(200, sku()));
  await scanTwo;
  await app.state.checkout();
  assert.equal(app.requests.length, 2);
  first.resolve(response(200, sku()));
  await scanOne;
  assert.equal(app.state.pendingScans, 0);
  assert.equal(app.state.cart[0].qty, 2);
});

test('lookup failures release their guard and keep existing cart items', async () => {
  const app = harness();
  app.state.cart = [sku()];
  app.network.send = async () => { throw new Error('Lookup unavailable'); };
  await scan(app);
  assert.equal(app.state.pendingScans, 0);
  assert.equal(app.state.cart.length, 1);
  assert.equal(app.flashes.at(-1), 'Lookup unavailable');
  app.network.send = async () => response();
  await app.state.checkout();
  assert.equal(app.requests.at(-1).url, '/api/sales');
});

test('an in-flight checkout blocks scans, edits, clear, store changes, logout and duplicate submission', async () => {
  const app = harness();
  app.state.cart = [sku()];
  const originalStore = app.state.currentStore;
  const sale = deferred();
  app.network.send = () => sale.promise;
  const checking = app.state.checkout();
  assert.equal(app.state.checkingOut, true);
  const event = { target: { value: '5' } };
  app.state.setCartQty(1, event);
  app.state.removeCartItem(1);
  app.state.clearCart();
  app.state.selectCurrentStore(app.state.stores[1]);
  await app.state.logout();
  await scan(app, 'SKU-2');
  await app.state.checkout();
  assert.equal(event.target.value, 1);
  assert.equal(app.state.cart[0].qty, 1);
  assert.equal(app.state.currentStore, originalStore);
  assert.equal(app.state.me.id, 1);
  assert.equal(app.requests.length, 1);
  sale.resolve(response());
  await checking;
  assert.equal(app.state.cart.length, 0);
  assert.equal(app.state.checkingOut, false);
});

test('a nonempty cart cannot silently move to another store', () => {
  const app = harness();
  app.state.cart = [sku()];
  const store = app.state.currentStore;
  app.state.selectCurrentStore(app.state.stores[1]);
  assert.equal(app.state.currentStore, store);
  assert.equal(app.state.cart.length, 1);
  app.state.clearCart();
  app.state.selectCurrentStore(app.state.stores[1]);
  assert.equal(app.state.currentStore, app.state.stores[1]);
});

test('clear invalidates old lookups even after leaving and returning to the same store', async () => {
  const app = harness();
  const lookup = deferred();
  app.network.send = () => lookup.promise;
  const scanning = scan(app);
  app.state.clearCart();
  app.state.selectCurrentStore(app.state.stores[1]);
  app.state.selectCurrentStore(app.state.stores[0]);
  lookup.resolve(response(200, sku()));
  await scanning;
  assert.equal(app.state.cart.length, 0);
  assert.equal(app.state.pendingScans, 0);
});

test('an old barcode reply cannot add items to a new account or decrement its pending counter', async () => {
  const app = harness();
  const oldLookup = deferred();
  app.network.send = () => oldLookup.promise;
  const oldScan = scan(app);
  app.state.me = null;
  app.state.me = { id: 2, role: 'manager' };
  const newLookup = deferred();
  app.network.send = () => newLookup.promise;
  const newScan = scan(app, 'SKU-2');
  oldLookup.resolve(response(200, sku()));
  await oldScan;
  assert.equal(app.state.cart.length, 0);
  assert.equal(app.state.pendingScans, 1);
  newLookup.resolve(response(200, sku(2)));
  await newScan;
  assert.equal(app.state.cart[0].id, 2);
  assert.equal(app.state.pendingScans, 0);
});

test('direct account and store changes synchronously reset cart context', () => {
  const app = harness();
  app.state.cart = [sku()];
  app.state.scanInput = 'PARTIAL';
  app.state.checkoutRequestId = 'OLD-REQUEST';
  app.state.currentStore = app.state.stores[1];
  assert.equal(app.state.cart.length, 0);
  assert.equal(app.state.scanInput, '');
  assert.equal(app.state.checkoutRequestId, '');
  app.state.cart = [sku()];
  app.state.me = { id: 2, role: 'manager' };
  assert.equal(app.state.cart.length, 0);
});

test('logout clears cart before its network request finishes and prevents overlapping login', async () => {
  const app = harness();
  app.state.cart = [sku()];
  const exiting = deferred();
  app.network.send = () => exiting.promise;
  const logout = app.state.logout();
  assert.equal(app.state.me, null);
  assert.equal(app.state.cart.length, 0);
  assert.equal(app.state.loggingOut, true);
  app.state.loginForm = { username: 'test', password: 'test-only' };
  await app.state.login();
  await scan(app);
  await app.state.checkout();
  assert.equal(app.requests.length, 1);
  exiting.resolve(response());
  await logout;
  assert.equal(app.state.loggingOut, false);
});

test('an expired current session clears checkout state', async () => {
  const app = harness();
  app.state.cart = [sku()];
  app.state.checkoutRequestId = 'OLD-REQUEST';
  app.network.send = async () => response(401);
  await scan(app);
  assert.equal(app.state.me, null);
  assert.equal(app.state.cart.length, 0);
  assert.equal(app.state.pendingScans, 0);
  assert.equal(app.state.checkoutRequestId, '');
});

test('a delayed unauthorized response from the previous session does not log out the new account', async () => {
  const app = harness();
  const lookup = deferred();
  app.network.send = () => lookup.promise;
  const scanning = scan(app);
  app.state.me = { id: 2, role: 'manager' };
  app.state.cart = [sku(2)];
  lookup.resolve(response(401));
  await scanning;
  assert.equal(app.state.me.id, 2);
  assert.equal(app.state.cart[0].id, 2);
  assert.equal(app.flashes.length, 0);
});

test('a late checkout success cannot clear or unlock a new sessions checkout', async () => {
  const app = harness();
  app.state.cart = [sku()];
  const oldSale = deferred();
  app.network.send = () => oldSale.promise;
  const oldCheckout = app.state.checkout();
  app.state.me = null;
  app.state.me = { id: 2, role: 'manager' };
  app.state.cart = [sku(2)];
  const newSale = deferred();
  app.network.send = () => newSale.promise;
  const newCheckout = app.state.checkout();
  const requestId = app.state.checkoutRequestId;
  oldSale.resolve(response());
  await oldCheckout;
  assert.equal(app.state.cart[0].id, 2);
  assert.equal(app.state.checkingOut, true);
  assert.equal(app.state.checkoutRequestId, requestId);
  assert.equal(app.flashes.length, 0);
  newSale.resolve(response());
  await newCheckout;
  assert.equal(app.state.cart.length, 0);
});

test('navigating away while awaiting checkout does not access an unmounted scan input', async () => {
  const app = harness();
  app.state.cart = [sku()];
  const sale = deferred();
  app.network.send = () => sale.promise;
  const checking = app.state.checkout();
  app.state.tab = 'orders';
  delete app.state.$refs.scan;
  sale.resolve(response());
  await checking;
  assert.equal(app.state.cart.length, 0);
  assert.equal(app.flashes.length, 1);
  assert.equal(app.focused(), 0);
});

test('a failed checkout keeps its cart and request ID for an unchanged retry', async () => {
  const app = harness();
  app.state.cart = [sku()];
  app.network.send = async () => { throw new Error('Response lost'); };
  await app.state.checkout();
  assert.equal(app.state.checkingOut, false);
  assert.equal(app.state.cart.length, 1);
  assert.equal(app.focused(), 1);
  app.network.send = async () => response();
  await app.state.checkout();
  assert.deepEqual(app.requests[0], app.requests[1]);
});

test('unfinished barcode input and invalid quantities cannot be submitted', async () => {
  const app = harness();
  app.state.cart = [sku()];
  app.state.scanInput = 'PARTIAL';
  await app.state.checkout();
  app.state.scanInput = '';
  for (const qty of [0, -1, 1.5, NaN, Infinity, '', '2']) {
    app.state.cart[0].qty = qty;
    await app.state.checkout();
  }
  assert.equal(app.requests.length, 0);
  app.state.cart[0].qty = 2;
  const event = { target: { value: '-1' } };
  app.state.setCartQty(1, event);
  assert.equal(event.target.value, 2);
  assert.equal(app.state.cart[0].qty, 2);
});

test('a stale initial session lookup cannot overwrite a more recent login', async () => {
  const app = harness();
  const session = deferred();
  app.network.send = () => session.promise;
  const fetching = app.state.fetchMe();
  app.state.me = { id: 2, role: 'manager' };
  app.state.cart = [sku(2)];
  session.resolve(response(200, { id: 1, role: 'manager' }));
  await fetching;
  assert.equal(app.state.me.id, 2);
  assert.equal(app.state.cart[0].id, 2);
});

test('checkout inputs and commands are wired to guarded handlers', () => {
  assert.match(html, /ref="scan"[^>]+:disabled="checkingOut"/);
  assert.match(html, /@change="setCartQty\(c\.id, \$event\)" :disabled="checkingOut \|\| pendingScans > 0"/);
  assert.match(html, /@click="removeCartItem\(c\.id\)" :disabled="checkingOut \|\| pendingScans > 0"/);
  assert.match(html, /@click="checkout" :disabled="!cart\.length \|\| checkingOut \|\| pendingScans > 0"/);
  assert.doesNotMatch(html, /@click="cart(?:=|\.)/);
});
