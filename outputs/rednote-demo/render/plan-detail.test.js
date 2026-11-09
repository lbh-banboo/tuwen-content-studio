/* render/plan-detail.test.js —— 逐行计划 / 实验分组（方案 A 纯函数）Node 单测
 * 运行：node render/plan-detail.test.js   （零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./plan-detail.js');

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
  id: '41474d88-daaf-55a1-a095-ce83516e8873',
  projectCode: 'PRJ-202609-6Q2ZK7',
  name: '屿见 Yujian 身体乳'
};

const EMPTY_DRAFT = {
  draftId: 'c7734d8d-0fef-41af-ab2d-a94f6d617777',
  projectId: PROJECT.id,
  baselineId: null,
  state: 'EDITING',
  totalQuantity: null,
  sceneItems: [],
  formItems: [],
  pairings: [],
  schemaVersion: 1,
  lockVersion: 1,
  etag: '"c7734d8d-0fef-41af-ab2d-a94f6d617777:1"',
  sceneAllocation: { total: 0, lines: [], allocatedTotal: 0, unallocated: 0, rounds: 1, notes: [] },
  formAllocation: { total: 0, lines: [], allocatedTotal: 0, unallocated: 0, rounds: 1, notes: [] }
};

const RICH_DRAFT = Object.assign({}, EMPTY_DRAFT, {
  totalQuantity: 10,
  sceneItems: [
    { key: 's1', name: '早晚日常', weightPermille: 600, capacity: null, state: 'ENABLED' },
    { key: 's2', name: '运动后', weightPermille: 400, capacity: 5, state: 'DISABLED', disabledReason: '无事实支持' }
  ],
  formItems: [
    { key: 'f1', name: '静态图', weightPermille: 500, capacity: null, state: 'ENABLED' },
    { key: 'f2', name: 'Live 图', weightPermille: 500, capacity: null, state: 'ENABLED' }
  ],
  pairings: [{ sceneKey: 's1', formKey: 'f1' }, { sceneKey: 's2', formKey: 'f2' }],
  sceneAllocation: {
    total: 10,
    lines: [
      { key: 's1', allocated: 6, weightPermille: 600, capacity: null, sortOrder: 1, floorPart: 6, remainder: 0, tieRank: 1, capacityShaved: false },
      { key: 's2', allocated: 4, weightPermille: 400, capacity: 5, sortOrder: 2, floorPart: 4, remainder: 0, tieRank: 2, capacityShaved: false }
    ],
    allocatedTotal: 10, unallocated: 0, rounds: 1, notes: ['按最大余数法分配']
  },
  formAllocation: {
    total: 10,
    lines: [
      { key: 'f1', allocated: 5, weightPermille: 500, capacity: null, sortOrder: 1, floorPart: 5, remainder: 0, tieRank: 1, capacityShaved: false },
      { key: 'f2', allocated: 5, weightPermille: 500, capacity: null, sortOrder: 2, floorPart: 5, remainder: 0, tieRank: 2, capacityShaved: false }
    ],
    allocatedTotal: 10, unallocated: 0, rounds: 1, notes: []
  }
});

/* ---------------------------- 汇总 ---------------------------- */

test('summarize: 空草稿 / 完整草稿', () => {
  assert.equal(R.summarize(EMPTY_DRAFT).updatedRows, 0);
  const s = R.summarize(RICH_DRAFT);
  assert.equal(s.scenes, 2);
  assert.equal(s.forms, 2);
  assert.equal(s.pairings, 2);
  assert.equal(s.totalQuantity, 10);
});

test('summarize: null 不抛错', () => {
  assert.doesNotThrow(() => R.summarize(null));
  assert.equal(R.summarize(null).updatedRows, 0);
});

/* ---------------------------- 页头 / 指标 ---------------------------- */

test('renderHead: 项目 / 行数 / 数据源说明', () => {
  const html = R.renderHead(PROJECT, RICH_DRAFT);
  assert.ok(html.includes('屿见 Yujian 身体乳'));
  assert.ok(html.includes('PRJ-202609-6Q2ZK7'));
  assert.ok(html.includes('步骤 4 / 8'));
  assert.ok(html.includes('行 4 · 配对 2'));
  assert.ok(html.includes('M4 WP38 未实现'));
});

