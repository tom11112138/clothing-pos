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

function deferred() {
  let resolve;
  const promise = new Promise(yes => { resolve = yes; });
  return { promise, resolve };
}

function label(id = 1) {
  return {sku_id: id, product_id: id, product_category: '半袖', label_version: 0, printable: true,
    configuration: {composition: '100% cotton', execution_standard: 'GB/T 22849-2024',
      label_usage: 'adult_skin', safety_category: 'B', label_verified: true},
    standards: {
      categories: [{name: '半袖', note: 'T恤与衬衫分开核对'}, {name: '牛仔裤', note: '机织与针织分开核对'}],
      execution_standards: [
        {code: 'GB/T 22849-2024', categories: ['半袖']},
        {code: 'GB/T 2660-2017', categories: ['半袖']},
        {code: 'FZ/T 81006-2017', categories: ['牛仔裤']},
      ],
      safety_categories: [{value: 'A', description: '婴幼儿须A类'}, {value: 'B', examples: '贴身半袖'}, {value: 'C', examples: '非贴身外套'}],
      checked_on: '2026-10-11',
    },
    image_url: `data:image/png;base64,${id}`, barcode: `SKU-${id}`, price: '129.00'};
}

function response(status = 200, body = label()) {
  return {ok: status >= 200 && status < 300, status, json: async () => body};
}

function harness() {
  let config;
  const requests = [], flashes = [], downloads = [], popups = [], writes = [];
  const network = {send: async () => response()};
  const popup = {opener: {}};
  vm.runInNewContext(script, {
    Vue: {createApp(value) { config = value; return {mount() {}}; }},
    URLSearchParams,
    document: {
      cookie: '', body: {appendChild() {}},
      createElement() { return {click() { downloads.push({href: this.href, name: this.download}); }, remove() {}}; },
    },
    window: {open(url) { popups.push(url); return popup; }},
    localStorage: {getItem() { return null; }, setItem() {}, removeItem() {}},
    setTimeout() {},
    fetch(url, options) {
      requests.push(url);
      if (options.body) writes.push({url, body: JSON.parse(options.body)});
      return network.send(url, options);
    },
  });
  const state = vue.reactive(config.data());
  for (const [name, method] of Object.entries(config.methods)) state[name] = method.bind(state);
  for (const [name, getter] of Object.entries(config.computed)) {
    const value = vue.computed(getter.bind(state));
    Object.defineProperty(state, name, {get: () => value.value});
  }
  state.flash = message => flashes.push(message);
  state.loadStock = () => {};
  for (const [name, definition] of Object.entries(config.watch)) {
    const handler = typeof definition === 'function' ? definition : definition.handler;
    vue.watch(() => state[name], handler.bind(state), {flush: definition.flush || 'sync'});
  }
  state.me = {id: 1, role: 'staff'};
  return {state, network, requests, flashes, downloads, popups, popup, writes};
}

test('an older SKU label cannot replace a newer preview', async () => {
  const app = harness(), first = deferred();
  app.network.send = () => first.promise;
  const opening = app.state.showBarcode({id: 1});
  app.network.send = async () => response(200, label(2));
  await app.state.showBarcode({id: 2});
  first.resolve(response());
  await opening;
  assert.equal(app.state.labelInfo.sku_id, 2);
  assert.equal(app.state.barcodeUrl, label(2).image_url);
  assert.equal(app.state.labelLoading, false);
});

test('closing a loading label keeps it closed after the response arrives', async () => {
  const app = harness(), pending = deferred();
  app.network.send = () => pending.promise;
  const opening = app.state.showBarcode({id: 1});
  app.state.closeBarcode();
  pending.resolve(response());
  await opening;
  assert.equal(app.state.barcodeSku, null);
  assert.equal(app.state.labelInfo, null);
  assert.equal(app.state.barcodeUrl, '');
});

test('changing users invalidates an in-flight label and disallows download', async () => {
  const app = harness(), pending = deferred();
  app.network.send = () => pending.promise;
  const opening = app.state.showBarcode({id: 1});
  app.state.me = {id: 2, role: 'staff'};
  pending.resolve(response());
  await opening;
  app.state.downloadLabel();
  app.state.printBarcode();
  assert.equal(app.state.labelInfo, null);
  assert.equal(app.state.barcodeSku, null);
  assert.equal(app.downloads.length, 0);
  assert.equal(app.popups.length, 0);
});

