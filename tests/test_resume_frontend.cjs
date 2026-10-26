const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const maxBytes = 2097152;
const pdfType = 'application/pdf';
const metadata = (filename = '我的简历.pdf', size = 16) => ({
  filename, media_type: pdfType, size_bytes: size, uploaded_at: '2026-10-15T15:00:00+00:00',
});
const response = resume => ({resume, max_bytes: maxBytes, allowed_extensions: ['.pdf', '.docx']});
const settle = () => new Promise(resolve => setImmediate(resolve));

function resumePage({role = 'candidate', deferredRead = false} = {}) {
  const elements = new Map(), calls = [], reads = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      id, value: '', files: [], hidden: false, disabled: false, dataset: {},
      textContent: id === 'resume-submit' ? '上传简历' : '', innerHTML: '', listeners: {},
      addEventListener(type, listener) { this.listeners[type] = listener; },
      setAttribute() {}, classList: {toggle() {}},
    });
    return elements.get(id);
  };
  const views = ['overview', 'explore', 'resume'].map(element);
  const navigation = views.map(view => {
    const button = element('nav-' + view.id);
    button.dataset.view = view.id;
    if (view.id === 'resume') button.dataset.roles = 'candidate';
    return button;
  });
  const completeRead = index => {
    const {reader, file} = reads[index];
    reader.result = 'data:' + (file.type || pdfType) + ';base64,' + file.content.toString('base64');
    reader.onload();
  };
  const context = vm.createContext({
    document: {
      getElementById: element, querySelector: element,
      querySelectorAll: selector => ['.nav','.nav[data-view]'].includes(selector) ? navigation : selector === '.view' ? views
        : selector === '[data-roles]' ? [element('nav-resume')] : [],
    },
    URLSearchParams, setInterval() {}, setTimeout, clearTimeout, console, window: {},
    FileReader: class {
      readAsDataURL(file) {
        reads.push({reader: this, file});
        if (!deferredRead) queueMicrotask(() => completeRead(reads.length - 1));
      }
    },
  });
  const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8')
    .replace(/^startApp\(\);$/m, '');
  vm.runInContext(source, context);
  context.respond = async () => response(null);
  context.request = async (url, body) => {
    calls.push({url, body});
    return context.respond(url, body);
  };
  context.currentUser = {id: 7, role, username: 'candidate_test', display_name: '我的名字'};
  vm.runInContext('api=(...args)=>request(...args); configureUser(currentUser);', context);
  const select = (filename = '我的简历.pdf', content = Buffer.from([0, 255, 128, 65]), declaredSize) => {
    const file = {name: filename, size: declaredSize ?? content.length, type: pdfType, content};
    element('resume-file').files = [file];
    element('resume-file').value = filename;
    return file;
  };
  const saved = value => { context.savedResume = value; vm.runInContext('state.resume=savedResume;renderResume()', context); };
  return {element, context, calls, reads, select, saved, completeRead};
}

test('candidate resume navigation hides job filters and reads only its own metadata', async () => {
  const {element, context, calls} = resumePage();
  element('nav-resume').listeners.click();
  await settle();
  assert.equal(vm.runInContext('state.view', context), 'resume');
  assert.equal(element('resume').hidden, false);
  assert.equal(element('.scope-bar').hidden, true);
  assert.equal(element('scope-note').hidden, true);
  assert.deepEqual(calls.map(call => call.url), ['/api/resume']);
  assert.equal(element('resume-current').hidden, true);
  assert.match(element('resume-empty').textContent, /还没有上传/);
});

test('non-candidates cannot open, read or upload resumes through frontend actions', async () => {
  for (const role of ['admin', 'boss', 'hr']) {
    const {element, context, calls, reads, select} = resumePage({role});
    select();
    await vm.runInContext('loadResume();uploadResume();switchView("resume");', context);
    assert.equal(element('nav-resume').hidden, true);
    assert.equal(vm.runInContext('state.view', context), 'overview');
    assert.equal(calls.length, 0);
    assert.equal(reads.length, 0);
  }
});

test('choosing a file never uploads it and invalid files are rejected before reading', async () => {
  const {element, context, calls, reads, select} = resumePage();
  select();
  element('resume-file').listeners.change();
  await settle();
  assert.equal(calls.length, 0);
  assert.equal(reads.length, 0);
  assert.match(element('resume-selection').textContent, /我的简历.pdf/);
  for (const [filename, size] of [['wrong.txt', 10], ['empty.pdf', 0], ['large.docx', maxBytes + 1]]) {
    select(filename, Buffer.from('x'), size);
    await vm.runInContext('uploadResume()', context);
    assert.equal(calls.length, 0);
    assert.equal(reads.length, 0);
    assert.equal(element('resume-notice').hidden, false);
    assert.equal(element('resume-notice').className, 'notice error');
  }
  element('resume-file').files = [];
  await vm.runInContext('uploadResume()', context);
  assert.match(element('resume-notice').textContent, /先选择/);
});

