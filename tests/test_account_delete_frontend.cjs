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
function user(id, overrides={}) { return {id,username:'user_'+id,display_name:'演示',role:'candidate',org_name:'',active:false,version:3,can_manage_jobs:false,...overrides}; }

test('delete action is limited to inactive non-self accounts other than bosses',async()=>{
  const {sandbox,element}=harness();
  sandbox.reply={items:[user(1,{role:'admin'}),user(2,{active:true}),user(3,{role:'boss'}),user(4),user(5,{role:'hr'})],total:5,page:1};
  vm.runInContext('api=async()=>reply',sandbox);
  await vm.runInContext('loadAccounts()',sandbox);
  const html=element('account-rows').innerHTML;
  assert.doesNotMatch(html,/data-account-delete="(?:1|2|3)"/);
  assert.match(html,/data-account-delete="4"/);
  assert.match(html,/data-account-delete="5"/);
  assert.match(html,/负责人需先交接/);
});

test('deletion explains irreversible impact and requires exact username',async()=>{
  const {sandbox,element}=harness(); const calls=[];
  sandbox.row=user(4);sandbox.request=async(url,body)=>{calls.push({url,body});return {items:[],total:0,page:1};};
  vm.runInContext('api=request;openAccountDialog(row,"delete")',sandbox);
  assert.match(element('account-dialog-title').textContent,/永久删除/);
  assert.match(element('account-dialog-explanation').textContent,/简历和收藏/);
  assert.match(element('account-dialog-explanation').textContent,/登录会话失效/);
  assert.match(element('account-dialog-explanation').textContent,/无法从系统恢复/);
  assert.match(element('account-dialog-explanation').textContent,/企业、岗位和共享历史保留/);
  assert.match(element('account-dialog-explanation').textContent,/备份可能仍保留/);
  assert.equal(element('account-dialog-submit').disabled,true);
  assert.equal(element('account-delete-confirm').required,true);
  element('account-delete-confirm').value=' user_4';
  await vm.runInContext('saveAccountAction()',sandbox);
  assert.equal(calls.length,0);
  assert.match(element('account-dialog-notice').textContent,/账号名/);
  element('account-delete-confirm').value='user_4';
  element('account-delete-confirm').listeners.input[0]();
  assert.equal(element('account-dialog-submit').disabled,false);
  await vm.runInContext('saveAccountAction()',sandbox);
  assert.equal(calls[0].url,'/api/admin/accounts/delete');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),{id:4,version:3,confirm_username:'user_4'});
});

test('cancel never submits a delete request and resets delete fields on next dialog',async()=>{
  const {sandbox,element}=harness(); const calls=[];
  sandbox.row=user(4);sandbox.request=async(...args)=>{calls.push(args);};
  vm.runInContext('api=request;openAccountDialog(row,"delete");closeAccountDialog();openAccountDialog(row,"status")',sandbox);
  assert.equal(calls.length,0);
  assert.equal(element('account-delete-fields').hidden,true);
  assert.equal(element('account-delete-confirm').required,false);
  assert.equal(element('account-dialog-submit').disabled,false);
});

test('server conflict reason is shown and dialog remains usable',async()=>{
  const {sandbox,element}=harness();sandbox.row=user(4);
  sandbox.request=async()=>{const error=new Error('该账号存在不可删除的审核关联');error.status=409;throw error;};
  vm.runInContext('api=request;openAccountDialog(row,"delete")',sandbox);
  element('account-delete-confirm').value='user_4';
  await vm.runInContext('saveAccountAction()',sandbox);
  assert.equal(element('account-dialog').open,true);
  assert.match(element('account-dialog-notice').textContent,/该账号存在不可删除的审核关联/);
  assert.match(element('account-dialog-notice').textContent,/刷新/);
  assert.equal(element('account-dialog-submit').disabled,false);
});

test('an in-flight deletion cannot be submitted twice or dismissed',async()=>{
  const {sandbox,element}=harness();let resolveRequest;const calls=[];
  sandbox.row=user(4);sandbox.request=(url,body)=>{calls.push({url,body});return new Promise(resolve=>{resolveRequest=resolve;});};
  vm.runInContext('api=request;openAccountDialog(row,"delete")',sandbox);
  element('account-delete-confirm').value='user_4';
  const first=vm.runInContext('saveAccountAction()',sandbox);
  await vm.runInContext('saveAccountAction()',sandbox);
  vm.runInContext('closeAccountDialog()',sandbox);
  assert.equal(calls.length,1);
  assert.equal(element('account-dialog').open,true);
  assert.equal(element('account-dialog-submit').disabled,true);
  // Rejecting the request leaves the dialog open without starting a list refresh.
  resolveRequest(Promise.reject(new Error('网络失败')));
  await first;
  assert.match(element('account-dialog-notice').textContent,/网络失败/);
});

test('deleting the last row on a page returns to the last existing page',async()=>{
  const {sandbox,element}=harness();const calls=[];
  sandbox.request=async url=>{calls.push(url);return calls.length===1?{items:[],total:20,page:2}:{items:[user(3)],total:20,page:1};};
  vm.runInContext('api=request;state.accountPage=2',sandbox);
  await vm.runInContext('loadAccounts()',sandbox);
  assert.equal(calls.length,2);
  assert.match(calls[0],/page=2/);assert.match(calls[1],/page=1/);
  assert.match(element('account-page-status').textContent,/第 1 \/ 1 页/);
  assert.match(element('account-rows').innerHTML,/user_3/);
});
