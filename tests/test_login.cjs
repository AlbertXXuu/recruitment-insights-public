const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function loginPage(invited = false) {
  const elements = new Map(), calls = [];
  const element = id => {
    if (!elements.has(id)) {
      const listeners = new Map();
      elements.set(id, {
        hidden: id === 'register-form', disabled: false, value: '', textContent: id,
        addEventListener(event, callback) { listeners.set(event, callback); },
        click() { listeners.get('click')?.(); },
        submit() {
          let prevented = false;
          listeners.get('submit')({preventDefault() { prevented = true; }});
          assert.equal(prevented, true);
        },
        focus() { document.activeElement = this; }
      });
    }
    return elements.get(id);
  };
  const document = {body: {dataset: invited ? {registration: 'disabled'} : {}}, getElementById: element, activeElement: null};
  const context = vm.createContext({
    document,
    window: {location: {href: '/login'}},
    fetch: async (url, options) => {
      calls.push({url, options});
      return {ok: true, json: async () => ({role: 'candidate'})};
    }
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/login.js'), 'utf8'), context);
  return {context, element, calls};
}

const settle = () => new Promise(resolve => setImmediate(resolve));
const password = 'Temporary-fixture-42!';

function fillRegistration(element, username = 'new_candidate') {
  element('register-username').value = username;
  element('register-password').value = password;
  element('register-password-confirm').value = password;
  element('register-name').value = '普通用户';
}

test('local registration completes and returns to a usable login form', async () => {
  const {element, context, calls} = loginPage();
  element('switch-auth').click();
  assert.equal(element('login-form').hidden, true);
  assert.equal(element('register-form').hidden, false);
  fillRegistration(element);
  element('password').value = 'stale-login-password';
  element('register-form').submit();
  await settle();
  assert.equal(calls[0].url, '/api/auth/register');
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    username: 'new_candidate', password: 'Temporary-fixture-42!', display_name: '普通用户'
  });
  assert.equal(element('login-form').hidden, false);
  assert.equal(element('register-form').hidden, true);
  assert.equal(element('username').value, 'new_candidate');
  assert.equal(element('register-submit').disabled, false);
  assert.match(element('login-notice').textContent, /创建成功/);
  for (const id of ['password', 'register-password', 'register-password-confirm']) {
    assert.equal(element(id).value, '', id);
  }
  assert.equal(context.document.activeElement, element('password'));
});

test('mismatched passwords explain the problem without sending a request', async () => {
  const {element, context, calls} = loginPage();
  element('switch-auth').click();
  fillRegistration(element);
  element('register-password-confirm').value = 'Different-fixture-43!';
  element('register-form').submit();
  await settle();
  assert.equal(calls.length, 0);
  assert.match(element('login-notice').textContent, /密码不一致/);
  assert.equal(element('login-notice').hidden, false);
  assert.equal(element('login-notice').className, 'notice error');
  assert.equal(context.document.activeElement, element('register-password-confirm'));
  assert.equal(element('register-submit').disabled, false);
  element('register-password-confirm').value = password;
  element('register-form').submit();
  await settle();
  assert.equal(calls.length, 1);
  assert.match(element('login-notice').textContent, /创建成功/);
});

test('a duplicate account preserves non-password input and can be corrected and retried', async () => {
  const {element, context, calls} = loginPage();
  element('switch-auth').click();
  fillRegistration(element, 'already_taken');
  context.fetch = async (url, options) => {
    calls.push({url, options});
    return {ok: false, json: async () => ({error: '账号已存在'})};
  };
  element('register-form').submit();
  await settle();
  assert.equal(element('login-notice').textContent, '账号已存在');
  assert.equal(element('login-notice').hidden, false);
  assert.equal(element('register-form').hidden, false);
  assert.equal(element('register-username').value, 'already_taken');
  assert.equal(element('register-name').value, '普通用户');
  assert.equal(element('register-password').value, '');
  assert.equal(element('register-password-confirm').value, '');
  assert.equal(element('register-submit').disabled, false);
  assert.equal(element('register-submit').textContent, 'register-submit');
  assert.equal(element('switch-auth').disabled, false);
  context.fetch = async (url, options) => {
    calls.push({url, options});
    return {ok: true, json: async () => ({})};
  };
  fillRegistration(element, 'available_name');
  element('register-form').submit();
  await settle();
  assert.equal(calls.length, 2);
  assert.equal(JSON.parse(calls[1].options.body).username, 'available_name');
  assert.equal(element('username').value, 'available_name');
  assert.equal(element('login-form').hidden, false);
  assert.equal(context.document.activeElement, element('password'));
});

