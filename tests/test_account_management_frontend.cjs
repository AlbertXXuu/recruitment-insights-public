const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function harness() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      id, value:'', hidden:false, disabled:false, checked:false, required:false,
      innerHTML:'', textContent:'', dataset:{}, listeners:{},
      addEventListener(type,callback) { (this.listeners[type] ||= []).push(callback); },
      showModal(){ this.open=true; }, close(){ this.open=false; }, focus(){},
      setAttribute(){}, classList:{toggle(){}}, querySelectorAll(){return [];},
    });
    return elements.get(id);
  }
  const sandbox = vm.createContext({document:{getElementById:element,querySelectorAll(){return [];},querySelector:element},
    URLSearchParams, setInterval(){},setTimeout,clearTimeout,console,window:{}});
  const source = fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8').replace(/^startApp\(\);$/m,'');
  vm.runInContext(source,sandbox);
  vm.runInContext("state.user={id:1,role:'admin'};state.view='accounts';",sandbox);
  return {sandbox,element};
}
function user(id, overrides={}) { return {id, username:'u'+id, display_name:'测试', role:'hr',org_name:'企业',active:true,version:2,can_manage_jobs:true,...overrides}; }

test('account list escapes user supplied text and renders state and controls',async()=>{
  const {sandbox,element}=harness();
  sandbox.reply={items:[user(2,{username:'<img src=x onerror=1>',display_name:'<script>x</script>',org_name:'<b>测试</b>'})],total:1,page:1};
  vm.runInContext('api=async()=>reply',sandbox);
  await vm.runInContext('loadAccounts()',sandbox);
  const html=element('account-rows').innerHTML;
  assert.doesNotMatch(html,/<script>|<img/);
  assert.match(html,/&lt;script&gt;/);
  assert.match(html,/停用/);
  assert.match(element('account-page-status').textContent,/共 1 个/);
});

test('editing HR sends version and boolean maintenance permission',async()=>{
  const {sandbox,element}=harness();const calls=[];
  sandbox.row=user(2); sandbox.request=async(url,body)=>{calls.push({url,body});return {items:[],total:0,page:1};};
  vm.runInContext('api=request;openAccountDialog(row,"edit")',sandbox);
  element('account-edit-name').value='新名称';element('account-edit-manage').checked=false;
  await vm.runInContext('saveAccountAction()',sandbox);
  assert.equal(calls[0].url,'/api/admin/accounts/edit');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),{id:2,version:2,display_name:'新名称',can_manage_jobs:false});
});

test('removing HR only sends id and version and explains reversible removal',async()=>{
  const {sandbox,element}=harness();const calls=[];
  sandbox.row=user(2);sandbox.request=async(url,body)=>{calls.push({url,body});return [];};
  vm.runInContext('api=request;openAccountDialog(row,"remove")',sandbox);
  assert.match(element('account-dialog-explanation').textContent,/恢复后为只读/);
  await vm.runInContext('saveAccountAction()',sandbox);
  assert.equal(calls[0].url,'/api/team/deactivate');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),{id:2,version:2});
});

test('conflict keeps the dialog open and informs the user to refresh',async()=>{
  const {sandbox,element}=harness();sandbox.row=user(2);
  sandbox.request=async()=>{const error=new Error('conflict');error.status=409;throw error;};
  vm.runInContext('api=request;openAccountDialog(row,"status")',sandbox);
  await vm.runInContext('saveAccountAction()',sandbox);
  assert.equal(element('account-dialog').open,true);
  assert.equal(element('account-dialog-notice').hidden,false);
  assert.match(element('account-dialog-notice').textContent,/刷新/);
  assert.equal(element('account-dialog-submit').disabled,false);
});

test('older account search result cannot overwrite a newer result',async()=>{
  const {sandbox,element}=harness();const pending=[];
  sandbox.request=url=>new Promise(resolve=>pending.push({url,resolve}));
  vm.runInContext('api=request',sandbox);
  const first=vm.runInContext('loadAccounts()',sandbox);
  const second=vm.runInContext('loadAccounts()',sandbox);
  pending[1].resolve({items:[user(3,{display_name:'新结果'})],total:1,page:1});await second;
  pending[0].resolve({items:[user(2,{display_name:'旧结果'})],total:1,page:1});await first;
  assert.match(element('account-rows').innerHTML,/新结果/);
  assert.doesNotMatch(element('account-rows').innerHTML,/旧结果/);
});

test('account management hides unrelated source update control, restoring it on other views',()=>{
  const {sandbox,element}=harness();
  vm.runInContext('refreshView=async()=>{}; switchView("accounts")',sandbox);
  assert.equal(element('.source-action').hidden,true);
  vm.runInContext('switchView("overview")',sandbox);
  assert.equal(element('.source-action').hidden,false);
});
