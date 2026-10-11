const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, 'static/index.html'), 'utf8');
const script = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));
const vueContext = vm.createContext({console});
vm.runInContext(fs.readFileSync(path.join(__dirname, 'static/vendor/vue.global.prod.js'), 'utf8'), vueContext);
const vue = vueContext.Vue;
const response = (body, status=200) => ({ok:status>=200 && status<300, status, json:async()=>body});
function deferred() {
  let resolve;
  const promise = new Promise(yes=>{resolve=yes;});
  return {promise, resolve};
}
function report(date='2026-10-11', store=null) {
  return {period:date.length===7?'month':'day', date, store, timezone:'Asia/Shanghai',
    date_from:'2026-10-01', date_to:'2026-10-31', summary:{sales_cents:1234, refund_cents:0, net_cents:1234},
    stores:[], payments:[], days:[]};
}
function harness() {
  let config;
  const requests=[], flashes=[], popups=[];
  const network={send:async()=>response(report())};
  vm.runInNewContext(script, {
    Vue:{createApp(value){config=value; return {mount(){}};}}, URLSearchParams,
    localStorage:{getItem(){return null;}, setItem(){}, removeItem(){}},
    document:{cookie:''}, setTimeout(){},
    window:{open(){const popup={document:{write(value){popup.html=value;}}, opener:{}}; popups.push(popup); return popup;}},
    fetch(url, options){requests.push(url); return network.send(url, options);},
  });
  const state=vue.reactive(config.data());
  for (const [name, fn] of Object.entries(config.methods)) state[name]=fn.bind(state);
  for (const [name, fn] of Object.entries(config.computed)) {
    const getter=vue.computed(fn.bind(state));
    Object.defineProperty(state, name, {get:()=>getter.value});
  }
  state.loadStock=()=>{}; state.flash=value=>flashes.push(value); state.$nextTick=fn=>fn(); state.$refs={};
  for (const [name, definition] of Object.entries(config.watch)) {
    const fn=typeof definition==='function'?definition:definition.handler;
    vue.watch(()=>state[name], fn.bind(state), {flush:definition.flush || 'sync'});
  }
  state.me={id:1, role:'manager'};
  state.tab='orders'; state.orderView='day';
  return {state, network, requests, flashes, popups};
}

test('staff always requests their bound store, even if local filter is tampered with', async()=>{
  const app=harness();
  app.state.me={id:2, role:'staff', store:'2号店'};
  app.state.orderView='day'; app.state.turnoverFilter.store='1号店';
  await app.state.loadTurnover();
  assert.equal(new URL(app.requests[0], 'http://local').searchParams.get('store'), '2号店');
  assert.equal(app.state.turnoverFilter.store, '2号店');
  app.state.orderFilter.store='4号店';
  app.network.send=async()=>response({orders:[], summary:{count:0, total:0, item_count:0}});
  await app.state.loadOrders();
  assert.equal(new URL(app.requests[1], 'http://local').searchParams.get('store'), '2号店');
});

test('older report cannot overwrite a newer store and period request', async()=>{
  const app=harness(), old=deferred();
  app.network.send=()=>old.promise;
  const pending=app.state.loadTurnover();
  app.state.orderView='month'; app.state.turnoverFilter.store='3号店'; app.state.turnoverFilter.month='2026-10';
  app.network.send=async()=>response(report('2026-10', '3号店'));
  await app.state.loadTurnover();
  old.resolve(response(report('2026-10-11', '1号店')));
  await pending;
  assert.equal(app.state.turnover.store, '3号店');
  assert.equal(app.state.turnover.date, '2026-10');
  assert.equal(app.state.turnoverLoading, false);
});

test('switching accounts clears all financial data and discards late responses', async()=>{
  const app=harness(), pending=deferred();
  app.state.orders=[{id:1}]; app.state.orderDetail={id:1}; app.state.turnover=report();
  app.network.send=()=>pending.promise;
  const loading=app.state.loadTurnover();
  app.state.me={id:2, role:'staff', store:'2号店'};
  assert.equal(app.state.orders.length, 0);
  assert.equal(app.state.orderDetail, null);
  assert.equal(app.state.turnover, null);
  pending.resolve(response(report()));
  await loading;
  assert.equal(app.state.turnover, null);
  assert.equal(app.state.turnoverLoading, false);
});

