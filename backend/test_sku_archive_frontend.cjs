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
const sku = id => ({id, product_id:1, barcode:`OLD-${id}`, product_name:'Old shirt', color:'Blue', size:'M', version:0, quantity:0, active:true});
const status = (extra={}) => ({sku:sku(1), version:0, active:true, archived_at:null, inventories:[{store:'1号店',quantity:0}],
  can_archive:true, archive_reasons:[], can_delete:false, delete_reasons:['已有库存流水'], can_restore:false, ...extra});
const response = (body, code=200) => ({ok:code>=200 && code<300, status:code, json:async()=>body});
const deferred = () => {let resolve; const promise = new Promise(done=>resolve=done); return {promise,resolve};};
function harness() {
  let config;
  const requests=[], messages=[], network={send:async()=>response(status())};
  vm.runInNewContext(script, {
    Vue:{createApp(value){config=value; return {mount(){}};}}, URLSearchParams, console, setTimeout(){},
    document:{cookie:'',getElementById(){return null;}}, localStorage:{getItem(){return null;},setItem(){},removeItem(){}},
    fetch(url,opt){requests.push({url, method:opt.method || 'GET', body:opt.body && JSON.parse(opt.body)}); return network.send(url,opt);},
  });
  const state=vue.reactive(config.data());
  for(const [key,fn] of Object.entries(config.methods)) state[key]=fn.bind(state);
  for(const [key,fn] of Object.entries(config.computed)) {
    const getter=vue.computed(fn.bind(state)); Object.defineProperty(state,key,{get:()=>getter.value});
  }
  for(const [key,definition] of Object.entries(config.watch)) vue.watch(()=>state[key],(typeof definition==='function'?definition:definition.handler).bind(state),{flush:definition.flush || 'sync'});
  state.$nextTick=fn=>fn(); state.flash=value=>messages.push(value); state.loadStock=()=>{};
  state.me={id:1, role:'manager'};
  return {state, requests, network, messages};
}

test('archive reads fresh status and submits version with durable request ID',async()=>{
  const app=harness(); await app.state.openSkuLifecycle(sku(1),'archive');
  assert.equal(app.state.lifecycleAllowed,true);
  app.state.skus=[sku(1),sku(2)]; app.state.cart=[sku(1),sku(2)]; app.state.transferDraft=[sku(1),sku(2)];
  app.network.send=async()=>response({ok:true,id:1,active:false,version:1,archived_at:'now'});
  await app.state.saveSkuLifecycle();
  assert.equal(app.requests[1].url,'/api/skus/1/archive'); assert.equal(app.requests[1].method,'POST');
  assert.equal(app.requests[1].body.expected_version,0); assert(app.requests[1].body.client_request_id.length>=16);
  assert.equal(app.state.lifecycle.sku,null); assert.equal(app.state.hasManagementPending,false);
  assert.deepEqual(Array.from(app.state.skus,s=>s.id),[2]); assert.deepEqual(Array.from(app.state.cart,s=>s.id),[2]);
  assert.deepEqual(Array.from(app.state.transferDraft,s=>s.id),[2]);
});

test('blocked archive displays reasons and cannot submit',async()=>{
  const app=harness(); app.network.send=async()=>response(status({can_archive:false,archive_reasons:['4号店仍有库存','盘点未完成']}));
  await app.state.openSkuLifecycle(sku(1),'archive'); await app.state.saveSkuLifecycle();
  assert.equal(app.state.lifecycleAllowed,false); assert.equal(app.state.lifecycleReasons.length,2);
  assert.equal(app.requests.length,1);
});

test('blocked delete can switch to archive without deleting historical records',async()=>{
  const app=harness(); await app.state.delSku(sku(1));
  assert.equal(app.state.lifecycle.action,'delete'); assert.equal(app.state.lifecycleAllowed,false);
  await app.state.saveSkuLifecycle(); assert.equal(app.requests.length,1);
  app.state.lifecycle.action='archive'; app.network.send=async()=>response({id:1});
  await app.state.saveSkuLifecycle(); assert.equal(app.requests.at(-1).url,'/api/skus/1/archive');
});

test('unused SKU deletion submits expected version via DELETE',async()=>{
  const app=harness(); app.network.send=async()=>response(status({can_delete:true,delete_reasons:[]}));
  await app.state.delSku(sku(1)); app.network.send=async()=>response({id:1,deleted:true});
  await app.state.saveSkuLifecycle();
  assert.equal(app.requests.at(-1).method,'DELETE'); assert.equal(app.requests.at(-1).body.expected_version,0);
});

