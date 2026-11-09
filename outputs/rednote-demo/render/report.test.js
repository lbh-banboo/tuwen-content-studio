/* render/report.test.js —— 报告版本 / 结项事件 Node 单测
 * 运行：node render/report.test.js（零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./report.js');

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

const REPORT = {
  id: 'rprprprp-1111-4111-8111-111111111111',
  projectId: '00000000-0000-4000-8000-000000000001',
  reportKind: 'OVERVIEW',
  templateVersion: 'v1',
  currentVersionNo: 2
};

const VERSIONS = [
  {
    id: 'rv1', reportId: REPORT.id, versionNo: 1,
    cutoffAt: '2026-10-07T23:59:59+08:00',
    metricsSnapshot: { publicationCount: 1, deliveredCount: 1 },
    contentHash: 'sha256:0011223344',
    generatedAt: '2026-10-08T00:10:00+08:00'
  },
  {
    id: 'rv2', reportId: REPORT.id, versionNo: 2,
    cutoffAt: '2026-10-08T23:59:59+08:00',
    metricsSnapshot: { publicationCount: 2 },
    contentHash: 'sha256:ff99887766',
    generatedAt: '2026-10-09T00:10:00+08:00'
  }
];

const CLOSURES = [
  {
    id: 'c1',
    projectId: '00000000-0000-4000-8000-000000000001',
    closureType: 'COMPLETED',
    outstandingSnapshot: { pendingDeliveries: 0 },
    closedAt: '2026-10-08T00:00:00+08:00',
    reopenedAt: '2026-10-09T10:00:00+08:00',
    reopenReason: '客户追加需求'
  },
  {
    id: 'c2',
    projectId: '00000000-0000-4000-8000-000000000001',
    closureType: 'TERMINATED',
    outstandingSnapshot: { pendingDeliveries: 3, pendingPublications: 1 },
    closedAt: '2026-10-10T00:00:00+08:00',
    reopenedAt: null,
    reopenReason: null
  }
];

/* ---------------------------- 报告版本 ---------------------------- */

test('renderReportVersions: 版本按 versionNo 倒序，元信息来自 report + version', () => {
  const html = R.renderReportVersions(REPORT, VERSIONS);
  assert.ok(html.includes('报告 <span class="mono">rprprprp…1111</span> · 当前 v2 · 模板 v1'));
  assert.ok(html.includes('项目概览 · 已生成'));
  assert.ok(html.includes('2026-10-09 00:10'));
  assert.ok(html.includes('截止 2026-10-08 23:59'));
  assert.ok(html.includes('deliveredCount=1 · publicationCount=1'));
  assert.ok(html.includes('sha256:0011223344'));
  assert.ok(html.indexOf('v2') < html.indexOf('v1'));
  assert.equal(occurrences(html, 'class="rv"'), 2);
});

test('renderReportVersions: 无版本 / 无报告落到空态', () => {
  assert.ok(R.renderReportVersions(REPORT, []).includes('暂无报告版本'));
  assert.ok(R.renderReportVersions(null, []).includes('暂无报告版本'));
  assert.ok(!R.renderReportVersions(null, []).includes('当前 v'));
});

test('renderReportVersionsLoading / Error: 加载与错误态', () => {
  assert.ok(R.renderReportVersionsLoading().includes('正在读取报告版本…'));
  const err = R.renderReportVersionsError({ message: '<script>alert(1)</script>' });
  assert.ok(!err.includes('<script>'));
  assert.ok(err.includes('&lt;script&gt;'));
  assert.ok(err.includes('data-action="retry"'));
  assert.ok(R.renderReportVersionsError(null).includes('读取报告版本失败'));
});

/* ---------------------------- 结项事件 ---------------------------- */

test('buildClosureEvents: 一条记录展开结项 + 重开，按时间倒序', () => {
  const events = R.buildClosureEvents(CLOSURES);
  assert.equal(events.length, 3);
  assert.equal(events[0].title, '结项 · 提前终止');
  assert.ok(events[0].desc.includes('pendingDeliveries=3'));
  assert.equal(events[1].title, '项目重开');
  assert.ok(events[1].desc.includes('原因：客户追加需求'));
  assert.ok(events[1].desc.includes('原结项 2026-10-08 00:00 保留'));
  assert.equal(events[2].title, '结项 · 完整结项');
});

