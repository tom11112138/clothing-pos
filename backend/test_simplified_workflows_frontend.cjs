const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, 'static/index.html'), 'utf8');
const script = html.slice(html.indexOf('<script>')+8, html.lastIndexOf('</script>'));
const context = vm.createContext({console});
vm.runInContext(fs.readFileSync(path.join(__dirname,'static/vendor/vue.global.prod.js'),'utf8'),context);
const vue = context.Vue;
const sku = id => ({id,product_id:1,barcode:`SKU-${id}`,color:'Blue',size:String(id),price:100+id,cost:40+id,quantity:8,version:0,active:true,inventory_updated_at:'2026-10-11T00:00:00.123456+00:00'});
const response = (body, status=200) => ({ok:status>=200 && status<300,status,json:async()=>body});
const deferred = () => {let resolve; const promise = new Promise(yes=>resolve=yes); return {promise,resolve};};
function harness() {
  let config;
  const requests = [], flashes = [], network = {send:async(url)=>response(url.startsWith('/api/skus?') ? [sku(1),sku(2)] : sku(1))};
  vm.runInNewContext(script,{
    Vue:{createApp(value){config=value; return {mount(){}};}},URLSearchParams, console,
    document:{cookie:'',getElementById(){return null;}},localStorage:{getItem(){return null;},setItem(){},removeItem(){}},setTimeout(){},
    fetch(url,opt){requests.push({url,body:opt.body && JSON.parse(opt.body)}); return network.send(url,opt);},
  });
  const state = vue.reactive(config.data());
  for (const [key,fn] of Object.entries(config.methods)) state[key]=fn.bind(state);
  for (const [key,fn] of Object.entries(config.computed)) {
    const getter=vue.computed(fn.bind(state)); Object.defineProperty(state,key,{get:()=>getter.value});
  }
  state.loadStock=()=>{}; state.loadProducts=()=>{}; state.$nextTick=fn=>fn(); state.flash=value=>flashes.push(value);
  for (const [key,definition] of Object.entries(config.watch)) vue.watch(()=>state[key],(typeof definition==='function'?definition:definition.handler).bind(state),{flush:definition.flush || 'sync'});
  state.me={id:1,role:'manager'};
  return {state,network,requests,flashes};
}
async function openBulk(app) {
  await app.state.openBulkPricing({id:1,code:'TEST',name:'Test'});
  app.state.bulkPrice.rows[0].selected=true;
  app.state.bulkPrice.changeCost=true; app.state.bulkPrice.cost='50.25';
}
function buildIntake(app) {
  app.state.np={code:'NEW',name:'New shirt',category:'T恤衫',brand:'',tag_price:199};
  Object.assign(app.state.intake,{colors:' White,Blue,White ',sizes:'M,L',price:'129',cost:'60',quantity:'2',store:'3号店'});
  app.state.buildIntakeVariants();
}

test('bulk defaults to no selections, independent cost field, exact versions and preserved stock',async()=>{
  const app=harness();
  await app.state.openBulkPricing({id:1});
  assert.equal(app.state.bulkSelected.length,0);
  assert.equal(app.state.bulkPrice.changeCost,false);
  app.state.bulkPrice.rows[0].selected=true; app.state.bulkPrice.changeCost=true; app.state.bulkPrice.cost='0';
  app.state.skus=[sku(1),sku(2)];
  app.network.send=async()=>response({changed_count:1,items:[{id:1,price:101,cost:0,version:1}]});
  await app.state.saveBulkPricing();
  const body=app.requests.at(-1).body;
  assert.equal(body.cost,0); assert.equal(body.price,undefined);
  assert.deepEqual(body.items,[{sku_id:1,expected_version:0}]);
  assert.equal(app.state.skus[0].quantity,8); assert.equal(app.state.skus[0].cost,0);
  assert.equal(app.state.skus[1].cost,42);
});

