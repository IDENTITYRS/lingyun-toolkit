const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../ui/update-panel.js'), 'utf8');

function setup() {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, {textContent:'', hidden:false, disabled:false, listeners:{},
      addEventListener(name, callback) {this.listeners[name]=callback;},
      removeAttribute(name) {delete this[name];}});
    return elements.get(id);
  };
  const calls = [], timers = [];
  let busy = false, installing = false;
  let response = {ok:true,data:{ok:true,state:'latest',running:false,current_version:'3.0.0',releases:[{version:'3.0.0',notes:'first'},{version:'0.9.0',notes:'older'}]}};
  const window = {};
  vm.runInNewContext(source,{window,document:{getElementById:get},setTimeout:fn=>timers.push(fn),clearTimeout:()=>{}});
  const panel=window.createLingyunUpdatePanel(async (action,payload)=>{calls.push({action,payload});if(response instanceof Error)throw response;return response;},()=>busy,v=>installing=v);
  return {get,panel,calls,timers,setResponse:v=>response=v,setBusy:v=>busy=v,isInstalling:()=>installing};
}
const flush = () => new Promise(resolve=>setImmediate(resolve));

test('auto-check only once, history navigation bounds and selected version survive refresh',async()=>{
  const h=setup();h.panel.start();h.panel.start();await flush();
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].payload.automatic,true);
  assert.equal(h.get('updateHistoryPosition').textContent,'1 / 2');
  assert.equal(h.get('updateHistoryPrev').disabled,true);
  h.get('updateHistoryNext').listeners.click();
  assert.equal(h.get('updateHistoryNotes').textContent,'older');
  assert.equal(h.get('updateHistoryNext').disabled,true);
  h.get('checkUpdate').listeners.click();await flush();
  assert.equal(h.get('updateHistoryTitle').textContent,'V0.9.0 更新内容');
  assert.equal(h.get('installUpdate').hidden,true);
});
test('new release is separate from installed history; installation polls and disables business work',async()=>{
  const h=setup();h.setResponse({ok:true,data:{ok:true,state:'available',running:false,current_version:'3.0.0',latest_version:'3.1.0',releases:[{version:'3.1.0',notes:'new'},{version:'3.0.0',notes:'installed'}]}});
  h.panel.start();await flush();assert.equal(h.get('installUpdate').hidden,false);
  assert.match(h.get('updateNewNotes').textContent,/new/);assert.equal(h.get('updateHistoryNotes').textContent,'installed');
  h.setResponse({ok:true,data:{ok:true,state:'downloading',running:true,current_version:'3.0.0',progress:40,total_bytes:1000,downloaded_bytes:400}});
  h.get('installUpdate').listeners.click();await flush();
  assert.equal(h.calls[1].action,'install_update');assert.equal(h.isInstalling(),true);
  assert.equal(h.get('updateProgress').value,40);assert.equal(h.get('checkUpdate').disabled,true);
  assert.equal(h.timers.length,1);
});
test('check errors are retryable; active business task blocks install/check',async()=>{
  const h=setup();h.setResponse(new Error('offline'));h.panel.start();await flush();
  assert.match(h.get('updateMessage').textContent,/offline/);assert.equal(h.get('checkUpdate').disabled,false);
  h.setBusy(true);h.panel.controls();h.get('installUpdate').listeners.click();h.get('checkUpdate').listeners.click();
  assert.equal(h.calls.length,1);assert.equal(h.get('checkUpdate').disabled,true);
});