test('switching forms clears hidden passwords while retaining account details', () => {
  const {element} = loginPage();
  element('username').value = 'returning_candidate';
  element('password').value = password;
  element('switch-auth').click();
  assert.equal(element('password').value, '');
  fillRegistration(element);
  element('switch-auth').click();
  assert.equal(element('register-password').value, '');
  assert.equal(element('register-password-confirm').value, '');
  assert.equal(element('register-username').value, 'new_candidate');
  assert.equal(element('register-name').value, '普通用户');
  assert.equal(element('username').value, 'returning_candidate');
});

test('pending authentication blocks repeat submissions and form switches', async () => {
  for (const registering of [false, true]) {
    const {element, context, calls} = loginPage();
    if (registering) {
      element('switch-auth').click();
      fillRegistration(element);
    }
    let finish;
    context.fetch = (url, options) => {
      calls.push({url, options});
      return new Promise(resolve => { finish = resolve; });
    };
    const form = registering ? 'register-form' : 'login-form';
    const button = registering ? 'register-submit' : 'login-submit';
    element(form).submit();
    element(form).submit();
    element('switch-auth').click();
    element(registering ? 'login-form' : 'register-form').submit();
    assert.equal(calls.length, 1);
    assert.equal(element(button).disabled, true);
    assert.equal(element('switch-auth').disabled, true);
    assert.equal(element('register-form').hidden, !registering);
    finish({ok: true, json: async () => ({role: 'candidate'})});
    await settle();
    assert.equal(element(button).disabled, false);
    assert.equal(element('switch-auth').disabled, false);
    assert.equal(element('login-form').hidden, false);
  }
});

test('fetch rejection shows a Chinese network error without automatic retries', async () => {
  for (const registering of [false, true]) {
    const {element, context, calls} = loginPage();
    element('username').value = 'returning_candidate';
    if (registering) {
      element('switch-auth').click();
      fillRegistration(element);
    } else element('password').value = password;
    context.fetch = async (url, options) => {
      calls.push({url, options});
      throw new TypeError('Failed to fetch');
    };
    const button = registering ? 'register-submit' : 'login-submit';
    element(registering ? 'register-form' : 'login-form').submit();
    await settle();
    await settle();
    assert.equal(calls.length, 1);
    assert.equal(element('login-notice').textContent, '网络连接异常，请稍后重试。');
    assert.equal(element('login-notice').hidden, false);
    assert.equal(element(button).disabled, false);
    assert.equal(element('switch-auth').disabled, false);
    assert.equal(element('register-form').hidden, !registering);
    assert.equal(element('username').value, 'returning_candidate');
    if (registering) {
      assert.equal(element('register-username').value, 'new_candidate');
      assert.equal(element('register-name').value, '普通用户');
    }
    for (const id of ['password', 'register-password', 'register-password-confirm']) {
      assert.equal(element(id).value, '');
    }
    assert.equal(context.window.location.href, '/login');
  }
});

