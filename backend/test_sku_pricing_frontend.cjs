const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, 'static/index.html'), 'utf8');
const script = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));
const context = vm.createContext({console});
vm.runInContext(fs.readFileSync(path.join(__dirname, 'static/vendor/vue.global.prod.js'), 'utf8'), context);
const vue = context.Vue;
const sku = (id = 1) => ({id, barcode:`SKU-${id}`, product_name:'Test shirt', price:100, cost:40, quantity:7, version:0});
const response = (body = sku(), status = 200) => ({ok:status >= 200 && status < 300, status, json:async () => body});
function deferred() {
  let resolve;
  const promise = new Promise(yes => {resolve = yes;});
  return {promise, resolve};
}
function harness() {
  let config;
  const requests = [], flashes = [];
  const network = {send:async () => response()};
  vm.runInNewContext(script, {
    Vue:{createApp(value) {config = value; return {mount() {}};}}, URLSearchParams,
    document:{cookie:''}, localStorage:{getItem() {return null;}, setItem() {}, removeItem() {}}, setTimeout() {},
    fetch(url, options) {requests.push({url, body:options.body && JSON.parse(options.body)}); return network.send(url, options);},
  });
  const state = vue.reactive(config.data());
  for (const [name, fn] of Object.entries(config.methods)) state[name] = fn.bind(state);
  for (const [name, fn] of Object.entries(config.computed)) {
    const getter = vue.computed(fn.bind(state));
    Object.defineProperty(state, name, {get:() => getter.value});
  }
  state.loadStock = () => {};
  state.flash = value => flashes.push(value);
  for (const [name, definition] of Object.entries(config.watch)) {
    vue.watch(() => state[name], (typeof definition === 'function' ? definition : definition.handler).bind(state), {flush:definition.flush || 'sync'});
  }
  state.me = {id:1, role:'manager'};
  return {state, network, requests, flashes, loadStock:config.methods.loadStock.bind(state)};
}

test('manager opens fresh prices and saves only prices plus their version', async () => {
  const app = harness();
  app.state.skus = [sku()]; app.state.cart = [{id:1, price:100, qty:2}];
  await app.state.openSkuPricing({...sku(), cost:999});
  assert.equal(app.state.skuPriceForm.cost, '40');
  app.state.skuPriceForm = {price:'129.99', cost:'49.99'};
  app.network.send = async () => response({...sku(), price:129.99, cost:49.99, version:1});
  await app.state.saveSkuPricing();
  assert.deepEqual(app.requests[1], {url:'/api/skus/1', body:{price:129.99, cost:49.99, expected_version:0}});
  assert.equal(app.state.skus[0].quantity, 7);
  assert.equal(app.state.skus[0].barcode, 'SKU-1');
  assert.equal(app.state.skus[0].cost, 49.99);
  assert.equal(app.state.cart[0].price, 129.99);
  assert.equal(app.state.skuPriceSku, null);
});

test('staff cannot open or submit a price editor', async () => {
  const app = harness();
  app.state.me = {id:2, role:'staff', store:'2号店'};
  await app.state.openSkuPricing(sku());
  app.state.skuPriceSku = sku(); app.state.skuPriceForm = {price:'10', cost:'4'};
  await app.state.saveSkuPricing();
  assert.equal(app.requests.length, 0);
});

test('unconfirmed checkout prevents opening price edits', async () => {
  const app = harness();
  app.state.checkoutAttempt = {requestId:'pending'};
  await app.state.openSkuPricing(sku());
  assert.equal(app.requests.length, 0);
  assert.equal(app.state.skuPriceSku, null);
  assert.equal(app.flashes.length, 1);
});

test('invalid amounts do not send changes while zero is valid', async () => {
  const app = harness();
  await app.state.openSkuPricing(sku());
  for (const value of ['', '-1', 'NaN', 'Infinity', '1.001', '1e2', '10000000000000000']) {
    app.state.skuPriceForm.price = value;
    await app.state.saveSkuPricing();
    assert.equal(app.requests.length, 1);
    assert.ok(app.state.skuPriceError);
  }
  app.state.skuPriceForm = {price:'0', cost:'0.00'};
  await app.state.saveSkuPricing();
  assert.equal(app.requests[1].body.price, 0);
  assert.equal(app.requests[1].body.cost, 0);
});