test('restoration removes archived row and makes inactive inventory visible',async()=>{
  const app=harness(); app.network.send=async()=>response(status({can_restore:true,can_archive:false,archived_at:'now'}));
  app.state.archivedSkus=[sku(1),sku(2)]; await app.state.openSkuLifecycle(sku(1),'restore');
  assert.equal(app.state.lifecycleTitle,'恢复为停用');
  app.network.send=async()=>response({id:1,active:false,archived_at:null}); await app.state.saveSkuLifecycle();
  assert.equal(app.state.includeInactive,true); assert.deepEqual(Array.from(app.state.archivedSkus,s=>s.id),[2]);
  assert.equal(app.requests.at(-1).url,'/api/skus/1/restore');
});

test('lost response freezes the same request, method and URL through retry',async()=>{
  const app=harness(); await app.state.openSkuLifecycle(sku(1),'archive');
  app.network.send=async()=>{throw Error('Lost response');}; await app.state.saveSkuLifecycle();
  const first=app.requests.at(-1); assert.equal(app.state.hasManagementPending,true);
  app.state.closeSkuLifecycle(); await app.state.reloadSkuLifecycle();
  assert.equal(app.requests.length,2); assert.ok(app.state.lifecycle.sku);
  app.state.lifecycle.action='delete'; app.state.lifecycle.status.version=99;
  app.network.send=async()=>response({id:1}); await app.state.saveSkuLifecycle();
  assert.deepEqual(app.requests.at(-1),first); assert.equal(app.state.hasManagementPending,false);
});

test('saving prevents duplicate submission, cancellation and reloading',async()=>{
  const app=harness(); await app.state.openSkuLifecycle(sku(1),'archive');
  const pending=deferred(); app.network.send=()=>pending.promise;
  const saving=app.state.saveSkuLifecycle(); await app.state.saveSkuLifecycle(); app.state.closeSkuLifecycle();
  await app.state.reloadSkuLifecycle(); assert.equal(app.requests.length,2);
  assert.ok(app.state.lifecycle.sku); pending.resolve(response({id:1})); await saving;
});

test('conflict and business rejection require re-reading and new confirmation',async()=>{
  for(const code of [400,409]){
    const app=harness(); await app.state.openSkuLifecycle(sku(1),'archive');
    app.network.send=async()=>response({detail:'Changed'},code); await app.state.saveSkuLifecycle();
    assert.equal(app.state.lifecycle.reloadRequired,true); assert.equal(app.state.lifecycle.attempt,null);
    await app.state.saveSkuLifecycle(); assert.equal(app.requests.length,2);
    app.network.send=async()=>response(status({version:1,can_archive:false,archive_reasons:['New stock']}));
    await app.state.reloadSkuLifecycle(); assert.equal(app.state.lifecycle.reloadRequired,false);
    assert.equal(app.state.lifecycleAllowed,false); await app.state.saveSkuLifecycle(); assert.equal(app.requests.length,3);
  }
});

test('malformed status is not usable for destructive actions',async()=>{
  const app=harness(); app.network.send=async()=>response({sku:sku(1),can_archive:true});
  await app.state.openSkuLifecycle(sku(1),'archive'); await app.state.saveSkuLifecycle();
  assert.equal(app.state.lifecycle.reloadRequired,true); assert.equal(app.requests.length,1);
});

test('late status response after closing or changing account is ignored',async()=>{
  for(const reset of [s=>s.closeSkuLifecycle(),s=>{s.me={id:2,role:'staff'};}]){
    const app=harness(), pending=deferred(); app.network.send=()=>pending.promise;
    const loading=app.state.openSkuLifecycle(sku(1),'archive'); reset(app.state);
    pending.resolve(response(status())); await loading;
    assert.equal(app.state.lifecycle.status,null); assert.equal(app.state.lifecycle.sku,null);
  }
});

test('late submission response cannot alter another account inventory',async()=>{
  const app=harness(); await app.state.openSkuLifecycle(sku(1),'archive');
  const pending=deferred(); app.network.send=()=>pending.promise; const saving=app.state.saveSkuLifecycle();
  app.state.me={id:2,role:'staff'}; app.state.skus=[sku(1)];
  pending.resolve(response({id:1})); await saving;
  assert.equal(app.state.skus.length,1); assert.equal(app.messages.length,0);
});