test('a non-JSON response shows a Chinese service error and restores the form', async () => {
  for (const registering of [false, true]) {
    const {element, context, calls} = loginPage();
    element('username').value = 'returning_candidate';
    if (registering) {
      element('switch-auth').click();
      fillRegistration(element);
    } else element('password').value = password;
    context.fetch = async (url, options) => {
      calls.push({url, options});
      return {ok: true, json: async () => { throw new SyntaxError('Unexpected token < in JSON'); }};
    };
    const button = registering ? 'register-submit' : 'login-submit';
    element(registering ? 'register-form' : 'login-form').submit();
    await settle();
    await settle();
    assert.equal(calls.length, 1);
    assert.equal(element('login-notice').textContent, '服务暂时不可用，请稍后重试。');
    assert.equal(element('login-notice').hidden, false);
    assert.equal(element(button).disabled, false);
    assert.equal(element(button).textContent, button);
    assert.equal(element('switch-auth').disabled, false);
    assert.equal(element('register-form').hidden, !registering);
    assert.equal(element('username').value, 'returning_candidate');
    if (registering) {
      assert.equal(element('register-username').value, 'new_candidate');
      assert.equal(element('register-name').value, '普通用户');
    }
    for (const id of ['password', 'register-password', 'register-password-confirm']) {
      assert.equal(element(id).value, '');
    }
    assert.equal(context.window.location.href, '/login');
  }
});

test('a successful HTTP login without a valid role never navigates', async () => {
  for (const body of [{}, null, {role: 'owner'}, {role: 1}, 'candidate']) {
    const {element, context} = loginPage();
    element('username').value = 'returning_candidate';
    element('password').value = password;
    context.fetch = async () => ({ok: true, json: async () => body});
    element('login-form').submit();
    await settle();
    assert.equal(context.window.location.href, '/login');
    assert.equal(element('login-notice').textContent, '服务暂时不可用，请稍后重试。');
    assert.equal(element('username').value, 'returning_candidate');
    assert.equal(element('password').value, '');
    assert.equal(element('login-submit').disabled, false);
    assert.equal(element('switch-auth').disabled, false);
  }
});

test('invited page refuses programmatic registration while login remains available', async () => {
  const {element, context, calls} = loginPage(true);
  element('switch-auth').click();
  element('register-form').submit();
  await settle();
  assert.equal(element('login-form').hidden, false);
  assert.equal(element('register-form').hidden, true);
  assert.equal(calls.length, 0);
  element('username').value = 'invited_candidate';
  element('password').value = 'Temporary-fixture-42!';
  element('login-form').submit();
  await settle();
  assert.equal(calls[0].url, '/api/auth/login');
  assert.equal(calls[0].options.headers['X-Local-Request'], '1');
  assert.equal(context.window.location.href, '/public');
});

test('both modes send enterprise users to their workspace', async () => {
  for (const [invited, role] of [false, true].flatMap(invited => ['admin', 'boss', 'hr'].map(role => [invited, role]))) {
    const {element, context, calls} = loginPage(invited);
    context.fetch = async (url, options) => {
      calls.push({url, options});
      return {ok: true, json: async () => ({role})};
    };
    element('login-form').submit();
    await settle();
    assert.equal(context.window.location.href, '/enterprise');
    assert.equal(element('login-submit').disabled, false);
  }
});

test('a failed invited login shows useful feedback and allows a retry', async () => {
  const {element, context} = loginPage(true);
  element('username').value = 'invited_candidate';
  element('password').value = password;
  context.fetch = async () => ({ok: false, json: async () => ({error: '账号或密码错误'})});
  element('login-form').submit();
  await settle();
  assert.equal(context.window.location.href, '/login');
  assert.equal(element('login-submit').disabled, false);
  assert.equal(element('login-submit').textContent, 'login-submit');
  assert.equal(element('login-notice').hidden, false);
  assert.equal(element('login-notice').textContent, '账号或密码错误');
  assert.equal(element('username').value, 'invited_candidate');
  assert.equal(element('password').value, '');
  assert.equal(context.document.activeElement, element('password'));
  context.fetch = async () => ({ok: true, json: async () => ({role: 'candidate'})});
  element('password').value = password;
  element('login-form').submit();
  await settle();
  assert.equal(context.window.location.href, '/public');
});
