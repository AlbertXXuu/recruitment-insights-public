const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function overviewResponse(options = {}) {
  return {stats: {total: 0}, jobs: {items: [], total: 0, page: 1, page_size: 15}, options};
}

test('a failed overview request restores filter controls and allows an options retry', async () => {
  const elements=new Map();
  const element=id=>{
    if(!elements.has(id))elements.set(id,{value:id==='dataset'?'real':'',options:[{textContent:'全部'}],
      innerHTML:'',disabled:false,addEventListener(){},setAttribute(){},classList:{toggle(){}}});
    return elements.get(id);
  };
  const context=vm.createContext({document:{getElementById:element,querySelectorAll:()=>[]},
    URLSearchParams,setInterval(){},setTimeout,clearTimeout,console,window:{}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8').replace(/^startApp\(\);$/m,''),context);
  const requests=[];
  context.request=url=>{requests.push(new URL(url,'http://localhost'));return Promise.reject(new Error('temporary request failure'));};
  vm.runInContext('api=request;renderStats=()=>{};renderJobs=()=>{};',context);
  await assert.rejects(vm.runInContext('load(true)',context),/temporary request failure/);
  assert.equal(requests.length,1);
  assert.equal(requests[0].pathname,'/api/overview');
  assert.equal(requests[0].searchParams.get('include_options'),'1');
  for(const id of ['location','category','education','experience','source'])assert.equal(element(id).disabled,false);
  assert.equal(vm.runInContext('state.optionsDataset',context),undefined);
  context.request=url=>{requests.push(new URL(url,'http://localhost'));return Promise.resolve(overviewResponse({location:['杭州']}));};
  await vm.runInContext('api=request;load(true)',context);
  assert.equal(requests.length,2);
  assert.equal(requests[1].pathname,'/api/overview');
  assert.equal(requests[1].searchParams.get('include_options'),'1');
  assert.match(element('location').innerHTML,/杭州/);
  assert.equal(element('location').disabled,false);
});

test('a poll overlapping dataset switch still installs the new filter options', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: id === 'dataset' ? 'demo' : '', options: [{textContent: '全部'}],
      innerHTML: '', addEventListener() {}, setAttribute() {},
      classList: {toggle() {}},
    });
    return elements.get(id);
  };
  const sandbox = {
    document: {getElementById: element, querySelectorAll: () => [], addEventListener() {}},
    URLSearchParams, setInterval() {}, setTimeout, clearTimeout, console,
    window: {syncSelects() {}},
  };
  const context = vm.createContext(sandbox);
  let source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
  source = source.replace(/^startApp\(\);$/m, '');
  vm.runInContext(source, context);
  const pending = [];
  context.request = url => new Promise(resolve => pending.push({url, resolve}));
  vm.runInContext('api=request; renderStats=()=>{}; renderJobs=()=>{}; state.optionsDataset="real";', context);
  const switchRequest = vm.runInContext('load(true)', context);
  const pollRequest = vm.runInContext('load()', context);
  assert.equal(pending.length, 2, 'one combined request per overlapping load');
  for (const p of pending) {
    assert.equal(new URL(p.url, 'http://localhost').pathname, '/api/overview');
    assert.equal(new URL(p.url, 'http://localhost').searchParams.get('include_options'), '1');
    p.resolve(overviewResponse({location: ['北京', '上海']}));
  }
  await Promise.all([switchRequest, pollRequest]);
  assert.match(element('location').innerHTML, /北京/);
  assert.equal(element('location').disabled, false);
});

