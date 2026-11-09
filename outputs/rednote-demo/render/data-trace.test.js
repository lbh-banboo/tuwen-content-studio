/* render/data-trace.test.js —— 发布登记核对 / 四段留痕降级 Node 单测
 * 运行：node render/data-trace.test.js（零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./data-trace.js');

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
  }
];

/* ---------------------------- 发布登记核对 ---------------------------- */

test('renderPublicationCheckList: 正常列表输出表格与 N 行', () => {
  const html = R.renderPublicationCheckList(FIXTURE);
  assert.ok(html.startsWith('<div class="table-wrap">'));
  assert.ok(html.includes('<table class="tbl tbl-compact"'));
  assert.equal(occurrences(html, '<tr'), 3); // 表头 + 2 行
});

test('renderPublicationCheckRow: 发布记录 / 交付记录 / 链接 / 时间 / 两个枚举', () => {
  const html = R.renderPublicationCheckRow(FIXTURE[0]);
  assert.ok(html.includes('PUB-2026-0001'));
  assert.ok(html.includes('22222222…2222'));
  assert.ok(html.includes('href="https://www.xiaohongshu.com/explore/abc123"'));
  assert.ok(html.includes('标识 <span class="mono">note-demo-0001</span>'));
  assert.ok(html.includes('2026-10-07 12:00'));
  assert.ok(html.includes('已发布'));
  assert.ok(html.includes('已核对'));
});

test('renderPublicationCheckRow: UNKNOWN/UNVERIFIED tr-warn，DRAFT tr-off', () => {
  assert.ok(R.renderPublicationCheckRow(FIXTURE[0]).startsWith('<tr>'));
  assert.ok(R.renderPublicationCheckRow(FIXTURE[1]).startsWith('<tr class="tr-warn">'));
  const draft = Object.assign({}, FIXTURE[0], { state: 'DRAFT' });
  assert.ok(R.renderPublicationCheckRow(draft).startsWith('<tr class="tr-off">'));
});

test('renderIdentitySummary: 身份绑定计数由真实字段推导', () => {
  const html = R.renderIdentitySummary(FIXTURE);
  assert.ok(html.includes('1 / 2 已绑定真实发布版本'));
  assert.ok(html.includes('1 条待补发布身份'));
  assert.ok(html.includes('1 条发布稿未核对'));
  assert.ok(html.includes('变动来源'));
  assert.ok(html.includes('无数据源'));
});

test('renderTraceUnavailable: 明确标注无数据源，不输出示例策略数字', () => {
  const html = R.renderTraceUnavailable();
  assert.ok(html.includes('该区块无数据源'));
  assert.ok(html.includes('/audit-events'));
  assert.ok(html.includes('不展示示例数据'));
  assert.ok(!html.includes('ST-011'));
  assert.ok(!html.includes('2 / 2'));
});

/* ---------------------------- 空态 / 加载 / 错误态 ---------------------------- */

test('renderPublicationCheckEmpty: 空态文案', () => {
  assert.ok(R.renderPublicationCheckEmpty().includes('暂无发布登记'));
});

test('renderPublicationCheckList: 空数组与非法输入落到空态', () => {
  assert.ok(R.renderPublicationCheckList([]).includes('暂无发布登记'));
  assert.ok(R.renderPublicationCheckList(null).includes('暂无发布登记'));
  assert.ok(R.renderPublicationCheckList('oops').includes('暂无发布登记'));
});

test('renderPublicationCheckLoading: 加载态文案', () => {
  assert.ok(R.renderPublicationCheckLoading().includes('正在读取发布登记…'));
});

test('renderPublicationCheckError: 离线错误 / 可重试 / 裸字符串 / null', () => {
  const offline = R.renderPublicationCheckError({
    code: 'DEPENDENCY_UNAVAILABLE', message: '连不上后端。', offline: true
  });
  assert.ok(offline.includes('DEPENDENCY_UNAVAILABLE'));
  assert.ok(offline.includes('data-action="retry"'));
  assert.ok(R.renderPublicationCheckError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderPublicationCheckError({ code: 'RESOURCE_NOT_FOUND', retryable: false }).includes('请按错误码排查'));
  assert.ok(R.renderPublicationCheckError('裸字符串错误').includes('裸字符串错误'));
  assert.ok(R.renderPublicationCheckError(null).includes('读取发布登记失败'));
});

test('renderPublicationCheckError: 错误信息 HTML 被转义', () => {
  const html = R.renderPublicationCheckError({ message: '<script>alert(1)</script>' });
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});

/* ---------------------------- 边界 ---------------------------- */

test('renderPublicationCheckRow: 空记录 / 缺字段不抛错', () => {
  assert.doesNotThrow(() => R.renderPublicationCheckRow(null));
  assert.doesNotThrow(() => R.renderPublicationCheckRow({}));
  const html = R.renderPublicationCheckRow({});
  assert.ok(html.includes('未登记链接'));
  assert.ok(html.includes('—'));
});

test('renderPublicationCheckList: 过滤非对象行，保留合法行', () => {
  const html = R.renderPublicationCheckList([null, 42, FIXTURE[0]]);
  assert.equal(occurrences(html, '<tr'), 2);
});

test('renderPublicationCheckRow: 超长 key 截断、全量进 title', () => {
  const longKey = 'K'.repeat(100);
  const html = R.renderPublicationCheckRow({ publicationKey: longKey });
  assert.ok(html.includes('K'.repeat(26) + '…'));
  assert.ok(html.includes('title="' + longKey + '"'));
  assert.ok(!html.includes('>' + longKey + '<'));
});

test('renderPublicationCheckRow: key / noteId XSS 被转义', () => {
  const html = R.renderPublicationCheckRow({
    publicationKey: '"><img src=x onerror=alert(1)>',
    noteId: '<script>alert(2)</script>'
  });
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});

test('renderPublicationCheckRow: javascript: 链接被白名单挡掉', () => {
  const html = R.renderPublicationCheckRow({
    publicationKey: 'PUB-X',
    noteUrl: 'javascript:alert(1)',
    urlCanonical: 'javascript:alert(2)',
    noteId: 'n1'
  });
  assert.ok(!html.includes('javascript:'));
  assert.ok(html.includes('未登记链接'));
});

test('renderPublicationCheckRow: 未知枚举降级为中性 tag', () => {
  const html = R.renderPublicationCheckRow({
    publicationKey: 'PUB-Z', state: 'FAILED', contentState: 'MAYBE'
  });
  assert.ok(html.includes('FAILED'));
  assert.ok(html.includes('MAYBE'));
  assert.ok(!html.includes('tag-ok') && !html.includes('tag-warn') && !html.includes('tag-danger'));
  assert.ok(html.startsWith('<tr>'));
});

test('renderIdentitySummary: 空列表计数为 0，不抛错', () => {
  const html = R.renderIdentitySummary([]);
  assert.ok(html.includes('0 / 0 已绑定'));
  assert.ok(html.includes('0 条待补'));
  assert.ok(html.includes('0 条发布稿未核对'));
  assert.doesNotThrow(() => R.renderIdentitySummary(null));
});

test('renderTraceUnavailable: detail 文本被转义', () => {
  const html = R.renderTraceUnavailable('<script>alert(1)</script>');
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});

test('源码不含 DOM / 浏览器全局读取（data-trace.js + shared.js）', () => {
  ['data-trace.js', 'shared.js'].forEach(function (file) {
    const src = fs.readFileSync(path.join(__dirname, file), 'utf8');
    assert.equal(/document\.|window\./.test(src), false, file + ' 命中了 DOM 全局');
  });
});

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) process.exitCode = 1;