test('bulk blocks empty selection, missing field, invalid money and missing versions',async()=>{
  const app=harness(); await app.state.openBulkPricing({id:1});
  await app.state.saveBulkPricing();
  app.state.selectBulkRows(true); await app.state.saveBulkPricing();
  app.state.bulkPrice.changePrice=true;
  for (const value of ['','-1','1.001','NaN','1e2']) {app.state.bulkPrice.price=value; await app.state.saveBulkPricing();}
  assert.equal(app.requests.length,1);
  app.state.bulkPrice.price='50'; app.state.bulkPrice.rows[0].version=null;
  await app.state.saveBulkPricing(); assert.equal(app.state.bulkPrice.reloadRequired,true);
  assert.equal(app.requests.length,1);
});

test('bulk network retry retains exact payload and request ID even when inputs change',async()=>{
  const app=harness(); await openBulk(app);
  app.network.send=async()=>{throw new Error('Lost response');};
  await app.state.saveBulkPricing();
  assert.equal(app.state.hasManagementPending,true);
  const first=app.requests.at(-1).body;
  app.state.closeBulkPricing(); assert.ok(app.state.bulkPrice.product);
  app.state.bulkPrice.cost='999'; app.state.bulkPrice.rows[1].selected=true;
  app.network.send=async()=>response({changed_count:1,items:[{id:1,price:101,cost:50.25,version:1}]});
  await app.state.saveBulkPricing();
  assert.deepEqual(app.requests.at(-1).body,first);
  assert.equal(app.state.hasManagementPending,false);
});

test('bulk duplicate click and reload cannot send an additional request during submission',async()=>{
  const app=harness(); await openBulk(app); const pending=deferred(); app.network.send=()=>pending.promise;
  const saving=app.state.saveBulkPricing();
  await app.state.saveBulkPricing(); await app.state.reloadBulkPricing(); app.state.closeBulkPricing();
  assert.equal(app.requests.length,2); assert.ok(app.state.bulkPrice.product);
  pending.resolve(response({changed_count:1,items:[{id:1,price:101,cost:50.25,version:1}]})); await saving;
});

test('bulk conflict preserves draft but demands fresh versions and reconfirmation',async()=>{
  const app=harness(); await openBulk(app);
  app.network.send=async()=>response({detail:'Reload'},409); await app.state.saveBulkPricing();
  assert.equal(app.state.bulkPrice.reloadRequired,true); assert.equal(app.state.bulkPrice.cost,'50.25');
  await app.state.saveBulkPricing(); assert.equal(app.requests.length,2);
  app.network.send=async()=>response([{...sku(1),version:2},sku(2)]);
  await app.state.reloadBulkPricing();
  assert.equal(app.state.bulkPrice.rows[0].version,2); assert.equal(app.state.bulkPrice.rows[0].selected,true);
  assert.equal(app.state.bulkPrice.reloadRequired,false);
});

test('intake deduplicates pairs, preserves per-variant edits and previews separate store quantity',()=>{
  const app=harness(); buildIntake(app);
  assert.equal(app.state.intake.variants.length,4);
  app.state.intake.variants[0].quantity='5'; app.state.intake.variants[1].cost='65.25';
  app.state.buildIntakeVariants(); app.state.reviewIntake();
  const review=app.state.intake.review;
  assert.equal(review.store,'3号店'); assert.equal(review.variants[0].quantity,5); assert.equal(review.variants[1].cost,65.25);
  assert.equal(review.label.label_verified,false); assert.equal(review.label.execution_standard,null);
});

test('intake invalid row prevents review and blank or invalid product money fails',()=>{
  const app=harness(); buildIntake(app);
  app.state.intake.variants[0].quantity='1.5'; app.state.reviewIntake(); assert.equal(app.state.intake.review,null);
  app.state.intake.variants[0].quantity='0'; app.state.np.tag_price=-1; app.state.reviewIntake(); assert.equal(app.state.intake.review,null);
  app.state.np.tag_price=0; app.state.np.code=' '; app.state.reviewIntake(); assert.equal(app.state.intake.review,null);
});

test('intake confirms one immutable transaction, prevents duplicate click and resets only on success',async()=>{
  const app=harness(); buildIntake(app); app.state.reviewIntake();
  const pending=deferred(); app.network.send=()=>pending.promise;
  const saving=app.state.saveIntake(); await app.state.saveIntake(); app.state.closeIntakeReview();
  assert.equal(app.requests.length,1); assert.ok(app.state.intake.review);
  assert.equal(app.requests[0].body.variants.length,4); assert.equal(app.requests[0].body.store,'3号店');
  pending.resolve(response({created_count:4,store:'3号店'})); await saving;
  assert.equal(app.state.np.code,''); assert.equal(app.state.intake.variants.length,0);
});