test('renderClosureTimeline: 时间线结构 / 事件文案', () => {
  const html = R.renderClosureTimeline(CLOSURES);
  assert.ok(html.includes('class="timeline"'));
  assert.equal(occurrences(html, 'class="tl-item"'), 3);
  assert.ok(html.includes('结项 · 完整结项'));
  assert.ok(html.includes('项目重开'));
  assert.ok(html.includes('pendingPublications=1'));
});

test('summarizeClosures: 结项总数与重开次数', () => {
  assert.deepEqual(R.summarizeClosures(CLOSURES), { total: 2, reopened: 1 });
  assert.deepEqual(R.summarizeClosures(null), { total: 0, reopened: 0 });
});

test('renderClosureTimeline: 空 / 加载 / 错误态', () => {
  assert.ok(R.renderClosureTimeline([]).includes('暂无结项 / 重开记录'));
  assert.ok(R.renderClosureLoading().includes('正在读取结项记录…'));
  const err = R.renderClosureError({ message: '<script>alert(1)</script>' });
  assert.ok(!err.includes('<script>'));
  assert.ok(err.includes('data-action="retry"'));
});

/* ---------------------------- 边界 ---------------------------- */

test('renderReportVersions: 未知 reportKind / 缺字段降级', () => {
  const html = R.renderReportVersions(
    { id: 'r1', reportKind: 'WEIRD', templateVersion: null, currentVersionNo: null },
    [{ versionNo: null, cutoffAt: null, generatedAt: null, contentHash: null, metricsSnapshot: {} }]
  );
  assert.ok(html.includes('WEIRD'));
  assert.ok(html.includes('v—'));
  assert.ok(html.includes('无固化指标'));
  assert.ok(html.includes('—'));
});

test('renderReportVersions: 超长 / XSS 的 metricsSnapshot 与 hash 被转义', () => {
  const long = 'A'.repeat(200);
  const html = R.renderReportVersions(REPORT, [{
    versionNo: 9,
    metricsSnapshot: { '<img src=x onerror=alert(1)>': '<script>alert(2)</script>', longValue: long },
    contentHash: '<script>alert(3)</script>',
    generatedAt: '2026-10-08T00:00:00+08:00',
    cutoffAt: '2026-10-07T23:59:59+08:00'
  }]);
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(html.includes('&lt;img'));
});

test('buildClosureEvents: 未知 closureType / XSS 的 reopenReason 被保留为文本', () => {
  const events = R.buildClosureEvents([{
    closureType: 'PAUSED',
    projectId: '<img src=x>',
    outstandingSnapshot: { '<script>': '<b>v</b>' },
    closedAt: '2026-10-08T00:00:00+08:00',
    reopenedAt: '2026-10-09T00:00:00+08:00',
    reopenReason: '<script>alert(1)</script>'
  }]);
  assert.equal(events.length, 2);
  assert.ok(events[1].title.includes('PAUSED'));
  assert.ok(events[0].desc.includes('<script>alert(1)</script>')); // 数据层保留原文，渲染时转义

  const html = R.renderClosureTimeline([{
    closureType: 'PAUSED', projectId: '<img src=x>',
    outstandingSnapshot: { '<script>': '<b>v</b>' },
    closedAt: '2026-10-08T00:00:00+08:00',
    reopenedAt: '2026-10-09T00:00:00+08:00',
    reopenReason: '<script>alert(1)</script>'
  }]);
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;script&gt;'));
});

test('buildClosureEvents / renderClosureTimeline: 空 / 非法输入不抛错', () => {
  assert.deepEqual(R.buildClosureEvents(null), []);
  assert.deepEqual(R.buildClosureEvents([null, 42]), []);
  assert.doesNotThrow(() => R.renderClosureTimeline(null));
});

test('源码不含 DOM / 浏览器全局读取（report.js + shared.js）', () => {
  ['report.js', 'shared.js'].forEach(function (file) {
    const src = fs.readFileSync(path.join(__dirname, file), 'utf8');
    assert.equal(/document\.|window\./.test(src), false, file + ' 命中了 DOM 全局');
  });
});

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) process.exitCode = 1;
