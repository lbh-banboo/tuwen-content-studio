/* render/data-feedback.test.js —— 发布登记渲染层 Node 单测
 * 运行：node render/data-feedback.test.js（零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./data-feedback.js');

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

/* 形状与 tools/dev/mock_m4/samples.json 的 publications 一致 */
const FIXTURE = [
  {
    id: 'pppppppp-1111-4111-8111-111111111111',
    projectId: '00000000-0000-4000-8000-000000000001',
    deliveryId: '22222222-2222-4222-8222-222222222222',
    publicationKey: 'PUB-2026-0001',
    noteId: 'note-demo-0001',
    noteUrl: 'https://www.xiaohongshu.com/explore/abc123?xsec_token=XYZ',
    urlCanonical: 'https://www.xiaohongshu.com/explore/abc123',
    state: 'PUBLISHED',
    contentState: 'VERIFIED',
    publishedAt: '2026-10-07T12:00:00+08:00'
  },
  {
    id: 'qqqqqqqq-2222-4222-8222-222222222222',
    projectId: '00000000-0000-4000-8000-000000000001',
    deliveryId: null,
    publicationKey: 'PUB-2026-0002',
    noteId: null,
    noteUrl: null,
    urlCanonical: null,
    state: 'UNKNOWN',
    contentState: 'UNVERIFIED',
    publishedAt: null
  },
  {
    id: 'rrrrrrrr-3333-4333-8333-333333333333',
    projectId: '00000000-0000-4000-8000-000000000001',
    deliveryId: '44444444-4444-4444-8444-444444444444',
    publicationKey: 'PUB-2026-0003',
    noteId: 'note-demo-0003',
    noteUrl: 'https://www.xiaohongshu.com/explore/def456',
    urlCanonical: 'https://www.xiaohongshu.com/explore/def456',
    state: 'DRAFT',
    contentState: 'VERIFIED',
    publishedAt: null
  }
];

/* ---------------------------- 正常列表 ---------------------------- */

test('renderPublicationList: 正常列表输出完整表格与 N 行', () => {
  const html = R.renderPublicationList(FIXTURE);
  assert.ok(html.startsWith('<div class="table-wrap">'));
  assert.ok(html.includes('<table class="tbl">'));
  assert.ok(html.includes('<thead>') && html.includes('<tbody>'));
  assert.equal(occurrences(html, '<tr'), 4); // 1 表头 + 3 行
});

test('renderPublicationRow: 发布记录 / 交付记录 / 链接 / 时间来自数据', () => {
  const html = R.renderPublicationRow(FIXTURE[0]);
  assert.ok(html.includes('PUB-2026-0001'));
  assert.ok(html.includes('22222222…2222'));
  assert.ok(html.includes('title="22222222-2222-4222-8222-222222222222"'));
  assert.ok(html.includes('href="https://www.xiaohongshu.com/explore/abc123"'));
  assert.ok(html.includes('www.xiaohongshu.com/explore/abc123'));
  assert.ok(html.includes('标识 <span class="mono">note-demo-0001</span>'));
  assert.ok(html.includes('2026-10-07 12:00'));
});

test('renderPublicationRow: state / contentState 映射为 tag', () => {
  const published = R.renderPublicationRow(FIXTURE[0]);
  assert.ok(published.includes('已发布'));
  assert.ok(published.includes('已核对'));
  const unknown = R.renderPublicationRow(FIXTURE[1]);
  assert.ok(unknown.includes('待确认'));
  assert.ok(unknown.includes('未核对'));
  const draft = R.renderPublicationRow(FIXTURE[2]);
  assert.ok(draft.includes('待发布'));
});

test('renderPublicationRow: 行色 —— UNKNOWN/UNVERIFIED tr-warn，DRAFT tr-off，正常无标记', () => {
  assert.ok(R.renderPublicationRow(FIXTURE[0]).startsWith('<tr>'));
  assert.ok(R.renderPublicationRow(FIXTURE[1]).startsWith('<tr class="tr-warn">'));
  assert.ok(R.renderPublicationRow(FIXTURE[2]).startsWith('<tr class="tr-off">'));

  const publishedUnverified = Object.assign({}, FIXTURE[0], { contentState: 'UNVERIFIED' });
  assert.ok(R.renderPublicationRow(publishedUnverified).startsWith('<tr class="tr-warn">'));
});

/* ---------------------------- 空态 / 加载 / 错误态 ---------------------------- */

test('renderPublicationEmpty: 空态跨 6 列 + 文案', () => {
  const html = R.renderPublicationEmpty();
  assert.ok(html.includes('colspan="6"'));
  assert.ok(html.includes('暂无发布登记'));
});