test('finishing the initial load preserves a candidate navigation to favorites', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      id, value: id === 'dataset' ? 'demo' : '', options: [{textContent: '全部'}],
      innerHTML: '', hidden: false, dataset: {}, listeners: {},
      addEventListener(type, listener) { this.listeners[type] = listener; },
      setAttribute() {}, classList: {toggle() {}},
    });
    return elements.get(id);
  };
  const views = ['overview', 'explore', 'favorites'].map(element);
  const navigation = ['explore', 'favorites'].map(view => {
    const button = element('nav-' + view);
    button.dataset.view = view;
    return button;
  });
  const context = vm.createContext({
    document: {
      getElementById: element, querySelector: element,
      querySelectorAll: selector => selector === '.nav' ? navigation : selector === '.view' ? views : [],
    },
    URLSearchParams, setInterval() {}, setTimeout, clearTimeout, console, window: {},
  });
  const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8')
    .replace(/^startApp\(\);$/m, '');
  vm.runInContext(source, context);
  const pending = [];
  let signalInitialLoad;
  const initialLoadStarted = new Promise(resolve => { signalInitialLoad = resolve; });
  context.request = url => {
    if (url === '/api/auth/me') return Promise.resolve({role: 'candidate', display_name: '测试求职者'});
    if (url === '/api/favorites') return Promise.resolve([]);
    return new Promise(resolve => {
      pending.push({url, resolve});
      signalInitialLoad();
    });
  };
  vm.runInContext('api=request; renderStats=()=>{}; renderJobs=()=>{};', context);
  const startup = vm.runInContext('startApp()', context);
  await initialLoadStarted;

  // The user navigates while the original combined overview is still pending.
  element('nav-favorites').listeners.click();
  assert.equal(vm.runInContext('state.view', context), 'favorites');
  for (const request of pending) request.resolve(overviewResponse());
  await startup;

  assert.equal(vm.runInContext('state.view', context), 'favorites');
  assert.equal(element('favorites').hidden, false);
  assert.equal(element('explore').hidden, true);
});

function lifecycleHarness() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      id, value: ['dataset', 'management-dataset'].includes(id) ? 'demo' : id === 'management-status' ? 'all' : '',
      options: [{textContent: '全部'}], textContent: '', innerHTML: '',
      hidden: false, open: false, dataset: {}, listeners: {},
      addEventListener(type, listener) { this.listeners[type] = listener; },
      setAttribute() {}, focus() {}, scrollIntoView() {}, reset() {},
      classList: {toggle() {}}, querySelector() { return null; }, querySelectorAll() { return []; },
      showModal() { this.open = true; }, close() { this.open = false; },
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    document: {getElementById: element, querySelector: element, querySelectorAll: () => []},
    URLSearchParams, setInterval() {}, setTimeout, clearTimeout, console, window: {},
  });
  const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8')
    .replace(/^startApp\(\);$/m, '');
  vm.runInContext(source, context);
  vm.runInContext('api=(...args)=>request(...args); renderStats=()=>{}; renderJobs=()=>{};', context);
  return {element, context};
}

test('account display name and static identity remain separate for every role', () => {
  const {element, context} = lifecycleHarness();
  for (const [role, label] of [['candidate', '求职者'], ['hr', 'HR'], ['boss', '企业负责人'], ['admin', '数据管理员']]) {
    context.currentUser = {role, display_name: '我的显示名称'};
    vm.runInContext('configureUser(currentUser)', context);
    assert.equal(element('account-name').textContent, '我的显示名称');
    assert.equal(element('account-role').textContent, '身份：' + label);
    assert.equal(element('account-name').innerHTML, '');
  }
});