test('explicit upload sends binary-safe base64 and safely displays saved metadata', async () => {
  const {element, context, calls, select} = resumePage();
  const file = select('new.DOCX');
  const hostileFilename = '<img src=x onerror=alert(1)>.docx';
  context.respond = async () => response({...metadata(hostileFilename, file.size),
    media_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'});
  let prevented = false;
  element('resume-form').listeners.submit({preventDefault() { prevented = true; }});
  await settle();
  assert.equal(prevented, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/resume');
  assert.equal(calls[0].body.filename, 'new.DOCX');
  assert.equal(calls[0].body.content_base64, file.content.toString('base64'));
  assert.equal(element('resume-filename').textContent, hostileFilename);
  assert.equal(element('resume-filename').innerHTML, '');
  assert.equal(element('resume-type').textContent, 'DOCX');
  assert.match(element('resume-size').textContent, /4 字节/);
  assert.equal(element('resume-current').hidden, false);
  assert.equal(element('resume-empty').hidden, true);
  assert.equal(element('resume-file').value, '');
  assert.equal(element('resume-submit').disabled, false);
  const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
  assert.match(html, /id="resume-download"[^>]*href="\/api\/resume\/download"[^>]*download/);
});

test('the exact 2 MiB limit is accepted without an off-by-one rejection', async () => {
  const {context, calls, select} = resumePage();
  select('boundary.pdf', Buffer.alloc(maxBytes));
  context.respond = async () => response(metadata('boundary.pdf', maxBytes));
  await vm.runInContext('uploadResume()', context);
  assert.equal(calls.length, 1);
  assert.equal(Buffer.from(calls[0].body.content_base64, 'base64').length, maxBytes);
});

test('failed uploads preserve the saved original and selection for an explicit retry', async () => {
  const {element, context, calls, select, saved} = resumePage();
  saved(metadata('old.pdf'));
  select('replacement.pdf');
  context.respond = async () => { throw new Error('文件内容不符合 PDF 格式'); };
  await vm.runInContext('uploadResume()', context);
  await settle();
  assert.equal(calls.length, 1);
  assert.equal(element('resume-filename').textContent, 'old.pdf');
  assert.equal(element('resume-current').hidden, false);
  assert.equal(element('resume-file').value, 'replacement.pdf');
  assert.equal(element('resume-file').disabled, false);
  assert.equal(element('resume-submit').disabled, false);
  assert.match(element('resume-notice').textContent, /文件内容不符合/);
  context.respond = async () => response(metadata('replacement.pdf'));
  await vm.runInContext('uploadResume()', context);
  assert.equal(calls.length, 2);
  assert.equal(element('resume-filename').textContent, 'replacement.pdf');
});

test('file read failures preserve the old resume and never send partial data', async () => {
  const {element, context, calls, reads, select, saved} = resumePage({deferredRead: true});
  saved(metadata('old.pdf'));select();
  const pending = vm.runInContext('uploadResume()', context);
  reads[0].reader.onerror();
  await pending;
  assert.equal(calls.length, 0);
  assert.equal(element('resume-filename').textContent, 'old.pdf');
  assert.match(element('resume-notice').textContent, /文件读取失败/);
  assert.equal(element('resume-submit').disabled, false);
  assert.equal(element('resume-refresh').disabled, false);
});

test('uploading blocks repeated submits and refreshes while the file is still reading', async () => {
  const {element, context, calls, reads, select, completeRead} = resumePage({deferredRead: true});
  select();context.respond = async () => response(metadata('saved.pdf'));
  const first = vm.runInContext('uploadResume()', context);
  await vm.runInContext('uploadResume();loadResume()', context);
  assert.equal(reads.length, 1);
  assert.equal(calls.length, 0);
  assert.equal(element('resume-submit').disabled, true);
  assert.equal(element('resume-file').disabled, true);
  assert.equal(element('resume-refresh').disabled, true);
  completeRead(0);await first;
  assert.equal(calls.length, 1);
  assert.equal(element('resume-submit').disabled, false);
});

test('a delayed read cannot overwrite the result or notice of a successful upload', async () => {
  for (const failOldRead of [false, true]) {
    const {element, context, select} = resumePage();
    let finishRead, failRead;
    context.respond = (url, body) => body ? Promise.resolve(response(metadata('new.pdf')))
      : new Promise((resolve, reject) => { finishRead = resolve;failRead = reject; });
    const oldRead = vm.runInContext('loadResume()', context);
    select('new.pdf');await vm.runInContext('uploadResume()', context);
    if (failOldRead) failRead(new Error('过期请求失败'));
    else finishRead(response(metadata('old.pdf')));
    await oldRead;
    assert.equal(element('resume-filename').textContent, 'new.pdf');
    assert.match(element('resume-notice').textContent, /简历已保存/);
    assert.equal(element('resume-refresh').disabled, false);
  }
});

test('a failed metadata refresh retains the original and permits another refresh', async () => {
  const {element, context, calls, saved} = resumePage();
  saved(metadata('retained.pdf'));
  context.respond = async () => { throw new TypeError('Failed to fetch'); };
  await vm.runInContext('loadResume()', context);
  assert.equal(element('resume-filename').textContent, 'retained.pdf');
  assert.equal(element('resume-refresh').disabled, false);
  assert.equal(element('resume-notice').textContent, '网络连接异常，请稍后重试。');
  context.respond = async () => response(metadata('retained.pdf'));
  await vm.runInContext('loadResume()', context);
  assert.equal(calls.length, 2);
  assert.equal(element('resume-notice').hidden, true);
});
