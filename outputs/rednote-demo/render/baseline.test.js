/* render/baseline.test.js —— 生产基线页（方案 A 纯函数）Node 单测
 * 运行：node render/baseline.test.js   （零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./baseline.js');

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

const PROJECT = {
  id: '5f2d4fec-1d6d-5fa5-9260-e65cf86e0fc8',
  projectCode: 'PRJ-202609-7K3M2Q',
  name: '素与 Su& 氨基酸洁面'
};

const EMPTY_DRAFT = {
  draftId: '59ce5df0-af07-4488-95c0-74c4306c1421',
  projectId: PROJECT.id,
  briefVersionId: null,
  claims: [],
  conflicts: [],
  scenes: [],
  boundaries: {},
  researchSelection: [],
  operatorConfirmed: false,
  lockVersion: 1,
  updatedAt: '2026-10-17T10:55:04.892085+08:00',
  etag: '"59ce5df0-af07-4488-95c0-74c4306c1421:1"',
  activeBaselineId: null,
  activeBaselineRevisionNo: null,
  readiness: {
    ready: false,
    conditions: [
      { key: 'blockingResolved', satisfied: true, reason: '阻断问题已全部处置。' },
      { key: 'hasProducibleScene', satisfied: false, reason: '没有任何场景可生产。' },
      { key: 'operatorConfirmed', satisfied: false, reason: '运营尚未明确点击确认。' }
    ]
  }
};

const READY_DRAFT = Object.assign({}, EMPTY_DRAFT, {
  briefVersionId: 'aaaaaaaa-1111-4111-8111-111111111111',
  activeBaselineId: 'bbbbbbbb-1111-4111-8111-111111111111',
  activeBaselineRevisionNo: 3,
  operatorConfirmed: true,
  claims: [{ claimId: 'c-1', state: 'CONFIRMED' }],
  conflicts: [{ conflictId: 'k-2', state: 'SETTLED' }],
  scenes: [{ sceneId: 's-1', state: 'PRODUCIBLE' }],
  readiness: {
    ready: true,
    conditions: [
      { key: 'blockingResolved', satisfied: true, reason: '阻断问题已全部处置。' },
      { key: 'hasProducibleScene', satisfied: true, reason: '1 个场景可生产。' },
      { key: 'operatorConfirmed', satisfied: true, reason: '运营已确认。' }
    ]
  }
});

const BASELINES = [
  {
    baselineId: 'bbbbbbbb-1111-4111-8111-111111111111',
    revisionNo: 3,
    briefVersionId: 'aaaaaaaa-1111-4111-8111-111111111111',
    rulesetVersion: '5',
    supersedesId: 'cccccccc-1111-4111-8111-111111111111',
    itemCount: 12,
    excludedCount: 3,
    isActive: true,
    createdAt: '2026-09-15T18:22:00+08:00'
  },
  {
    baselineId: 'cccccccc-1111-4111-8111-111111111111',
    revisionNo: 2,
    briefVersionId: null,
    rulesetVersion: '4',
    supersedesId: null,
    itemCount: 10,
    excludedCount: 1,
    isActive: false,
    createdAt: '2026-09-14T11:20:00+08:00'
  }
];

/* ---------------------------- 汇总 ---------------------------- */

test('summarize: 空基线 + 未 ready 草稿', () => {
  const s = R.summarize(EMPTY_DRAFT, []);
  assert.equal(s.baselineCount, 0);
  assert.equal(s.ready, false);
  assert.equal(s.satisfied, 1);
  assert.equal(s.briefVersionId, '');
});

test('summarize: 基线条数与生效修订', () => {
  const s = R.summarize(READY_DRAFT, BASELINES);
  assert.equal(s.baselineCount, 2);
  assert.equal(s.active, 1);
  assert.equal(s.activeRevisionNo, 3);
  assert.equal(s.ready, true);
});

test('summarize: null 不抛错', () => {
  assert.doesNotThrow(() => R.summarize(null, null));
  assert.equal(R.summarize(null, null).baselineCount, 0);
});

/* ---------------------------- 页头 / 指标 ---------------------------- */

test('renderHead: 项目 / 基线数量 / 生效修订', () => {
  const html = R.renderHead(PROJECT, READY_DRAFT, BASELINES);
  assert.ok(html.includes('素与 Su&amp; 氨基酸洁面'));
  assert.ok(html.includes('PRJ-202609-7K3M2Q'));
  assert.ok(html.includes('基线 2 个'));
  assert.ok(html.includes('生效修订 r3'));
  assert.ok(html.includes('步骤 3 / 8'));
});

test('renderMetrics: 基线 / 修订 / 冲突 / 条件', () => {
  const html = R.renderMetrics(READY_DRAFT, BASELINES);
  assert.equal(occurrences(html, 'class="card metric"'), 4);
  assert.ok(html.includes('基线数量'));
  assert.ok(html.includes('生效修订'));
  assert.ok(html.includes('完成条件'));
});