test('overview sends one complete filtered request and fetches options only when needed', async () => {
  const {element, context} = lifecycleHarness();
  const requests = [];
  const rendered = [];
  context.recordStats = value => rendered.push(['stats', value.total]);
  context.recordJobs = value => rendered.push(['jobs', value.total]);
  vm.runInContext('renderStats=recordStats;renderJobs=recordJobs;state.page=3;state.skill="React";', context);
  for (const [key, value] of Object.entries({dataset:'real',scope:'all',q:'C++ & AI',location:'杭州',
      category:'前端开发',education:'本科',experience:'应届',source:'greenhouse:figma'})) element(key).value=value;
  context.request = async url => {
    const parsed = new URL(url, 'http://localhost');
    requests.push(parsed);
    const result = {stats:{total:31},jobs:{items:[],total:31,page:3,page_size:15}};
    if (parsed.searchParams.get('include_options') === '1') result.options = {
      location:['杭州'],category:['前端开发'],education:['本科'],experience:['应届'],source:['greenhouse:figma'],
    };
    return result;
  };
  await vm.runInContext('load()', context);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].pathname, '/api/overview');
  assert.deepEqual(Object.fromEntries(requests[0].searchParams), {
    dataset:'real',scope:'all',q:'C++ & AI',location:'杭州',category:'前端开发',education:'本科',
    experience:'应届',source:'greenhouse:figma',skill:'React',page:'3',include_options:'1',
  });
  assert.deepEqual(rendered, [['stats',31],['jobs',31]]);
  const installedOptions=element('location').innerHTML;
  element('q').value='下一次筛选';
  await vm.runInContext('load()', context);
  assert.equal(requests.length, 2);
  assert.equal(requests[1].searchParams.get('include_options'), '0');
  assert.equal(element('location').innerHTML, installedOptions);
  element('dataset').value='demo';
  await vm.runInContext('load()', context);
  assert.equal(requests[2].searchParams.get('include_options'), '1');
  assert.equal(requests[2].searchParams.get('dataset'), 'demo');
  element('scope').value='company';
  await vm.runInContext('load()', context);
  assert.equal(requests[3].searchParams.get('include_options'), '1');
  assert.equal(requests[3].searchParams.get('scope'), 'company');
  assert.equal(requests.length, 4);
  assert.ok(requests.every(request => request.pathname === '/api/overview'));
});

test('an older overview response cannot overwrite a newer dataset or its options', async () => {
  const {element, context} = lifecycleHarness();
  const pending = [];
  const rendered = [];
  context.recordStats = value => rendered.push(value.total);
  context.request = url => new Promise(resolve => pending.push({url,resolve}));
  vm.runInContext('renderStats=recordStats;',context);
  element('scope').value='all';
  const oldRequest=vm.runInContext('load(true)',context);
  element('dataset').value='real';
  const newRequest=vm.runInContext('load()',context);
  assert.equal(pending.length,2);
  pending[1].resolve({...overviewResponse({location:['最新地区']}),stats:{total:4924}});
  await newRequest;
  pending[0].resolve({...overviewResponse({location:['旧地区']}),stats:{total:180}});
  await oldRequest;
  assert.deepEqual(rendered,[4924]);
  assert.match(element('location').innerHTML,/最新地区/);
  assert.doesNotMatch(element('location').innerHTML,/旧地区/);
  assert.equal(vm.runInContext('state.optionsDataset',context),'real:all');
  assert.equal(element('location').disabled,false);
});

test('an older overview failure cannot enable controls while the latest load is pending', async () => {
  const {element, context} = lifecycleHarness();
  const pending=[];
  context.request=url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject}));
  const oldRequest=vm.runInContext('load(true)',context);
  const oldRejection=assert.rejects(oldRequest,/旧请求失败/);
  element('dataset').value='real';
  const latest=vm.runInContext('load()',context);
  pending[0].reject(new Error('旧请求失败'));
  await oldRejection;
  for(const id of ['location','category','education','experience','source'])assert.equal(element(id).disabled,true);
  pending[1].resolve(overviewResponse({location:['上海']}));
  await latest;
  assert.equal(element('location').disabled,false);
  assert.match(element('location').innerHTML,/上海/);
});