test('duplicate submit and close are blocked while saving', async () => {
  const app = harness(), pending = deferred();
  await app.state.openSkuPricing(sku());
  app.network.send = () => pending.promise;
  const saving = app.state.saveSkuPricing();
  app.state.closeSkuPricing();
  await app.state.saveSkuPricing();
  assert.equal(app.requests.length, 2);
  assert.equal(app.state.skuPriceSku.id, 1);
  pending.resolve(response({...sku(), version:1}));
  await saving;
  assert.equal(app.state.skuPriceSaving, false);
});

test('a conflict keeps the draft and blocks resubmission until fresh data loads', async () => {
  const app = harness();
  await app.state.openSkuPricing(sku());
  app.state.skuPriceForm.cost = '90';
  app.network.send = async () => response({detail:'Reload prices'}, 409);
  await app.state.saveSkuPricing();
  assert.equal(app.state.skuPriceForm.cost, '90');
  assert.equal(app.state.skuPriceReloadRequired, true);
  await app.state.saveSkuPricing();
  assert.equal(app.requests.length, 2);
  app.network.send = async () => response({...sku(), cost:50, version:2});
  await app.state.reloadSkuPricing();
  assert.equal(app.state.skuPriceForm.cost, '50');
  assert.equal(app.state.skuPriceVersion, 2);
  assert.equal(app.state.skuPriceReloadRequired, false);
});

test('failed load cannot save stale prices and may be reloaded', async () => {
  const app = harness();
  app.network.send = async () => response({detail:'Unavailable'}, 500);
  await app.state.openSkuPricing(sku());
  await app.state.saveSkuPricing();
  assert.equal(app.requests.length, 1);
  assert.equal(app.state.skuPriceReloadRequired, true);
  app.network.send = async () => response();
  await app.state.reloadSkuPricing();
  assert.equal(app.state.skuPriceReloadRequired, false);
});

test('switching SKU or closing discards late load results', async () => {
  const app = harness(), pending = deferred();
  app.network.send = () => pending.promise;
  const opening = app.state.openSkuPricing(sku());
  app.network.send = async () => response(sku(2));
  await app.state.openSkuPricing(sku(2));
  pending.resolve(response(sku()));
  await opening;
  assert.equal(app.state.skuPriceSku.id, 2);
  app.state.closeSkuPricing();
  assert.equal(app.state.skuPriceSku, null);
});

test('switching accounts discards a save response and resets sensitive drafts', async () => {
  const app = harness(), pending = deferred();
  app.state.skus = [sku()];
  await app.state.openSkuPricing(sku());
  app.network.send = () => pending.promise;
  const saving = app.state.saveSkuPricing();
  app.state.me = {id:2, role:'staff', store:'2号店'};
  pending.resolve(response({...sku(), price:500, cost:200, version:1}));
  await saving;
  assert.equal(app.state.skuPriceSku, null);
  assert.equal(app.state.skuPriceForm.cost, '');
  assert.equal(app.state.skuPriceSaving, false);
  assert.equal(app.state.skus.length, 0);
  assert.deepEqual(app.flashes, []);
});

test('an older inventory query cannot overwrite successfully saved prices', async () => {
  const app = harness(), old = deferred();
  app.state.skus = [sku()];
  app.network.send = () => old.promise;
  const loading = app.loadStock();
  app.network.send = async () => response();
  await app.state.openSkuPricing(sku());
  app.state.skuPriceForm.price = '150';
  app.network.send = async () => response({...sku(), price:150, version:1});
  await app.state.saveSkuPricing();
  old.resolve(response([sku()]));
  await loading;
  assert.equal(app.state.skus[0].price, 150);
});