test('old order list and details cannot leak into a newly logged in staff account', async()=>{
  const app=harness(), list=deferred(), detail=deferred();
  app.network.send=url=>url.includes('/api/orders?')?list.promise:detail.promise;
  const listing=app.state.loadOrders(), opening=app.state.openOrder(1);
  app.state.me={id:3, role:'staff', store:'4号店'};
  list.resolve(response({orders:[{id:1}], summary:{count:1,total:999,item_count:1}}));
  detail.resolve(response({id:1, store:'1号店'}));
  await listing; await opening;
  assert.equal(app.state.orders.length, 0);
  assert.equal(app.state.orderDetail, null);
  assert.equal(app.state.orderSummary.total, 0);
});

test('new order query wins when replies arrive out of order', async()=>{
  const app=harness(), old=deferred();
  app.network.send=()=>old.promise;
  const loading=app.state.loadOrders();
  app.state.orderFilter.store='4号店';
  app.network.send=async()=>response({orders:[{id:2}], summary:{count:501,total:1000,item_count:1000},has_more:true});
  await app.state.loadOrders();
  old.resolve(response({orders:[{id:1}],summary:{count:1,total:1,item_count:1}}));
  await loading;
  assert.equal(app.state.orders[0].id, 2);
  assert.equal(app.state.ordersHasMore, true);
  assert.equal(app.state.orderSummary.count, 501);
});

test('a manager with a store binding can still switch the selected financial store', ()=>{
  const app=harness();
  app.state.me={id:1,role:'manager',store:'3号店'};
  app.state.orderView='day'; app.state.turnoverFilter.store='1号店'; app.state.orderFilter.store='1号店';
  app.state.currentStore='4号店';
  assert.equal(app.state.turnoverFilter.store, '4号店');
  assert.equal(app.state.orderFilter.store, '4号店');
  assert.equal(new URL(app.requests.at(-1), 'http://local').searchParams.get('store'), '4号店');
});

test('report failure hides previous values and permits reload', async()=>{
  const app=harness();
  app.state.turnover=report();
  app.network.send=async()=>response({detail:'Unavailable'},500);
  await app.state.loadTurnover();
  assert.equal(app.state.turnover, null);
  assert.equal(app.state.turnoverError, 'Unavailable');
  assert.equal(app.state.turnoverLoading, false);
  app.network.send=async()=>response(report());
  await app.state.loadTurnover();
  assert.equal(app.state.turnoverError, '');
  assert.equal(app.state.turnover.date, '2026-10-11');
});

test('monthly report drills into one day and opens orders with exact period bounds', async()=>{
  const app=harness();
  app.state.turnover=report('2026-10', '3号店');
  app.network.send=async()=>response({orders:[],summary:{count:0,total:0,item_count:0}});
  app.state.openTurnoverOrders();
  await Promise.resolve();
  assert.equal(app.state.orderView, 'orders');
  assert.equal(app.state.orderFilter.date_from, '2026-10-01');
  assert.equal(app.state.orderFilter.date_to, '2026-10-31');
  assert.equal(app.state.orderFilter.store, '3号店');
  app.network.send=async()=>response(report('2026-10-05'));
  app.state.showTurnoverDay('2026-10-05');
  assert.equal(app.state.orderView, 'day');
  assert.equal(new URL(app.requests.at(-1), 'http://local').searchParams.get('date'), '2026-10-05');
});

test('receipt includes payment and escapes multiline notes without executing markup', ()=>{
  const app=harness();
  app.state.orderDetail={order_no:'ORDER',store:'1号店',operator:'test',total:10,payment_method:'wechat',
    note:'gift\n<script>evil()</script>',items:[]};
  app.state.printOrder();
  assert.match(app.popups[0].html, /支付方式：微信/);
  assert.match(app.popups[0].html, /gift\n&lt;script&gt;evil\(\)&lt;\/script&gt;/);
  assert.equal(app.popups[0].opener, null);
  assert.equal(app.state.paymentName(null), '未记录');
  assert.equal(app.state.money(-1234), '-12.34');
});