test('a successful latest overview read clears a previous read error after rendering', async () => {
  const {element, context} = lifecycleHarness();
  context.request = async () => { throw new Error('Failed to fetch'); };
  await vm.runInContext('reload()', context);
  assert.equal(element('notice').hidden, false);
  assert.equal(element('notice').textContent, '读取失败：Failed to fetch');
  assert.equal(element('notice').className, 'notice error');

  let resolve;
  context.request = () => new Promise(done => { resolve = done; });
  const rendered = [];
  context.recordStats = stats => {
    rendered.push(stats.total);
    assert.equal(element('notice').hidden, false, 'the error stays visible until fresh data renders');
  };
  vm.runInContext('renderStats=recordStats;', context);
  const retry = vm.runInContext('load()', context);
  assert.equal(element('notice').hidden, false);
  resolve({...overviewResponse(), stats: {total: 47}});
  await retry;
  assert.deepEqual(rendered, [47]);
  assert.equal(element('notice').hidden, true);
  assert.equal(element('notice').textContent, '');
});

test('a successful overview read preserves action errors and success notices', async () => {
  for (const error of [true, false]) {
    const {element, context} = lifecycleHarness();
    let resolve;
    context.request = () => new Promise(done => { resolve = done; });
    const read = vm.runInContext('reload()', context);
    const message = error ? '导入失败：CSV 格式错误' : '已导入 180 条模拟数据';
    if (error) {
      context.failAction = async () => { throw new Error(message); };
      await vm.runInContext('action($("import-submit"), failAction)', context);
    } else {
      vm.runInContext(`notice(${JSON.stringify(message)})`, context);
    }
    resolve(overviewResponse());
    await read;
    assert.equal(element('notice').hidden, false);
    assert.equal(element('notice').textContent, message);
    assert.equal(element('notice').className, error ? 'notice error' : 'notice');
  }
});

test('a superseded overview failure cannot replace a newer successful read', async () => {
  const {element, context} = lifecycleHarness();
  element('notice').hidden = true;
  const pending = [];
  const rendered = [];
  context.request = () => new Promise((resolve, reject) => pending.push({resolve, reject}));
  context.recordStats = stats => rendered.push(stats.total);
  vm.runInContext('renderStats=recordStats;', context);
  const oldRead = vm.runInContext('reload(true)', context);
  const latest = vm.runInContext('reload(true)', context);
  pending[1].resolve({...overviewResponse({location: ['北京']}), stats: {total: 47}});
  await latest;
  pending[0].reject(new Error('过期请求 Failed to fetch'));
  await oldRead;
  assert.deepEqual(rendered, [47]);
  assert.equal(element('notice').hidden, true);
  assert.equal(element('notice').textContent, '');
  assert.match(element('location').innerHTML, /北京/);
});

test('quality remains a separate request alongside the combined overview', async () => {
  const {context} = lifecycleHarness();
  const requests=[];
  const rendered=[];
  context.recordQuality=value=>rendered.push(value);
  vm.runInContext('state.view="quality";state.duplicatePage=2;renderQuality=recordQuality;',context);
  const quality={total:5,issues:[]};
  context.request=async url=>{
    const parsed=new URL(url,'http://localhost');
    requests.push(parsed);
    return parsed.pathname==='/api/quality'?quality:overviewResponse();
  };
  await vm.runInContext('load(true)',context);
  assert.deepEqual(requests.map(request=>request.pathname),['/api/overview','/api/quality']);
  assert.equal(requests[0].searchParams.get('include_options'),'1');
  assert.equal(requests[1].searchParams.get('duplicate_page'),'2');
  assert.deepEqual(rendered,[quality]);
});

