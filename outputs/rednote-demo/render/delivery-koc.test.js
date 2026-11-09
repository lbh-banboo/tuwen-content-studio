/* render/delivery-koc.test.js —— KOC 渲染层 Node 单测
 * 运行：node render/delivery-koc.test.js（零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./delivery-koc.js');

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

/* 形状与 tools/dev/mock_m4/samples.json 的 koc_records 一致 */
const FIXTURE = [
  {
    id: 'aaaaaaaa-1111-4111-8111-111111111111',
    projectId: '00000000-0000-4000-8000-000000000001',
    displayName: '素人甲（虚构）',
    platformHomeUrl: 'https://www.xiaohongshu.com/user/profile/demo001',
    homeUrlCanonical: 'https://www.xiaohongshu.com/user/profile/demo001',
    contactRef: 'koc-contact/demo001'
  },
  {
    id: 'bbbbbbbb-2222-4222-8222-222222222222',
    projectId: '00000000-0000-4000-8000-000000000001',
    displayName: '阿栗的日常',
    platformHomeUrl: null,
    homeUrlCanonical: null,
    contactRef: null
  }
];

/* ---------------------------- 正常列表 ---------------------------- */

test('renderKocList: 正常列表输出 N 张 KOC 卡片', () => {
  const html = R.renderKocList(FIXTURE);
  assert.equal(occurrences(html, 'class="koc"'), 2);
  assert.ok(html.includes('素人甲（虚构）'));
  assert.ok(html.includes('阿栗的日常'));
});

test('renderKocRow: 展示名 / 首字头像 / 短 id / 主页链接 / 联系引用', () => {
  const html = R.renderKocRow(FIXTURE[0]);
  assert.ok(html.includes('<span class="kav">素</span>'));
  assert.ok(html.includes('title="aaaaaaaa-1111-4111-8111-111111111111"'));
  assert.ok(html.includes('aaaaaaaa…1111'));
  assert.ok(html.includes('href="https://www.xiaohongshu.com/user/profile/demo001"'));
  assert.ok(html.includes('www.xiaohongshu.com/user/profile/demo001'));
  assert.ok(html.includes('联系引用 koc-contact/demo001'));
  assert.ok(html.includes('target="_blank" rel="noopener noreferrer"'));
});

test('renderKocRow: 无主页链接时显示占位，不输出空 href', () => {
  const html = R.renderKocRow(FIXTURE[1]);
  assert.ok(html.includes('未填写主页链接'));
  assert.ok(!html.includes('href=""'));
});

/* ---------------------------- 空态 / 加载 / 错误态 ---------------------------- */

test('renderKocEmpty: 空态文案', () => {
  const html = R.renderKocEmpty();
  assert.ok(html.includes('暂无 KOC 记录'));
  assert.ok(html.includes('新增 KOC 后会出现在这里'));
});

test('renderKocList: 空数组与非法输入落到空态', () => {
  assert.ok(R.renderKocList([]).includes('暂无 KOC 记录'));
  assert.ok(R.renderKocList(null).includes('暂无 KOC 记录'));
  assert.ok(R.renderKocList('oops').includes('暂无 KOC 记录'));
});

test('renderKocLoading: 加载态文案', () => {
  const html = R.renderKocLoading();
  assert.ok(html.includes('正在读取 KOC 记录…'));
  assert.ok(html.includes('fa-spin'));
});

test('renderKocError: 离线错误展示 code / message / 重试按钮', () => {
  const html = R.renderKocError({
    code: 'DEPENDENCY_UNAVAILABLE',
    message: '连不上后端（network_error）。',
    offline: true
  });
  assert.ok(html.includes('DEPENDENCY_UNAVAILABLE'));
  assert.ok(html.includes('连不上后端'));
  assert.ok(html.includes('data-action="retry"'));
  assert.ok(html.includes('确认 mock / 后端已启动'));
});

test('renderKocError: 可重试 / 不可重试 / 裸字符串 / null 分支', () => {
  assert.ok(R.renderKocError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderKocError({ code: 'RESOURCE_NOT_FOUND', retryable: false }).includes('请按错误码排查'));
  assert.ok(R.renderKocError('裸字符串错误').includes('裸字符串错误'));
  assert.ok(R.renderKocError(null).includes('读取 KOC 记录失败'));
});

test('renderKocError: 错误信息 HTML 被转义', () => {
  const html = R.renderKocError({ message: '<script>alert(1)</script>' });
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});

/* ---------------------------- 边界 ---------------------------- */

test('renderKocRow: 空记录 / 缺字段不抛错', () => {
  assert.doesNotThrow(() => R.renderKocRow(null));
  assert.doesNotThrow(() => R.renderKocRow({}));
  const html = R.renderKocRow({});
  assert.ok(html.includes('未命名 KOC'));
  assert.ok(html.includes('未填写主页链接'));
  assert.ok(html.includes('—'));
});

test('renderKocList: 过滤非对象行，保留合法行', () => {
  const html = R.renderKocList([null, 42, FIXTURE[0], undefined]);
  assert.equal(occurrences(html, 'class="koc"'), 1);
});

test('renderKocRow: 超长展示名截断、全量进 title', () => {
  const longName = '长'.repeat(100);
  const html = R.renderKocRow({ displayName: longName });
  assert.ok(html.includes('长'.repeat(40) + '…'));
  assert.ok(html.includes('title="' + longName + '"'));
  assert.ok(!html.includes('>' + longName + '<'));
});

test('renderKocRow: 展示名 / 联系引用 XSS 被转义', () => {
  const html = R.renderKocRow({
    displayName: '"><img src=x onerror=alert(1)>',
    contactRef: '<script>alert(2)</script>'
  });
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});

test('renderKocRow: javascript: 主页链接被白名单挡掉', () => {
  const html = R.renderKocRow({
    displayName: '危险链接',
    platformHomeUrl: 'javascript:alert(1)',
    homeUrlCanonical: 'data:text/html,<script>alert(1)</script>'
  });
  assert.ok(!html.includes('javascript:'));
  assert.ok(!html.includes('data:text/html'));
  assert.ok(html.includes('未填写主页链接'));
});

test('renderKocRow: 协议相对地址 //evil.com 也被挡掉', () => {
  const html = R.renderKocRow({ displayName: '相对地址', platformHomeUrl: '//evil.com/x' });
  assert.ok(!html.includes('//evil.com'));
});

/* ---------------------------- 纯度（机械证明） ---------------------------- */

test('源码不含 DOM / 浏览器全局读取（delivery-koc.js + shared.js）', () => {
  const files = ['delivery-koc.js', 'shared.js'];
  files.forEach(function (file) {
    const src = fs.readFileSync(path.join(__dirname, file), 'utf8');
    assert.equal(/document\.|window\./.test(src), false, file + ' 命中了 DOM 全局');
  });
});

/* ---------------------------- 汇总 ---------------------------- */

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) {
  process.exitCode = 1;
}
