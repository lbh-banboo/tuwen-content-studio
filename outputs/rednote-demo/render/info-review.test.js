/* render/info-review.test.js —— 信息审核页（方案 A 纯函数）Node 单测
 * 运行：node render/info-review.test.js   （零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./info-review.js');

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

/* 与真后端 GET /information-draft 一致（种子草稿为空） */
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
      { key: 'blockingResolved', satisfied: true, reason: '阻断问题已全部处置。', blockingOpen: [], producibleScenes: [] },
      { key: 'hasProducibleScene', satisfied: false, reason: '没有任何场景可生产。', blockingOpen: [], producibleScenes: [] },
      { key: 'operatorConfirmed', satisfied: false, reason: '运营尚未明确点击确认。', blockingOpen: [], producibleScenes: [] }
    ]
  }
};

const FULL_DRAFT = Object.assign({}, EMPTY_DRAFT, {
  briefVersionId: 'aaaaaaaa-1111-4111-8111-111111111111',
  activeBaselineId: 'bbbbbbbb-1111-4111-8111-111111111111',
  activeBaselineRevisionNo: 3,
  operatorConfirmed: true,
  claims: [
    { claimId: 'c-1', statement: '氨基酸表活占比 62%', source: 'Brief v2 P.5', state: 'CONFIRMED' },
    { claimId: 'c-2', statement: '品牌官方页表述 ≥60%', source: { label: 'R-014 官方页' }, state: 'CONFLICT' },
    { claimId: 'c-3', statement: 'pH 5.5±0.5', source: 'Brief v2 P.5', state: 'PENDING' }
  ],
  conflicts: [
    { conflictId: 'k-1', kind: 'SOURCE', state: 'OPEN', blocking: true },
    { conflictId: 'k-2', kind: 'TIMELINESS', state: 'SETTLED', action: 'RANGE_STATEMENT' }
  ],
  scenes: [
    { sceneId: 's-1', state: 'PRODUCIBLE', factRefs: ['c-1'] },
    { sceneId: 's-2', state: 'NEEDS_LIMIT', factRefs: ['c-1', 'c-3'] },
    { sceneId: 's-3', state: 'DISABLED', factRefs: [] }
  ],
  boundaries: { tone: '克制', forbidden: ['医美级', '抗炎'] },
  researchSelection: [{ fileId: 'f-1', reason: '覆盖人群研究' }]
});

/* ---------------------------- 汇总 ---------------------------- */

test('summarize: 空草稿 → 0 审核项，1/3 条件满足', () => {
  const s = R.summarize(EMPTY_DRAFT);
  assert.equal(s.claims, 0);
  assert.equal(s.conflictsOpen, 0);
  assert.equal(s.producible, 0);
  assert.equal(s.ready, false);
  assert.equal(s.satisfied, 1);
});

test('summarize: 完整草稿正确计数', () => {
  const s = R.summarize(FULL_DRAFT);
  assert.equal(s.claims, 3);
  assert.equal(s.confirmed, 1);
  assert.equal(s.conflictClaims, 1);
  assert.equal(s.pending, 1);
  assert.equal(s.conflictsOpen, 1);
  assert.equal(s.blockingOpen, 1);
  assert.equal(s.producible, 1);
});

test('summarize: null / 非对象不抛错', () => {
  assert.doesNotThrow(() => R.summarize(null));
  assert.equal(R.summarize(null).claims, 0);
});

/* ---------------------------- 页头 / 指标 ---------------------------- */

test('renderHead: 项目 / 待处置冲突 / 完成条件状态', () => {
  const html = R.renderHead(PROJECT, EMPTY_DRAFT);
  assert.ok(html.includes('素与 Su&amp; 氨基酸洁面'));
  assert.ok(html.includes('PRJ-202609-7K3M2Q'));
  assert.ok(html.includes('步骤 3 / 8'));
  assert.ok(html.includes('完成条件未满足'));
});

test('renderMetrics: 4 张指标卡计数', () => {
  const html = R.renderMetrics(FULL_DRAFT);
  assert.equal(occurrences(html, 'class="card metric"'), 4);
  assert.ok(html.includes('阻断冲突'));
  assert.ok(html.includes('可生产场景'));
  assert.ok(html.includes('完成条件'));
});

/* ---------------------------- 完成条件 ---------------------------- */

test('renderReadinessCard: 三条条件逐条渲染，blockingOpen 展示', () => {
  const draft = JSON.parse(JSON.stringify(EMPTY_DRAFT));
  draft.readiness.conditions[0].satisfied = false;
  draft.readiness.conditions[0].blockingOpen = ['k-1'];
  const html = R.renderReadinessCard(draft);
  assert.ok(html.includes('阻断问题已经处理'));
  assert.ok(html.includes('至少有一个可生产场景'));
  assert.ok(html.includes('运营明确点击确认'));
  assert.ok(html.includes('阻断项：'));
  assert.ok(html.includes('k-1'));
  assert.ok(html.includes('not ready'));
});