test('a lifecycle conflict retains the reason and reloads a fresh token before retry', async () => {
  const {element, context} = lifecycleHarness();
  const mutations = [];
  context.request = async (url, body) => {
    if (url === '/api/jobs/lifecycle') {
      mutations.push(JSON.parse(JSON.stringify(body)));
      if (mutations.length === 1) {
        const conflict = new Error('记录已更新');
        conflict.status = 409;
        throw conflict;
      }
      return {id: 7, active: false, source_active: true, manual_inactive: true, edit_token: 'after-save'};
    }
    if (url === '/api/jobs/7') return {
      id: 7, payload: {title: '最新岗位名称', company: '示例企业', location: '北京'},
      active: true, source_active: true, manual_inactive: false, edit_token: 'fresh-token',
    };
    if (url.startsWith('/api/admin/jobs')) return {items: [], total: 0, page: 1};
    if (url === '/api/audit') return [];
    return {};
  };
  vm.runInContext(`state.canManage=true; state.user={role:'admin'};
    openLifecycle({id:7,title:'原岗位名称',company:'示例企业',active:true,source_active:true,
      manual_inactive:false,edit_token:'old-token'},'deactivate');`, context);
  element('lifecycle-reason').value = '岗位已招满，保留此原因';

  await vm.runInContext('saveLifecycle()', context);
  assert.equal(element('lifecycle-reason').value, '岗位已招满，保留此原因');
  assert.equal(element('lifecycle-submit').disabled, true);
  assert.match(element('lifecycle-notice').innerHTML, /data-reload-lifecycle/);
  await vm.runInContext('saveLifecycle()', context);
  assert.equal(mutations.length, 1, 'a stale token cannot be resubmitted before reload');

  await vm.runInContext('reloadLifecycle()', context);
  assert.equal(element('lifecycle-reason').value, '岗位已招满，保留此原因');
  assert.equal(element('lifecycle-title').textContent, '最新岗位名称');
  assert.equal(element('lifecycle-submit').disabled, false);
  await vm.runInContext('saveLifecycle()', context);
  assert.deepEqual(mutations[1], {
    id: 7, action: 'deactivate', reason: '岗位已招满，保留此原因', expected_edit_token: 'fresh-token',
  });
  assert.equal(element('lifecycle-dialog').open, false);
});

test('management filters and pagination remain independent of the public filters', async () => {
  const {element, context} = lifecycleHarness();
  const requests = [];
  context.request = async url => {
    const parsed = new URL(url, 'http://localhost');
    requests.push(parsed);
    return {items: [], total: 31, page: Number(parsed.searchParams.get('page'))};
  };
  element('dataset').value = 'real';
  element('q').value = '公开列表搜索';
  element('location').value = '北京';
  element('scope').value = 'public';
  element('management-q').value = '单独管理搜索';
  element('management-status').value = 'inactive';
  vm.runInContext('state.page=5; state.managementPage=2;', context);

  await vm.runInContext('loadManagement()', context);
  const first = requests[0];
  assert.equal(first.pathname, '/api/admin/jobs');
  assert.equal(first.searchParams.get('dataset'), 'demo');
  assert.equal(first.searchParams.get('q'), '单独管理搜索');
  assert.equal(first.searchParams.get('status'), 'inactive');
  assert.equal(first.searchParams.get('page'), '2');
  assert.equal(first.searchParams.has('location'), false);
  assert.equal(first.searchParams.has('scope'), false);

  element('management-next').listeners.click();
  await new Promise(setImmediate);
  assert.equal(requests.at(-1).searchParams.get('page'), '3');
  assert.equal(element('management-next').disabled, true);
  element('management-prev').listeners.click();
  await new Promise(setImmediate);
  assert.equal(requests.at(-1).searchParams.get('page'), '2');
  element('management-status').value = 'active';
  element('management-status').listeners.change();
  await new Promise(setImmediate);
  assert.equal(requests.at(-1).searchParams.get('page'), '1');
  assert.equal(requests.at(-1).searchParams.get('status'), 'active');
  assert.equal(element('dataset').value, 'real');
  assert.equal(element('q').value, '公开列表搜索');
  assert.equal(element('location').value, '北京');
  assert.equal(element('scope').value, 'public');
  assert.equal(vm.runInContext('state.page', context), 5);
});

