/* render/index.test.js —— 登录 / 导览落地页（方案 A 纯函数）Node 单测
 * 运行：node render/index.test.js   （零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./index.js');

let passed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log('[PASS] ' + name);
  } catch (err) {
    failures.push({ name, err });
    console.log('[FAIL] ' + name + '\n       ' + err.message);
  }
}

function occurrences(haystack, needle) {
  return haystack.split(needle).length - 1;
}

/* 形状与真后端 GET /me 一致 */
const ME = {
  id: '365bcf69-fc8b-5dc1-9144-00cc975daeb8',
  displayName: 'member06@example.com',
  role: 'ADMIN',
  csrfToken: 'secret-csrf-token-value',
  sessionId: 'secret-session-id-value',
  capabilities: {
    enabledFormats: ['TEXT_ONLY', 'STATIC', 'LIVE', 'MIXED'],
    localModels: false,
    allowedExternalProviders: [],
    liveProfiles: [],
    importFormats: ['CSV', 'XLSX', 'SCREENSHOT'],
    limits: {
      trialQuantity: 10,
      scaleMax: 30,
      bulkMax: 50,
      defaultPageLimit: 20,
      maxPageLimit: 100
    }
  }
};

/* ---------------------------- 正常：GET /me ---------------------------- */

test('renderSession: 身份与能力来自 /me', () => {
  const html = R.renderSession(ME);
  assert.ok(html.includes('已登录'));
  assert.ok(html.includes('member06@example.com'));
  assert.ok(html.includes('管理员'));
  assert.ok(html.includes('纯文本') && html.includes('Live 图'));
  assert.ok(html.includes('试产 10 条 · 扩产 ≤30 · 批产 ≤50'));
  assert.ok(html.includes('本地模型不可用'));
  assert.ok(html.includes('allowedExternalProviders 为空'));
  assert.ok(html.includes('workbench.v2.html') && html.includes('projects.v2.html'));
});

test('renderSession: csrfToken / sessionId 绝不进入 HTML', () => {
  const html = R.renderSession(ME);
  assert.equal(html.includes(ME.csrfToken), false);
  assert.equal(html.includes(ME.sessionId), false);
  assert.ok(html.includes('不展示 csrfToken / sessionId'));
});

test('renderSession: 外部模型列表非空时逐项展示', () => {
  const me = JSON.parse(JSON.stringify(ME));
  me.capabilities.allowedExternalProviders = ['OPENAI', 'ANTHROPIC'];
  const html = R.renderSession(me);
  assert.ok(html.includes('OPENAI') && html.includes('ANTHROPIC'));
});

test('renderSession: OPERATOR 角色映射为「运营」', () => {
  const me = JSON.parse(JSON.stringify(ME));
  me.role = 'OPERATOR';
  assert.ok(R.renderSession(me).includes('运营'));
});

/* ---------------------------- 登录表单 / 结果 ---------------------------- */

test('renderLoginForm: 默认邮箱进入 value，表单可提交', () => {
  const html = R.renderLoginForm({ email: 'member06@example.com' });
  assert.ok(html.includes('<form id="login-form"'));
  assert.ok(html.includes('name="email"'));
  assert.ok(html.includes('value="member06@example.com"'));
  assert.ok(html.includes('type="submit"'));
  assert.ok(html.includes('/dev/auth/login'));
});

test('renderLoginForm: 邮箱里的引号被转义（防属性逃逸）', () => {
  const html = R.renderLoginForm({ email: '" onfocus="alert(1)' });
  assert.ok(!html.includes('onfocus="alert(1)'));
  assert.ok(html.includes('&quot;'));
});

test('renderLoginForm: 不渲染密码字段（dev 旁路只收邮箱）', () => {
  const html = R.renderLoginForm({});
  assert.equal(html.includes('type="password"'), false);
  assert.equal(html.includes('name="password"'), false);
});

test('renderLoginBusy: 显示正在登录的邮箱', () => {
  const html = R.renderLoginBusy('a@b.cn');
  assert.ok(html.includes('正在登录 a@b.cn'));
});

test('renderLoginResult: 使用登录响应字段，不推测 capabilities', () => {
  const html = R.renderLoginResult({
    userId: 'u-1',
    displayName: '林知远',
    role: 'ADMIN',
    csrfToken: 'secret-csrf-token-value',
    expiresAt: '2026-10-17T10:38:23+00:00'
  });
  assert.ok(html.includes('登录成功') && html.includes('林知远') && html.includes('管理员'));
  assert.ok(html.includes('2026-10-17T10:38:23+00:00'));
  assert.ok(html.includes('完整能力可在 GET /me 读取'));
  assert.equal(html.includes('secret-csrf-token-value'), false);
});

/* ---------------------------- 空 / 错误 ---------------------------- */

test('renderAuthError: 离线错误带 code / message / 重试', () => {
  const html = R.renderAuthError({
    code: 'DEPENDENCY_UNAVAILABLE',
    message: '连不上后端（network_error）。',
    offline: true
  });
  assert.ok(html.includes('DEPENDENCY_UNAVAILABLE'));
  assert.ok(html.includes('连不上后端'));
  assert.ok(html.includes('127.0.0.1:8787'));
  assert.ok(html.includes('data-action="retry"'));
});

test('renderAuthError: 可重试 / 不可重试 / 裸字符串 / null 分支', () => {
  assert.ok(R.renderAuthError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderAuthError({ code: 'PERMISSION_DENIED', retryable: false }).includes('请按错误码排查'));
  assert.ok(R.renderAuthError('裸字符串错误').includes('裸字符串错误'));
  assert.ok(R.renderAuthError(null).includes('登录状态读取失败'));
});

test('renderAuthError: 错误文案里的 HTML 被转义', () => {
  const html = R.renderAuthError({ message: '<img src=x onerror=alert(1)>' });
  assert.equal(html.includes('<img'), false);
  assert.ok(html.includes('&lt;img'));
});

test('renderSessionLoading: 加载态含提示', () => {
  assert.ok(R.renderSessionLoading().includes('正在检查登录态…'));
});

/* ---------------------------- 边界 ---------------------------- */

test('renderSession: null / 缺字段不抛错，标「未提供」', () => {
  assert.doesNotThrow(() => R.renderSession(null));
  const html = R.renderSession({});
  assert.ok(html.includes('显示名未提供'));
  assert.ok(html.includes('未提供'));
  assert.ok(html.includes('本地模型不可用'));
});

test('renderSession: 显示名与角色里的 HTML 被转义', () => {
  const me = JSON.parse(JSON.stringify(ME));
  me.displayName = '<script>alert(1)</script>';
  me.role = '"><img src=x onerror=alert(1)>';
  me.capabilities.allowedExternalProviders = ['<b>x</b>'];
  const html = R.renderSession(me);
  assert.equal(html.includes('<script>'), false);
  assert.equal(html.includes('<img'), false);
  assert.equal(html.includes('<b>x</b>'), false);
  assert.ok(html.includes('&lt;script&gt;'));
});

test('renderLoginResult: expiresAt 缺失标「未提供」', () => {
  const html = R.renderLoginResult({ displayName: 'x', role: 'ADMIN' });
  assert.equal(occurrences(html, '未提供') >= 1, true);
});

/* ---------------------------- 纯度（机械证明） ---------------------------- */

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'index.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

/* ---------------------------- 汇总 ---------------------------- */

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) {
  process.exitCode = 1;
}