test('staff cannot load archive status or open lifecycle actions',async()=>{
  const app=harness(); app.state.me={id:2,role:'staff'};
  await app.state.loadArchived(); await app.state.openSkuLifecycle(sku(1),'archive'); app.state.goArchive();
  assert.equal(app.requests.length,0); assert.equal(app.state.tab,'stock');
});

test('pending scans or uncertain checkout prevent opening archive action',async()=>{
  for(const key of ['pendingScans','checkingOut','checkoutAttempt']){
    const app=harness(); app.state[key]=true; await app.state.openSkuLifecycle(sku(1),'archive');
    assert.equal(app.requests.length,0); assert.equal(app.state.lifecycle.sku,null);
  }
});

test('pending archive blocks logout, store changes and new management operations',async()=>{
  const app=harness(); await app.state.openSkuLifecycle(sku(1),'archive');
  app.network.send=async()=>response({detail:'Failure'},503); await app.state.saveSkuLifecycle();
  await app.state.logout(); app.state.selectCurrentStore('4号店'); await app.state.openBulkPricing({id:1});
  assert.equal(app.state.me.id,1); assert.equal(app.state.currentStore,'1号店'); assert.equal(app.requests.length,2);
});

test('archive search and paging use the applied keyword rather than an unsent edit',async()=>{
  const app=harness(); app.network.send=async()=>response({items:[sku(1)],total:61});
  app.state.archiveKeyword=' OLD & 1 '; await app.state.loadArchived(true);
  let query=new URL(app.requests.at(-1).url,'http://localhost').searchParams;
  assert.equal(query.get('keyword'),'OLD & 1'); assert.equal(query.get('offset'),'0'); assert.equal(query.get('limit'),'30');
  app.state.archiveKeyword='unsubmitted'; app.state.changeArchivePage(1); await new Promise(resolve=>setImmediate(resolve));
  query=new URL(app.requests.at(-1).url,'http://localhost').searchParams;
  assert.equal(query.get('keyword'),'OLD & 1'); assert.equal(query.get('offset'),'30');
});

test('restoring last item clamps archive page to remaining last page',async()=>{
  const app=harness(); app.state.archiveOffset=30;
  app.network.send=async()=>response({items:[],total:0}); await app.state.loadArchived(false);
  assert.equal(app.requests.length,2); assert.equal(app.state.archiveOffset,0); assert.equal(app.state.archiveLoading,false);
});

test('stale archive search response and late data after logout are ignored',async()=>{
  const app=harness(), pending=deferred(); app.network.send=()=>pending.promise;
  app.state.archiveKeyword='old'; const old=app.state.loadArchived(true);
  app.network.send=async()=>response({items:[sku(2)],total:1}); app.state.archiveKeyword='new'; await app.state.loadArchived(true);
  pending.resolve(response({items:[sku(1)],total:99})); await old;
  assert.deepEqual(Array.from(app.state.archivedSkus,s=>s.id),[2]); assert.equal(app.state.archiveTotal,1);
  const next=deferred(); app.network.send=()=>next.promise; const loading=app.state.loadArchived(true); app.state.me=null;
  next.resolve(response({items:[sku(1)],total:1})); await loading; assert.equal(app.state.archivedSkus.length,0);
});

test('archive loading failure is visible and does not retain old result rows',async()=>{
  const app=harness(); app.state.archivedSkus=[sku(1)]; app.network.send=async()=>response({detail:'No access'},403);
  await app.state.loadArchived(); assert.equal(app.state.archiveError,'No access'); assert.equal(app.state.archivedSkus.length,0);
});

test('archived SKU cannot open stale cost or stock correction forms',async()=>{
  const app=harness(); app.network.send=async()=>response({...sku(1),archived_at:'now'});
  await app.state.openSkuPricing(sku(1)); assert.equal(app.state.skuPriceReloadRequired,true);
  assert.match(app.state.skuPriceError,/归档/); await app.state.openCorrection(sku(1));
  assert.equal(app.state.correction.sku,null); assert.match(app.state.correction.error,/归档/);
});

test('template separates archive and inactive views and does not promise history deletion',()=>{
  assert(html.includes("tab==='archive' && isManager")); assert(html.includes('恢复为停用'));
  assert(!html.includes('库存流水将一并移除'));
});