test('company lifecycle controls preserve ended-source and draft visibility', async () => {
  const {element, context} = lifecycleHarness();
  const mutations = [];
  context.request = async (url, body) => {
    if (url === '/api/jobs/lifecycle') {
      mutations.push(JSON.parse(JSON.stringify(body)));
      return {id: body.id, active: body.id === 9, source_active: body.id === 9, manual_inactive: false};
    }
    if (url === '/api/auth/me') return {role: 'boss', display_name: '测试负责人', org_id: 1, org_name: '示例企业'};
    if (url === '/api/company/jobs' || url === '/api/audit') return [];
    return {};
  };
  vm.runInContext(`state.canManage=true; state.user={role:'boss'};
    openLifecycle({id:8,title:'来源已结束的岗位',source_active:false,manual_inactive:true,
      published:true,edit_token:'ended-token'},'restore');`, context);
  assert.match(element('lifecycle-explanation').textContent, /仍停用，不会重新公开/);
  assert.equal(element('lifecycle-reason').required, false);
  await vm.runInContext('saveLifecycle()', context);
  assert.match(element('notice').textContent, /已解除人工下架.*来源已结束.*仍停用，不会公开/);
  assert.doesNotMatch(element('notice').textContent, /已发布|已公开/);
  assert.deepEqual(mutations[0], {id: 8, action: 'restore', expected_edit_token: 'ended-token'});

  vm.runInContext(`openLifecycle({id:9,title:'企业草稿',source_active:true,manual_inactive:true,
    published:false,edit_token:'draft-token'},'restore');`, context);
  assert.match(element('lifecycle-explanation').textContent, /仍是企业草稿.*不会自动发布/);
  await vm.runInContext('saveLifecycle()', context);
  assert.match(element('notice').textContent, /已恢复启用，仍保留为企业草稿/);
  assert.doesNotMatch(element('notice').textContent, /已发布|已公开/);
  assert.deepEqual(mutations[1], {id: 9, action: 'restore', expected_edit_token: 'draft-token'});

  // Effective source inactivity must not become an accidental manual hold when editing.
  context.request = async url => ({
    id: Number(url.split('/').at(-1)), raw: {title: '来源失效岗位', location: '北京', description: '岗位描述'},
    active: false, source_active: false, manual_inactive: url.endsWith('/11'),
    published: false, is_demo: true, edit_token: 'edit-token',
  });
  await vm.runInContext('editCompanyJob(10)', context);
  assert.equal(element('company-active').checked, true, 'editing source inactivity keeps manual enablement');
  await vm.runInContext('editCompanyJob(11)', context);
  assert.equal(element('company-active').checked, false, 'editing a manual hold keeps it unchecked');
});

function deferredCompanyEditor() {
  const harness = lifecycleHarness();
  const pending = new Map();
  harness.context.request = url => new Promise((resolve, reject) => pending.set(url, {resolve, reject}));
  return {...harness, pending};
}

function companyRecord(id, title) {
  return {id, raw: {title, location: '北京', description: '岗位描述'}, edit_token: 'token-' + id,
    is_demo: true, published: false, manual_inactive: false};
}

test('a delayed company edit cannot replace a new job or its unsaved input', async () => {
  const {element, context, pending} = deferredCompanyEditor();
  const oldEdit = vm.runInContext('editCompanyJob(1)', context);
  await vm.runInContext('editCompanyJob()', context);
  element('company-title').value = '未保存的新岗位';
  element('company-description').value = '用户正在填写的描述';
  pending.get('/api/jobs/1').resolve(companyRecord(1, '旧岗位'));
  await oldEdit;

  assert.equal(element('company-job-id').value, '');
  assert.equal(element('company-title').value, '未保存的新岗位');
  assert.equal(element('company-description').value, '用户正在填写的描述');
  assert.equal(element('company-editor-title').textContent, '新增岗位');
  assert.equal(vm.runInContext('state.companyEditToken', context), null);
});

test('company edits resolved out of order keep the latest selected job and token', async () => {
  const {element, context, pending} = deferredCompanyEditor();
  const first = vm.runInContext('editCompanyJob(1)', context);
  const latest = vm.runInContext('editCompanyJob(2)', context);
  pending.get('/api/jobs/2').resolve(companyRecord(2, '最后选择的岗位'));
  await latest;
  element('company-title').value = '对最后岗位的未保存修改';
  pending.get('/api/jobs/1').resolve(companyRecord(1, '先前岗位'));
  await first;

  assert.equal(element('company-job-id').value, 2);
  assert.equal(element('company-title').value, '对最后岗位的未保存修改');
  assert.equal(vm.runInContext('state.companyEditToken', context), 'token-2');
});

