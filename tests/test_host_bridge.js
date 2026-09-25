// Run: node --test tests/test_host_bridge.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../ui/host-bridge.js'), 'utf8');
const actions = ['license_status', 'activate_license', 'import_sections', 'choose_folder', 'scan_assets', 'import_assets', 'map_images'];

function harness(send) {
  const handlers = new Map();
  const timers = new Map();
  const calls = [];
  let timerId = 0;
  const context = vm.createContext({window: {},
    setTimeout: callback => { timers.set(++timerId, callback); return timerId; },
    clearTimeout: id => timers.delete(id)});
  vm.runInContext(source, context);
  const sdk = {
    onAction(action, callback) {
      assert.equal(handlers.has(action), false, 'only one callback per action');
      handlers.set(action, callback);
    },
    sendAction(action, data, options) {
      calls.push({action, data, options});
      return send && send(action, data, options, handlers.get(action));
    }
  };
  return {request: context.window.createLingyunHostBridge(sdk), sdk, calls, timers,
    reply: (action, result) => handlers.get(action)(result),
    expire: () => [...timers.values()].forEach(callback => callback())};
}

test('旧 SDK 返回 undefined：七个动作都等待回调，每个动作只发一次', async () => {
  const h = harness();
  for (const action of actions) {
    const expected = {ok: true, data: {action}};
    let settled = false;
    const request = h.request(action, {test: true}, {timeoutMs: 15000});
    request.then(() => { settled = true; });
    await Promise.resolve();
    assert.equal(settled, false);
    h.reply(action, expected);
    assert.equal(await request, expected);
  }
  assert.equal(h.calls.length, actions.length);
  assert.equal(h.timers.size, 0);
});

test('旧 SDK 同步回调不会在发送前丢失，连续查询不累积监听器', async () => {
  const expected = {ok: true, data: {active: false, machine_code: 'LY-TEST'}};
  const h = harness((action, data, options, reply) => { reply(expected); });
  assert.equal(await h.request('license_status', {}), expected);
  assert.equal(await h.request('license_status', {}), expected);
  assert.equal(h.calls.length, 2);
  assert.equal(h.timers.size, 0);
});

test('新 SDK 的 Promise 结果优先于同步和异步广播回调', async () => {
  let complete;
  const h = harness((action, data, options, reply) => {
    reply({ok: false, error: '旧回调'});
    return new Promise(resolve => { complete = resolve; });
  });
  const request = h.request('activate_license', {code: 'TEST'}, {timeoutMs: 30000});
  h.reply('activate_license', {ok: false, error: '重复广播'});
  const expected = {ok: true, data: {active: true, added_days: 30}};
  complete(expected);
  assert.equal(await request, expected);
  h.reply('activate_license', {ok: false, error: '迟到广播'});
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].options.timeoutMs, 30000);
  assert.equal(h.timers.size, 0);
});

test('不重发失败请求，并保留后端错误和授权门禁', async () => {
  const h = harness();
  const request = h.request('import_assets', {});
  const error = {ok: false, error: '授权已到期', license_required: true};
  h.reply('import_assets', error);
  assert.equal(await request, error);
  assert.equal(h.calls.length, 1);
  for (const send of [() => { throw new Error('发送失败'); }, () => Promise.reject(new Error('发送失败'))]) {
    const failed = harness(send);
    await assert.rejects(failed.request('license_status', {}), /发送失败/);
    assert.equal(failed.calls.length, 1);
    assert.equal(failed.timers.size, 0);
  }
});

test('不同动作分别等待，同名并发请求不会重复发送', async () => {
  const h = harness();
  const status = h.request('license_status', {});
  const scan = h.request('scan_assets', {});
  await assert.rejects(h.request('scan_assets', {}), /请勿重复提交/);
  h.reply('scan_assets', {ok: true, data: 'scan'});
  h.reply('license_status', {ok: true, data: 'status'});
  assert.equal((await scan).data, 'scan');
  assert.equal((await status).data, 'status');
  assert.equal(h.calls.length, 2);
});

test('旧 SDK 超时可见，迟到回调不会错配给重试请求', async () => {
  const h = harness();
  const request = h.request('activate_license', {});
  const rejected = assert.rejects(request, /等待宿主返回超时/);
  h.expire();
  await rejected;
  await assert.rejects(h.request('activate_license', {}), /上次操作超时/);
  assert.equal(h.calls.length, 1);
  h.reply('activate_license', {ok: true, data: 'late'});
  const next = h.request('activate_license', {});
  h.reply('activate_license', {ok: true, data: 'new'});
  assert.equal((await next).data, 'new');
  assert.equal(h.timers.size, 0);
});

test('新 SDK 超时后的旧 Promise 和广播不影响下一次请求', async () => {
  const completions = [];
  const h = harness(() => new Promise(resolve => completions.push(resolve)));
  const first = h.request('license_status', {});
  const rejected = assert.rejects(first, /等待宿主返回超时/);
  h.expire();
  await rejected;
  const next = h.request('license_status', {});
  completions[0]({ok: true, data: 'old'});
  h.reply('license_status', {ok: true, data: 'old broadcast'});
  completions[1]({ok: true, data: 'new'});
  assert.equal((await next).data, 'new');
});

test('缺失 SDK、缺失回调接口和无效回传有明确错误', async () => {
  const context = vm.createContext({window: {}, setTimeout, clearTimeout});
  vm.runInContext(source, context);
  await assert.rejects(context.window.createLingyunHostBridge(null)('license_status'), /未连接/);
  const h = harness();
  delete h.sdk.onAction;
  await assert.rejects(h.request('license_status'), /缺少插件结果回调接口/);
  assert.equal(h.timers.size, 0);
  for (const result of [undefined, null, 'invalid', []]) {
    const invalid = harness();
    const request = invalid.request('license_status');
    invalid.reply('license_status', result);
    await assert.rejects(request, /结果格式无效/);
    assert.equal(invalid.timers.size, 0);
  }
});