/* ---------------------------- 基线列表 ---------------------------- */

test('renderBaselinesCard: 空数组 → 空态与 POST 提示', () => {
  const html = R.renderBaselinesCard([]);
  assert.ok(html.includes('尚无生产基线'));
  assert.ok(html.includes('POST /baselines'));
});

test('renderBaselinesCard: 生效 / 历史 / 纳入排除计数', () => {
  const html = R.renderBaselinesCard(BASELINES);
  assert.equal(occurrences(html, '<tr'), 3); // 表头 + 2 行
  assert.ok(html.includes('r3') && html.includes('r2'));
  assert.ok(html.includes('生效中') && html.includes('历史'));
  assert.ok(html.includes('>12<') && html.includes('>3<'));
  assert.ok(html.includes('2026-09-15 18:22'));
  assert.ok(html.includes('tr-ok'));
});

/* ---------------------------- 完成条件 / 草稿 ---------------------------- */

test('renderReadinessCard: 条件与 ready 标记', () => {
  const html = R.renderReadinessCard(EMPTY_DRAFT);
  assert.ok(html.includes('阻断问题已经处理'));
  assert.ok(html.includes('至少有一个可生产场景'));
  assert.ok(html.includes('运营明确点击确认'));
  assert.ok(html.includes('not ready'));
  assert.ok(R.renderReadinessCard({ readiness: null }).includes('readiness 未提供'));
});

test('renderDraftSummaryCard: 规模与 ETag', () => {
  const html = R.renderDraftSummaryCard(READY_DRAFT);
  assert.ok(html.includes('aaaaaaaa…1111'));
  assert.ok(html.includes('事实（claims）'));
  assert.ok(html.includes('1 条'));
  assert.ok(html.includes('已确认'));
  assert.ok(html.includes('2026-10-17 10:55'));
  assert.ok(html.includes('ETag'));
});

/* ---------------------------- 前置条件 / 组装 ---------------------------- */

test('renderCreatePrereqCard: 缺 briefVersionId 与未 ready → 不可创建', () => {
  const html = R.renderCreatePrereqCard(EMPTY_DRAFT);
  assert.ok(html.includes('未满足'));
  assert.ok(html.includes('briefVersionId 为空'));
  assert.ok(html.includes('readiness.ready=false'));
  assert.ok(html.includes('POST /projects/{id}/baselines'));
});

test('renderCreatePrereqCard: 前置满足 → 可创建说明', () => {
  const html = R.renderCreatePrereqCard(READY_DRAFT);
  assert.ok(html.includes('已满足'));
  assert.ok(html.includes('创建基线'));
  assert.ok(html.includes('exclusionReason'));
});

test('renderAssemblyCard: 静态规则标注', () => {
  const html = R.renderAssemblyCard();
  assert.ok(html.includes('规则说明（静态）'));
  assert.ok(html.includes('跨品牌事实不得迁移'));
  assert.equal(occurrences(html, 'tl-item'), 5);
});

/* ---------------------------- 转义 / 错误 / 纯度 ---------------------------- */

test('基线 / 项目字段里的 HTML 被转义', () => {
  const html = R.renderHead(
    { name: '<img src=x>', projectCode: '"><b>x</b>' },
    READY_DRAFT,
    [{ baselineId: '<script>x</script>', revisionNo: 1, rulesetVersion: '<b>v</b>', isActive: false, createdAt: '2026-01-01T00:00:00+08:00' }]
  ) + R.renderBaselinesCard([{ baselineId: '<script>x</script>', revisionNo: 1, rulesetVersion: '<b>v</b>', isActive: false, createdAt: '2026-01-01T00:00:00+08:00' }]);
  assert.equal(html.includes('<script>'), false);
  assert.equal(html.includes('<img'), false);
  assert.equal(html.includes('<b>v</b>'), false);
});

test('null 输入不抛错', () => {
  assert.doesNotThrow(() => R.renderHead(null, null, null));
  assert.doesNotThrow(() => R.renderMetrics(null, null));
  assert.doesNotThrow(() => R.renderBaselinesCard(null));
  assert.doesNotThrow(() => R.renderDraftSummaryCard(null));
  assert.doesNotThrow(() => R.renderCreatePrereqCard(null));
});

test('renderCardLoading / renderCardError 分支', () => {
  assert.ok(R.renderCardLoading('读基线').includes('读基线'));
  assert.ok(R.renderCardError({ code: 'DEPENDENCY_UNAVAILABLE', offline: true }).includes('127.0.0.1:8787'));
  assert.ok(R.renderCardError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderCardError({ message: '<b>x</b>' }).includes('&lt;b&gt;x&lt;/b&gt;'));
});

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'baseline.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) process.exitCode = 1;