test('cancelling a pending company edit prevents a delayed response from reopening it', async () => {
  const {element, context, pending} = deferredCompanyEditor();
  const edit = vm.runInContext('editCompanyJob(1)', context);
  element('cancel-company-edit').listeners.click();
  pending.get('/api/jobs/1').resolve(companyRecord(1, '已经取消的岗位'));
  await edit;

  assert.equal(element('company-editor').hidden, true);
  assert.equal(element('company-job-id').value, '');
});

test('a superseded company edit failure does not interrupt the current new form', async () => {
  const {element, context, pending} = deferredCompanyEditor();
  const oldEdit = vm.runInContext('editCompanyJob(1)', context);
  await vm.runInContext('editCompanyJob()', context);
  element('company-title').value = '仍在填写的新岗位';
  pending.get('/api/jobs/1').reject(new Error('旧请求网络失败'));
  await oldEdit;

  assert.equal(element('company-title').value, '仍在填写的新岗位');
  assert.equal(element('company-job-id').value, '');
  assert.equal(element('company-editor').hidden, false);
});

test('the public sample action loads genuine data, clears stale filters and reports its provenance', async () => {
  const {element, context} = lifecycleHarness();
  const mutations = [];
  const reads = [];
  context.request = async (url, body) => {
    if (url === '/api/public-sample') {
      mutations.push(JSON.parse(JSON.stringify(body)));
      return {total: 1234, source_count: 8, captured_at: '2026-10-04T00:00:00Z', rejected: 0};
    }
    reads.push(new URL(url, 'http://localhost'));
    return url.startsWith('/api/overview') ? overviewResponse({location: ['北京', '上海']}) : {};
  };
  element('dataset').value = 'demo';
  element('q').value = '模拟岗位关键词';
  element('source').value = 'demo:课程模拟数据';
  vm.runInContext("state.skill='OldSkill'; state.page=3; state.view='import';", context);

  await element('load-public-sample').listeners.click();

  assert.deepEqual(mutations, [{}]);
  assert.equal(element('dataset').value, 'real');
  assert.equal(element('q').value, '');
  assert.equal(element('source').value, '');
  assert.equal(vm.runInContext('state.skill', context), '');
  assert.equal(vm.runInContext('state.view', context), 'overview');
  assert.ok(reads.every(url => url.searchParams.get('dataset') === 'real'));
  assert.ok(reads.every(url => !url.searchParams.has('q') && !url.searchParams.has('source') && !url.searchParams.has('skill')));
  assert.match(element('notice').textContent, /1234 条真实岗位.*8 个公开来源.*采集于.*更新记录/);
  assert.equal(element('load-public-sample').disabled, false);

  vm.runInContext("configureUser({role:'candidate', display_name:'普通用户'});", context);
  assert.equal(element('load-public-sample').hidden, true);
  vm.runInContext("configureUser({role:'admin', display_name:'管理员'});", context);
  assert.equal(element('load-public-sample').hidden, false);
});

test('a failed public sample load preserves the current dataset and user filters', async () => {
  const {element, context} = lifecycleHarness();
  context.request = async url => {throw new Error('快照文件校验失败');};
  element('dataset').value = 'demo';
  element('q').value = '已填写的筛选';
  vm.runInContext("state.view='import';", context);

  await element('load-public-sample').listeners.click();

  assert.equal(element('dataset').value, 'demo');
  assert.equal(element('q').value, '已填写的筛选');
  assert.equal(vm.runInContext('state.view', context), 'import');
  assert.match(element('notice').textContent, /快照文件校验失败/);
  assert.equal(element('notice').className, 'notice error');
  assert.equal(element('load-public-sample').disabled, false);
});