test('failed regeneration clears the old image and disables printing', async () => {
  const app = harness();
  await app.state.showBarcode({id: 1});
  app.network.send = async () => response(400, {detail: 'Cannot fit barcode'});
  await app.state.refreshLabel();
  app.state.downloadLabel();
  app.state.printBarcode();
  assert.equal(app.state.barcodeUrl, '');
  assert.equal(app.state.labelInfo, null);
  assert.equal(app.state.labelError, 'Cannot fit barcode');
  assert.equal(app.downloads.length, 0);
  assert.equal(app.popups.length, 0);
});

test('download keeps the generated image and print uses the chosen price and copy count', async () => {
  const app = harness();
  app.state.labelPriceBasis = 'selling';
  await app.state.showBarcode({id: 1});
  app.state.labelCopies = 3;
  app.state.downloadLabel();
  app.state.printBarcode();
  assert.equal(app.requests[0], '/api/skus/1/label?price_basis=selling');
  assert.deepEqual(app.downloads, [{href: label().image_url, name: 'sku-1-40x60mm.png'}]);
  const url = new URL(app.popups[0], 'http://localhost');
  assert.equal(url.pathname, '/static/label-print.html');
  assert.equal(url.searchParams.get('copies'), '3');
  assert.equal(url.searchParams.get('price_basis'), 'selling');
  assert.equal(app.popup.opener, null);
});

test('only integer copy counts from one through one hundred can open printing', async () => {
  const app = harness();
  await app.state.showBarcode({id: 1});
  for (const copies of [0, 101, -1, 1.5, '', NaN, Infinity]) {
    app.state.labelCopies = copies;
    app.state.printBarcode();
  }
  assert.equal(app.popups.length, 0);
  assert.equal(app.flashes.length, 7);
});

test('unconfirmed data allows a preview but cannot trigger download or printing', async () => {
  const app = harness();
  app.network.send = async () => response(200, {...label(), printable: false});
  await app.state.showBarcode({id: 1});
  app.state.downloadLabel();
  app.state.printBarcode();
  assert.equal(app.downloads.length, 0);
  assert.equal(app.popups.length, 0);
  assert.equal(app.state.labelPrintable, false);
});

test('an unsaved edit disables printing and survives changing the price mode', async () => {
  const app = harness();
  await app.state.showBarcode({id: 1});
  app.state.labelForm.composition = '50% cotton 50% polyester';
  app.state.labelPriceBasis = 'selling';
  await app.state.refreshLabel();
  assert.equal(app.state.labelForm.composition, '50% cotton 50% polyester');
  assert.equal(app.state.labelDirty, true);
  app.state.downloadLabel();
  assert.equal(app.downloads.length, 0);
});

test('saving labels is manager-only and duplicate clicks send one versioned request', async () => {
  const app = harness(), pending = deferred();
  await app.state.showBarcode({id: 1});
  app.state.labelForm.composition = '50% cotton 50% polyester';
  await app.state.saveLabel();
  assert.equal(app.writes.length, 0);
  app.state.me = {id: 1, role: 'manager'};
  await app.state.showBarcode({id: 1});
  app.state.labelForm.composition = '50% cotton 50% polyester';
  app.state.labelForm.label_verified = false;
  app.network.send = url => url.includes('/products/') ? pending.promise : response();
  const saving = app.state.saveLabel();
  await app.state.saveLabel();
  assert.equal(app.writes.length, 1);
  assert.equal(app.writes[0].body.expected_version, 0);
  assert.equal(app.writes[0].body.composition, '50% cotton 50% polyester');
  assert.equal(app.writes[0].body.label_verified, false);
  pending.resolve(response());
  await saving;
  assert.equal(app.state.labelSaving, false);
});

test('a version conflict keeps the draft and error without printing it', async () => {
  const app = harness();
  app.state.me = {id: 1, role: 'manager'};
  await app.state.showBarcode({id: 1});
  app.state.labelForm.composition = 'Edited';
  app.network.send = async () => response(409, {detail: 'Reload the label'});
  await app.state.saveLabel();
  assert.equal(app.state.labelForm.composition, 'Edited');
  assert.equal(app.state.labelError, 'Reload the label');
  assert.equal(app.state.labelSaving, false);
  assert.equal(app.state.labelPrintable, false);
});