test('intake uncertain response retries the original request and conflict keeps draft editable',async()=>{
  const app=harness(); buildIntake(app); app.state.reviewIntake();
  app.network.send=async()=>response({detail:'Server failure'},500); await app.state.saveIntake();
  const first=app.requests.at(-1).body;
  app.state.intake.store='4号店'; app.state.np.code='CHANGED'; app.state.reviewIntake();
  await app.state.saveIntake(); assert.deepEqual(app.requests.at(-1).body,first);
  app.network.send=async()=>response({detail:'Code exists'},409); await app.state.saveIntake();
  assert.equal(app.state.intake.attempt,null); assert.equal(app.state.intake.reloadRequired,false);
  app.state.closeIntakeReview(); assert.equal(app.state.intake.review,null); assert.equal(app.state.intake.variants.length,4);
});

test('correction fetches fresh snapshot and permits zero only with mandatory reason',async()=>{
  const app=harness(); await app.state.openCorrection(sku(1));
  assert.equal(app.state.correction.actual,'');
  app.state.correction.actual='0'; await app.state.saveCorrection(); assert.equal(app.requests.length,1);
  app.state.correction.note='counted';
  app.network.send=async()=>response({sku_id:1,store:'1号店',before_quantity:8,quantity:0,inventory_updated_at:'next'});
  await app.state.saveCorrection(); const body=app.requests.at(-1).body;
  assert.equal(body.actual_qty,0); assert.equal(body.expected_quantity,8);
  assert.equal(body.expected_updated_at,sku(1).inventory_updated_at); assert.equal(body.note,'counted');
  assert.equal(body.price,undefined); assert.equal(app.state.correction.open,false);
});

test('correction supports exact barcode lookup of inactive SKU, not partial matches',async()=>{
  const app=harness(); await app.state.openCorrection(); app.state.correction.barcode='SKU-2';
  app.network.send=async()=>response([sku(1),{...sku(2),active:false}]); await app.state.loadCorrection();
  assert.equal(app.state.correction.sku.id,2); assert.equal(app.state.correction.sku.active,false);
  assert.ok(app.requests[0].url.includes('include_inactive=true'));
  app.state.correction.barcode='SKU'; app.state.invalidateCorrection(); await app.state.loadCorrection();
  assert.equal(app.state.correction.sku,null); assert.ok(app.state.correction.error);
});

test('correction stale conflict cannot resubmit until actual count is reconfirmed',async()=>{
  const app=harness(); await app.state.openCorrection(sku(1));
  app.state.correction.actual='2'; app.state.correction.note='count';
  app.network.send=async()=>response({detail:'Stock moved'},409); await app.state.saveCorrection();
  assert.equal(app.state.correction.reloadRequired,true); await app.state.saveCorrection(); assert.equal(app.requests.length,2);
  app.network.send=async()=>response([{...sku(1),quantity:7}]); await app.state.loadCorrection();
  assert.equal(app.state.correction.actual,''); assert.equal(app.state.correction.sku.quantity,7);
  await app.state.saveCorrection(); assert.equal(app.requests.length,3);
});

test('correction switching store invalidates old snapshot, quantity and barcode read',async()=>{
  const app=harness(); await app.state.openCorrection(sku(1));
  app.state.correction.actual='2'; app.state.correction.store='2号店'; app.state.invalidateCorrection();
  assert.equal(app.state.correction.sku,null); assert.equal(app.state.correction.actual,'');
  await app.state.saveCorrection(); assert.equal(app.requests.length,1);
});

test('correction uncertain retry uses original store, count and timestamp',async()=>{
  const app=harness(); await app.state.openCorrection(sku(1));
  app.state.correction.actual='2'; app.state.correction.note='count';
  app.network.send=async()=>{throw new Error('connection lost');}; await app.state.saveCorrection();
  const first=app.requests.at(-1).body;
  app.state.closeCorrection(); assert.equal(app.state.correction.open,true);
  app.state.correction.actual='7'; app.state.correction.store='2号店';
  app.network.send=async()=>response({sku_id:1,store:'1号店',before_quantity:8,quantity:2,inventory_updated_at:'next'});
  await app.state.saveCorrection(); assert.deepEqual(app.requests.at(-1).body,first);
});