test('renderReadinessCard: readiness 缺失显示空态', () => {
  assert.ok(R.renderReadinessCard({ readiness: null }).includes('readiness 未提供'));
});

/* ---------------------------- 事实 / 冲突 / 场景 ---------------------------- */

test('renderClaimsCard: 陈述 / 来源 / 状态 + 空态', () => {
  const html = R.renderClaimsCard(FULL_DRAFT);
  assert.equal(occurrences(html, '<tr'), 4); // 表头 + 3 行
  assert.ok(html.includes('氨基酸表活占比 62%'));
  assert.ok(html.includes('Brief v2 P.5'));
  assert.ok(html.includes('R-014 官方页'));
  assert.ok(html.includes('已确认') && html.includes('冲突') && html.includes('待确认'));
  assert.ok(html.includes('tr-danger'));
  assert.ok(R.renderClaimsCard(EMPTY_DRAFT).includes('暂无事实清单'));
});

test('renderConflictsCard: 类型中文 / 状态 / 阻断 / 处置动作', () => {
  const html = R.renderConflictsCard(FULL_DRAFT);
  assert.ok(html.includes('来源冲突') && html.includes('时效冲突'));
  assert.ok(html.includes('待处置') && html.includes('已处置'));
  assert.ok(html.includes('阻断'));
  assert.ok(html.includes('区间表述'));
  assert.ok(R.renderConflictsCard(EMPTY_DRAFT).includes('暂无事实冲突'));
});

test('renderScenesCard: 状态与 factRefs', () => {
  const html = R.renderScenesCard(FULL_DRAFT);
  assert.ok(html.includes('可生产') && html.includes('需标注限制') && html.includes('已停用'));
  assert.ok(html.includes('c-1'));
  assert.ok(html.includes('s-2'));
  assert.ok(R.renderScenesCard(EMPTY_DRAFT).includes('暂无场景'));
});

/* ---------------------------- 边界 / 选用 / 元信息 ---------------------------- */

test('renderBoundariesCard: 键值与数组', () => {
  const html = R.renderBoundariesCard(FULL_DRAFT);
  assert.ok(html.includes('tone'));
  assert.ok(html.includes('克制'));
  assert.ok(html.includes('医美级') && html.includes('抗炎'));
  assert.ok(R.renderBoundariesCard(EMPTY_DRAFT).includes('暂无表达边界'));
});

test('renderResearchSelectionCard: 空数组与对象条目', () => {
  assert.ok(R.renderResearchSelectionCard(EMPTY_DRAFT).includes('暂无研究资料选用'));
  const html = R.renderResearchSelectionCard(FULL_DRAFT);
  assert.ok(html.includes('f-1'));
  assert.ok(html.includes('覆盖人群研究'));
});

test('renderMetaCard: 草稿元信息含 ETag 与活跃基线', () => {
  const html = R.renderMetaCard(FULL_DRAFT);
  assert.ok(html.includes('59ce5df0…1421'));
  assert.ok(html.includes('aaaaaaaa…1111'));
  assert.ok(html.includes('bbbbbbbb…1111'));
  assert.ok(html.includes('基线修订号'));
  assert.ok(html.includes('If-Match'));
  assert.ok(html.includes('2026-10-17 10:55'));
});

/* ---------------------------- 转义 / 错误 / 纯度 ---------------------------- */

test('claim / conflict / boundaries 里的 HTML 被转义', () => {
  const draft = JSON.parse(JSON.stringify(EMPTY_DRAFT));
  draft.claims = [{ claimId: 'c-x', statement: '<script>alert(1)</script>', source: '<img src=x>', state: 'PENDING' }];
  draft.boundaries = { note: '<b>bad</b>' };
  const html = R.renderClaimsCard(draft) + R.renderBoundariesCard(draft);
  assert.equal(html.includes('<script>'), false);
  assert.equal(html.includes('<img'), false);
  assert.equal(html.includes('<b>bad</b>'), false);
});

test('null 输入不抛错', () => {
  assert.doesNotThrow(() => R.renderHead(null, null));
  assert.doesNotThrow(() => R.renderMetrics(null));
  assert.doesNotThrow(() => R.renderReadinessCard(null));
  assert.doesNotThrow(() => R.renderClaimsCard(null));
});

test('renderCardLoading / renderCardError 分支', () => {
  assert.ok(R.renderCardLoading('读审核').includes('读审核'));
  assert.ok(R.renderCardError({ code: 'DEPENDENCY_UNAVAILABLE', offline: true }).includes('127.0.0.1:8787'));
  assert.ok(R.renderCardError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderCardError({ message: '<b>x</b>' }).includes('&lt;b&gt;x&lt;/b&gt;'));
});

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'info-review.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) process.exitCode = 1;