test('renderMetrics: 4 张指标卡含「实验分组 无数据源」', () => {
  const html = R.renderMetrics(RICH_DRAFT);
  assert.equal(occurrences(html, 'class="card metric"'), 4);
  assert.ok(html.includes('草稿行'));
  assert.ok(html.includes('配对'));
  assert.ok(html.includes('无数据源'));
  assert.ok(html.includes('404'));
});

/* ---------------------------- 行 / 配对 ---------------------------- */

test('renderRowsCard: 空 → 空态 + 无独立数据源说明', () => {
  const html = R.renderRowsCard(EMPTY_DRAFT);
  assert.ok(html.includes('草稿行为空'));
  assert.ok(html.includes('逐行决策问题'));
});

test('renderRowsCard: 场景/形式行 / 权重 / 停用原因', () => {
  const html = R.renderRowsCard(RICH_DRAFT);
  assert.equal(occurrences(html, '<tr'), 5); // 表头 + 4 行
  assert.ok(html.includes('早晚日常'));
  assert.ok(html.includes('600‰'));
  assert.ok(html.includes('无事实支持'));
  assert.ok(html.includes('静态图') && html.includes('Live 图'));
});

test('renderPairingsCard: 空 / 正常配对', () => {
  assert.ok(R.renderPairingsCard(EMPTY_DRAFT).includes('暂无配对'));
  const html = R.renderPairingsCard(RICH_DRAFT);
  assert.equal(occurrences(html, '<tr'), 3); // 表头 + 2 对
  assert.ok(html.includes('s1') && html.includes('f1'));
  assert.ok(html.includes('s2') && html.includes('f2'));
});

/* ---------------------------- 分配 / 实验分组 ---------------------------- */

test('renderAllocationsCard: 两维表 / floor / 余数 / notes', () => {
  const html = R.renderAllocationsCard(RICH_DRAFT);
  assert.ok(html.includes('场景'));
  assert.ok(html.includes('形式'));
  assert.ok(html.includes('已分配 10'));
  assert.ok(html.includes('floor'));
  assert.ok(html.includes('按最大余数法分配'));
});

test('renderAllocationsCard: 空读数 → 空态', () => {
  const html = R.renderAllocationsCard(EMPTY_DRAFT);
  assert.ok(html.includes('分配行为空'));
});

test('renderExperimentCard: 如实降级为「无数据源」，给出 404 证据', () => {
  const html = R.renderExperimentCard();
  assert.ok(html.includes('无数据源'));
  assert.ok(html.includes('M4 WP38'));
  assert.ok(html.includes('experiment-groups'));
  assert.ok(html.includes('404 RESOURCE_NOT_FOUND'));
  assert.ok(html.includes('不编造'));
});

/* ---------------------------- 转义 / 错误 / 纯度 ---------------------------- */

test('行名称 / 停用原因里的 HTML 被转义', () => {
  const draft = Object.assign({}, EMPTY_DRAFT, {
    sceneItems: [{ key: '<s>', name: '<img src=x>', weightPermille: 0, state: 'DISABLED', disabledReason: '<b>x</b>' }]
  });
  const html = R.renderRowsCard(draft) + R.renderHead({ name: '<script>x</script>', projectCode: '"><b>x</b>' }, draft);
  assert.equal(html.includes('<img'), false);
  assert.equal(html.includes('<script>'), false);
  assert.equal(html.includes('<b>x</b>'), false);
});

test('null 输入不抛错', () => {
  assert.doesNotThrow(() => R.renderHead(null, null));
  assert.doesNotThrow(() => R.renderMetrics(null));
  assert.doesNotThrow(() => R.renderRowsCard(null));
  assert.doesNotThrow(() => R.renderPairingsCard(null));
  assert.doesNotThrow(() => R.renderAllocationsCard(null));
});

test('renderCardLoading / renderCardError 分支', () => {
  assert.ok(R.renderCardLoading('读逐行').includes('读逐行'));
  assert.ok(R.renderCardError({ code: 'DEPENDENCY_UNAVAILABLE', offline: true }).includes('127.0.0.1:8787'));
  assert.ok(R.renderCardError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderCardError({ message: '<b>x</b>' }).includes('&lt;b&gt;x&lt;/b&gt;'));
});

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'plan-detail.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) process.exitCode = 1;