test('new management actions are denied to staff and blocked behind an unconfirmed sale',async()=>{
  const app=harness(); app.state.checkoutAttempt={pending:true};
  await app.state.openBulkPricing({id:1}); await app.state.openCorrection(sku(1)); assert.equal(app.requests.length,0);
  app.state.me={id:2,role:'staff',store:'2号店'};
  await app.state.openBulkPricing({id:1}); await app.state.openCorrection(sku(1));
  buildIntake(app); app.state.reviewIntake(); await app.state.saveIntake();
  assert.equal(app.requests.length,0);
});

test('late load and mutation responses never populate a different account',async()=>{
  const app=harness(), pending=deferred(); app.network.send=()=>pending.promise;
  const opening=app.state.openBulkPricing({id:1}); app.state.me={id:2,role:'manager'};
  pending.resolve(response([sku(1)])); await opening; assert.equal(app.state.bulkPrice.product,null);
  app.network.send=async()=>response([sku(1)]); await openBulk(app);
  const save=deferred(); app.network.send=()=>save.promise; const saving=app.state.saveBulkPricing();
  app.state.me={id:3,role:'staff',store:'2号店'};
  save.resolve(response({changed_count:1,items:[{id:1,price:101,cost:50,version:1}]})); await saving;
  assert.equal(app.state.bulkPrice.product,null); assert.equal(app.state.skus.length,0);
});

test('pending management result prevents store switch and manual logout',async()=>{
  const app=harness(); await openBulk(app); app.network.send=async()=>{throw new Error('unknown');}; await app.state.saveBulkPricing();
  app.state.selectCurrentStore('4号店'); await app.state.logout();
  assert.equal(app.state.currentStore,'1号店'); assert.equal(app.state.me.id,1); assert.equal(app.requests.length,2);
});

test('leaving the page warns only while a management result is pending',()=>{
  const app=harness(); let warned=0; const event={preventDefault(){warned++;}};
  app.state.guardPendingManagement(event); assert.equal(warned,0);
  app.state.intake.attempt='pending'; app.state.guardPendingManagement(event);
  assert.equal(warned,1); assert.equal(event.returnValue,'');
});

test('regenerating updates untouched defaults while retaining individually edited variants',()=>{
  const app=harness();buildIntake(app);
  app.state.intake.variants[0].cost='75';app.state.intake.variants[0].quantity='5';
  app.state.intake.cost='50';app.state.intake.quantity='3';app.state.buildIntakeVariants();
  assert.equal(app.state.intake.variants[0].cost,'75');assert.equal(app.state.intake.variants[0].quantity,'5');
  assert.equal(app.state.intake.variants[1].cost,'50');assert.equal(app.state.intake.variants[1].quantity,'3');
});

test('cost-only batch change preserves an unsent cart price while price changes update it',async()=>{
  const app=harness();await openBulk(app);app.state.cart=[{id:1,price:90,qty:1}];
  app.network.send=async()=>response({changed_count:1,items:[{id:1,price:101,cost:50,version:1}]});
  await app.state.saveBulkPricing();assert.equal(app.state.cart[0].price,90);
  app.network.send=async()=>response([sku(1)]);await app.state.openBulkPricing({id:1});
  app.state.selectBulkRows(true);app.state.bulkPrice.changePrice=true;app.state.bulkPrice.price='80';
  app.network.send=async()=>response({changed_count:1,items:[{id:1,price:80,cost:50,version:2}]});
  await app.state.saveBulkPricing();assert.equal(app.state.cart[0].price,80);
});

test('pending management confirmation also blocks scans and checkout',async()=>{
  const app=harness();app.state.intake.attempt='pending';app.state.cart=[{id:1,price:100,qty:1}];app.state.scanInput='SKU-1';
  await app.state.scan();await app.state.checkout();assert.equal(app.requests.length,0);
});