test('renderPublicationList: 空数组与非法输入落到空态', () => {
  assert.ok(R.renderPublicationList([]).includes('暂无发布登记'));
  assert.ok(R.renderPublicationList(null).includes('暂无发布登记'));
  assert.ok(R.renderPublicationList(undefined).includes('暂无发布登记'));
});

test('renderPublicationLoading: 加载态含表格与提示', () => {
  const html = R.renderPublicationLoading();
  assert.ok(html.includes('<table class="tbl">'));
  assert.ok(html.includes('正在读取发布登记…'));
});

test('renderPublicationError: 离线错误展示 code / message / 重试按钮', () => {
  const html = R.renderPublicationError({
    code: 'DEPENDENCY_UNAVAILABLE',
    message: '连不上后端（network_error）。',
    offline: true
  });
  assert.ok(html.includes('tr-danger'));
  assert.ok(html.includes('DEPENDENCY_UNAVAILABLE'));
  assert.ok(html.includes('连不上后端'));
  assert.ok(html.includes('data-action="retry"'));
});

test('renderPublicationError: 可重试 / 不可重试 / 裸字符串 / null 分支', () => {
  assert.ok(R.renderPublicationError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderPublicationError({ code: 'RESOURCE_NOT_FOUND', retryable: false }).includes('请按错误码排查'));
  assert.ok(R.renderPublicationError('裸字符串错误').includes('裸字符串错误'));
  assert.ok(R.renderPublicationError(null).includes('读取发布登记失败'));
});

test('renderPublicationError: 错误信息 HTML 被转义', () => {
  const html = R.renderPublicationError({ message: '<script>alert(1)</script>' });
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});

/* ---------------------------- 边界 ---------------------------- */

test('renderPublicationRow: 空记录 / 缺字段不抛错，空值显示 —', () => {
  assert.doesNotThrow(() => R.renderPublicationRow(null));
  assert.doesNotThrow(() => R.renderPublicationRow({}));
  const html = R.renderPublicationRow({});
  assert.ok(html.includes('未登记链接'));
  assert.equal(occurrences(html, '>—<'), 5); // 发布记录 / 交付记录 / 时间 / 状态 / 内容核对 共 5 处
});

test('renderPublicationList: 过滤非对象行，保留合法行', () => {
  const html = R.renderPublicationList([null, 42, FIXTURE[0]]);
  assert.equal(occurrences(html, '<tr'), 2);
});

test('renderPublicationRow: 超长 publicationKey 截断、全量进 title', () => {
  const longKey = 'K'.repeat(100);
  const html = R.renderPublicationRow({ publicationKey: longKey });
  assert.ok(html.includes('K'.repeat(26) + '…'));
  assert.ok(html.includes('title="' + longKey + '"'));
  assert.ok(!html.includes('>' + longKey + '<'));
});

test('renderPublicationRow: publicationKey / noteId XSS 被转义', () => {
  const html = R.renderPublicationRow({
    publicationKey: '"><img src=x onerror=alert(1)>',
    noteId: '<script>alert(2)</script>'
  });
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});

test('renderPublicationRow: javascript: 链接被白名单挡掉', () => {
  const html = R.renderPublicationRow({
    publicationKey: 'PUB-X',
    noteUrl: 'javascript:alert(1)',
    urlCanonical: 'javascript:alert(2)',
    noteId: 'n1'
  });
  assert.ok(!html.includes('javascript:'));
  assert.ok(html.includes('未登记链接'));
});

test('renderPublicationRow: 未知枚举降级为中性 tag，不误映射', () => {
  const html = R.renderPublicationRow({
    publicationKey: 'PUB-Z',
    state: 'FAILED',
    contentState: 'MAYBE'
  });
  assert.ok(html.includes('FAILED'));
  assert.ok(html.includes('MAYBE'));
  assert.ok(!html.includes('tag-ok') && !html.includes('tag-warn') && !html.includes('tag-danger'));
  assert.ok(html.startsWith('<tr>'));
});

test('renderPublicationRow: 已归一链接优先用 urlCanonical 作为 href', () => {
  const html = R.renderPublicationRow(FIXTURE[0]);
  assert.ok(html.includes('href="https://www.xiaohongshu.com/explore/abc123"'));
  assert.ok(!html.includes('href="https://www.xiaohongshu.com/explore/abc123?xsec_token=XYZ"'));
});

/* ---------------------------- 纯度（机械证明） ---------------------------- */

test('源码不含 DOM / 浏览器全局读取（data-feedback.js + shared.js）', () => {
  const files = ['data-feedback.js', 'shared.js'];
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