test('a late save response cannot reopen a label after changing users', async () => {
  const app = harness(), pending = deferred();
  app.state.me = {id: 1, role: 'manager'};
  await app.state.showBarcode({id: 1});
  app.state.labelForm.composition = 'Edited';
  app.network.send = () => pending.promise;
  const saving = app.state.saveLabel();
  app.state.me = {id: 2, role: 'staff'};
  pending.resolve(response());
  await saving;
  assert.equal(app.state.labelInfo, null);
  assert.equal(app.state.labelForm, null);
  assert.equal(app.state.labelSaving, false);
});

test('reference categories group standards without changing saved label claims', async () => {
  const app = harness();
  await app.state.showBarcode({id: 1});
  assert.equal(app.state.labelCategoryFilter, '半袖');
  assert.equal(app.state.relatedLabelStandards.length, 2);
  assert.equal(app.state.otherLabelStandards.length, 1);
  assert.equal(app.state.selectedLabelSafety.examples, '贴身半袖');
  app.state.labelCategoryFilter = '牛仔裤';
  assert.equal(app.state.relatedLabelStandards[0].code, 'FZ/T 81006-2017');
  assert.ok(app.state.otherLabelStandards.some(item => item.code === app.state.labelStandardChoice));
  assert.equal(app.state.labelForm.execution_standard, 'GB/T 22849-2024');
  assert.equal(app.state.labelForm.safety_category, 'B');
  assert.equal(app.state.labelForm.label_verified, true);
  assert.equal(app.state.labelDirty, false);
  assert.equal(app.writes.length, 0);
  await app.state.refreshLabel();
  assert.equal(app.state.labelCategoryFilter, '牛仔裤');
});

test('unknown legacy categories and custom standards stay available without invented defaults', async () => {
  const app = harness(), data = label();
  data.product_category = '上衣';
  data.configuration.execution_standard = 'Q/EXAMPLE 001-2026';
  data.configuration.safety_category = null;
  data.configuration.label_verified = false;
  data.printable = false;
  app.network.send = async () => response(200, data);
  await app.state.showBarcode({id: 1});
  assert.equal(app.state.labelCategoryFilter, '');
  assert.equal(app.state.relatedLabelStandards.length, 0);
  assert.equal(app.state.otherLabelStandards.length, 3);
  assert.equal(app.state.labelStandardChoice, '__custom');
  assert.equal(app.state.labelForm.execution_standard, 'Q/EXAMPLE 001-2026');
  assert.equal(app.state.labelForm.safety_category, '');
  assert.equal(app.state.selectedLabelSafety, undefined);
  assert.equal(app.state.labelPrintable, false);
});

test('choosing another product standard clears confirmation without guessing a safety category', async () => {
  const app = harness();
  await app.state.showBarcode({id: 1});
  app.state.labelStandardChoice = 'GB/T 2660-2017';
  app.state.chooseLabelStandard();
  assert.equal(app.state.labelForm.execution_standard, 'GB/T 2660-2017');
  assert.equal(app.state.labelForm.safety_category, 'B');
  assert.equal(app.state.labelForm.label_verified, false);
  assert.equal(app.state.labelPrintable, false);
});

test('product entry loads category choices from the shared backend catalog', async () => {
  const app = harness();
  app.network.send = async url => response(200, url.endsWith('label-catalog') ? label().standards : []);
  await app.state.loadProducts();
  assert.equal(app.requests.at(-1), '/api/products/label-catalog');
  assert.equal(app.state.productCategories.length, 2);
  assert.equal(app.state.productCategories[0].name, '半袖');
  assert.equal(app.state.np.category, '');
  assert.equal(app.state.productCatalogLoading, false);
});

test('changing users discards a late category catalog response', async () => {
  const app = harness(), pending = deferred();
  app.network.send = () => pending.promise;
  const loading = app.state.loadLabelCatalog();
  app.state.me = {id: 2, role: 'manager'};
  pending.resolve(response(200, label().standards));
  await loading;
  assert.equal(app.state.productCategories.length, 0);
  assert.equal(app.state.productCatalogLoading, false);
  assert.equal(app.state.productCatalogError, '');
});

test('failed category catalog can be reloaded without leaving a stuck loading state', async () => {
  const app = harness();
  app.network.send = async () => response(500, {detail: 'Catalog unavailable'});
  await app.state.loadLabelCatalog();
  assert.equal(app.state.productCatalogLoading, false);
  assert.equal(app.state.productCatalogError, 'Catalog unavailable');
  app.network.send = async () => response(200, label().standards);
  await app.state.loadLabelCatalog();
  assert.equal(app.state.productCatalogError, '');
  assert.equal(app.state.productCategories.length, 2);
});
