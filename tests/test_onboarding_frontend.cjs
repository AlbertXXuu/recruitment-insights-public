const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const settle = () => new Promise(resolve => setImmediate(resolve));

function harness() {
  const nodes = new Map(), requests = [];
  function element(id) {
    if (!nodes.has(id)) nodes.set(id, {value:'', checked:false, disabled:false, hidden:true,
      textContent:'', className:'', listeners:{}, elements:{},
      addEventListener(event, fn) {this.listeners[event] = fn;},
      reset() {for(const el of Object.values(this.elements)) {el.value='';el.checked=false;}},
      replaceChildren(){}, append(){}, focus(){}, querySelectorAll(){return [];}});
    return nodes.get(id);
  }
  const form=element('application-form');
  for(const name of ['username','password','confirmation','display_name','company_name','contact_name',
    'contact','website','description','dedicated_account']) form.elements[name]=element('field-'+name);
  const context=vm.createContext({document:{getElementById:element,querySelectorAll:()=>[element('submit-application')],
    createElement:()=>({append(){},textContent:''})},location:{pathname:'/join'},
    FormData:class {constructor(form){this.form=form;} *[Symbol.iterator](){
      for(const [key,el] of Object.entries(this.form.elements)) yield [key,el.value];}},
    fetch:async (url, options)=>{
      if(url==='/api/auth/me')return {ok:false,status:401,json:async()=>({error:'请先登录'})};
      requests.push({url,body:JSON.parse(options.body)});
      return context.response || {ok:true,status:201,json:async()=>({message:'申请已提交'})};
    }});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/onboarding.js'),'utf8'),context);
  function fill() {
    for(const [key,value] of Object.entries({username:'fixture_boss',password:'Fixture-only-123!',
      confirmation:'Fixture-only-123!',display_name:'申请人',company_name:'模拟企业',contact_name:'联系人',
      contact:'qa@example.test',website:'',description:'只用于企业入驻前端验收的模拟资料'}))form.elements[key].value=value;
    form.elements.dedicated_account.checked=true;
  }
  function submit(){form.listeners.submit({preventDefault(){},currentTarget:form});}
  return {context,element,form,requests,fill,submit};
}

test('enterprise registration submits once and clears credentials after success',async()=>{
  const h=harness();await settle();h.fill();h.submit();h.submit();await settle();
  assert.equal(h.requests.length,1);
  assert.equal(h.requests[0].url,'/api/auth/register-enterprise');
  assert.equal(h.requests[0].body.dedicated_account,true);
  assert.equal(h.element('registration').hidden,true);
  assert.match(h.element('message').textContent,/申请已提交/);
  for(const key of ['password','confirmation'])assert.equal(h.form.elements[key].value,'');
});

test('mismatched passwords send nothing and clear both password fields',async()=>{
  const h=harness();await settle();h.fill();h.form.elements.confirmation.value='wrong-fixture';
  h.submit();await settle();
  assert.equal(h.requests.length,0);
  assert.match(h.element('message').textContent,/两次密码/);
  for(const key of ['password','confirmation'])assert.equal(h.form.elements[key].value,'');
  assert.equal(h.element('submit-application').disabled,false);
});

test('API failure preserves company data, clears credentials, and permits retry',async()=>{
  const h=harness();await settle();h.fill();
  h.context.response={ok:false,status:409,json:async()=>({error:'企业已存在'})};
  h.submit();await settle();
  assert.equal(h.form.elements.company_name.value,'模拟企业');
  assert.equal(h.element('submit-application').disabled,false);
  for(const key of ['password','confirmation'])assert.equal(h.form.elements[key].value,'');
  h.context.response=null;h.fill();h.submit();await settle();
  assert.equal(h.requests.length,2);
  assert.match(h.element('message').textContent,/申请已提交/);
});
