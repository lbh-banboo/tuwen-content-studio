/* render/trial-config.test.js —— 试产配置页（方案 A 纯函数）Node 单测
 * 运行：node render/trial-config.test.js   （零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./trial-config.js');

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

/* 与真后端 GET /plan-draft 一致（屿见当前为空草稿） */
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
  baselineId: '9e8c90a0-4169-545b-8417-8028c6c400ee',
  totalQuantity: 10,
  sceneItems: [
    { key: 's1', name: '早晚日常', weightPermille: 600, sortOrder: 1, capacity: null, state: 'ENABLED' },
    { key: 's2', name: '运动后清洁', weightPermille: 400, sortOrder: 2, capacity: 5, state: 'DISABLED', disabledReason: '无事实支持' }
  ],
  formItems: [
    { key: 'f1', name: '静态图', weightPermille: 500, sortOrder: 1, capacity: null, state: 'ENABLED' },
    { key: 'f2', name: 'Live 图', weightPermille: 500, sortOrder: 2, capacity: null, state: 'ENABLED' }
  ],
  pairings: [{ sceneKey: 's1', formKey: 'f1' }],
  sceneAllocation: {
    total: 10,
    lines: [
      { key: 's1', allocated: 6, weightPermille: 600, capacity: null, sortOrder: 1, floorPart: 6, remainder: 0, tieRank: 1, capacityShaved: false },
      { key: 's2', allocated: 0, weightPermille: 400, capacity: 5, sortOrder: 2, floorPart: 0, remainder: 0, tieRank: 2, capacityShaved: true }
    ],
    allocatedTotal: 6, unallocated: 4, rounds: 1, notes: ['s2 被容量截断']
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

const BASELINES = [
  {
    baselineId: '9e8c90a0-4169-545b-8417-8028c6c400ee',
    revisionNo: 1,
    briefVersionId: '65e4833c-463e-5a8c-ba6a-a437c89d2f8c',
    rulesetVersion: '审核规则 v5（9 阻断 · 14 提醒）',
    supersedesId: null,
    itemCount: 6,
    excludedCount: 1,
    isActive: true,
    createdAt: '2026-09-26T11:00:00+08:00'
  }
];

const ASSETS = [{
  assetId: 'aaaaaaaa-1111-4111-8111-111111111111',
  projectId: PROJECT.id,
  fileObjectId: 'bbbbbbbb-1111-4111-8111-111111111111',
  state: 'ACTIVE',
  productModel: 'YJ-250',
  productColor: '乳白',
  productPart: '瓶身',
  source: '品牌方提供',
  note: '白底图',
  supersedesAssetId: null,
  createdAt: '2026-09-22T10:00:00+08:00',
  createdBy: '365bcf69-fc8b-5dc1-9144-00cc975daeb8'
}];

const PRICING = [{
  id: 'cccccccc-1111-4111-8111-111111111111',
  providerId: 'dddddddd-1111-4111-8111-111111111111',
  modelProfileId: null,
  state: 'ACTIVE',
  currency: 'CNY',
  label: '2026-09 供应商价目',
  effectiveFrom: '2026-09-01T00:00:00+08:00',
  lines: [{ unit: 'CALL', unitPrice: '0.80' }]
}];

/* ---------------------------- 汇总 ---------------------------- */

test('summarize: 空草稿 / 空素材 / 空价目', () => {
  const s = R.summarize(EMPTY_DRAFT, [], [], []);
  assert.equal(s.scenes, 0);
  assert.equal(s.forms, 0);
  assert.equal(s.assets, 0);
  assert.equal(s.pricing, 0);
  assert.equal(s.baselineId, '');
});

test('summarize: 完整草稿计数', () => {
  const s = R.summarize(RICH_DRAFT, ASSETS, PRICING, BASELINES);
  assert.equal(s.scenes, 2);
  assert.equal(s.forms, 2);
  assert.equal(s.sceneDisabled, 1);
  assert.equal(s.pairings, 1);
  assert.equal(s.totalQuantity, 10);
  assert.equal(s.assets, 1);
  assert.equal(s.pricing, 1);
  assert.equal(s.activeBaseline, 1);
});

test('summarize: null 不抛错', () => {
  assert.doesNotThrow(() => R.summarize(null, null, null, null));
});

/* ---------------------------- 页头 / 指标 ---------------------------- */

test('renderHead: 项目 / 状态 / 总量', () => {
  const html = R.renderHead(PROJECT, EMPTY_DRAFT);
  assert.ok(html.includes('屿见 Yujian 身体乳'));
  assert.ok(html.includes('PRJ-202609-6Q2ZK7'));
  assert.ok(html.includes('步骤 4 / 8'));
  assert.ok(html.includes('草稿 EDITING'));
  assert.ok(html.includes('总量 未提供 条'));
});

test('renderHead: 有总量时展示数值', () => {
  assert.ok(R.renderHead(PROJECT, RICH_DRAFT).includes('总量 10 条'));
});

test('renderMetrics: 4 张指标卡', () => {
  const html = R.renderMetrics(RICH_DRAFT, ASSETS, PRICING, BASELINES);
  assert.equal(occurrences(html, 'class="card metric"'), 4);
  assert.ok(html.includes('场景条目'));
  assert.ok(html.includes('形式条目'));
  assert.ok(html.includes('参考素材'));
  assert.ok(html.includes('价目版本'));
});

/* ---------------------------- 比例卡 ---------------------------- */

test('renderDimensionCard: 空 → 空态与写入提示', () => {
  const html = R.renderDimensionCard('场景比例', 'sceneItems', [], null);
  assert.ok(html.includes('场景比例未配置'));
  assert.ok(html.includes('PUT /plan-draft'));
});

test('renderDimensionCard: 权重 / 状态 / 分配 / 余数 / 停用原因', () => {
  const html = R.renderDimensionCard('场景比例', 'sceneItems', RICH_DRAFT.sceneItems, RICH_DRAFT.sceneAllocation);
  assert.equal(occurrences(html, '<tr'), 3); // 表头 + 2 行
  assert.ok(html.includes('早晚日常'));
  assert.ok(html.includes('600‰') && html.includes('60.0%'));
  assert.ok(html.includes('启用'));
  assert.ok(html.includes('停用'));
  assert.ok(html.includes('停用原因：无事实支持'));
  assert.ok(html.includes('6 条'));
  assert.ok(html.includes('floor 6'));
  assert.ok(html.includes('s2 被容量截断'));
  assert.ok(html.includes('已分配 6') && html.includes('未分配 4'));
});

test('renderDimensionCard: allocation 缺失 → 分配列显示 —', () => {
  const html = R.renderDimensionCard('形式比例', 'formItems', RICH_DRAFT.formItems, null);
  assert.equal(occurrences(html, '<td class="num mono"><span class="t-mut">—</span></td>') >= 1, true);
  assert.ok(html.includes('分配读数未提供'));
});

/* ---------------------------- 基线 / 素材 / 价目 ---------------------------- */

test('renderBaselineCard: 未绑定 → 提示需指定 baselineId', () => {
  const html = R.renderBaselineCard(BASELINES, EMPTY_DRAFT);
  assert.ok(html.includes('null — 保存 plan-draft 需指定 baselineId'));
  assert.ok(html.includes('审核规则 v5（9 阻断 · 14 提醒）'));
  assert.ok(html.includes('生效基线：r1'));
});

test('renderBaselineCard: 已绑定 / 无基线', () => {
  const bound = R.renderBaselineCard(BASELINES, RICH_DRAFT);
  assert.ok(bound.includes('绑定基线'));
  assert.ok(bound.includes('9e8c90a0…00ee'));
  assert.ok(R.renderBaselineCard([], EMPTY_DRAFT).includes('尚无基线'));
});

test('renderAssetsCard: 空 → 空态 + 登记端点', () => {
  const html = R.renderAssetsCard([]);
  assert.ok(html.includes('暂无参考素材'));
  assert.ok(html.includes('POST /projects/{id}/reference-assets'));
});

test('renderAssetsCard: 素材行字段', () => {
  const html = R.renderAssetsCard(ASSETS);
  assert.ok(html.includes('aaaaaaaa…1111'));
  assert.ok(html.includes('YJ-250 · 乳白 · 瓶身'));
  assert.ok(html.includes('来源 品牌方提供'));
  assert.ok(html.includes('ACTIVE'));
  assert.ok(html.includes('2026-09-22 10:00'));
});

test('renderPricingCard: 空 → 空态；有数据 → 行字段', () => {
  assert.ok(R.renderPricingCard([]).includes('暂无价目版本'));
  const html = R.renderPricingCard(PRICING);
  assert.ok(html.includes('2026-09 供应商价目'));
  assert.ok(html.includes('dddddddd…1111'));
  assert.ok(html.includes('CNY'));
  assert.ok(html.includes('>1<'));
  assert.ok(html.includes('2026-09-01 00:00'));
});

/* ---------------------------- 转义 / 错误 / 纯度 ---------------------------- */

test('名称 / 原因 / 价目里的 HTML 被转义', () => {
  const draft = Object.assign({}, EMPTY_DRAFT, {
    sceneItems: [{ key: 's<x>', name: '<img src=x>', weightPermille: 0, state: 'DISABLED', disabledReason: '<b>x</b>' }]
  });
  const html = R.renderDimensionCard('场景', 'sceneItems', draft.sceneItems, null) +
    R.renderHead({ name: '<script>x</script>', projectCode: '"><b>x</b>' }, draft);
  assert.equal(html.includes('<img'), false);
  assert.equal(html.includes('<script>'), false);
  assert.equal(html.includes('<b>x</b>'), false);
});

test('null 输入不抛错', () => {
  assert.doesNotThrow(() => R.renderHead(null, null));
  assert.doesNotThrow(() => R.renderMetrics(null, null, null, null));
  assert.doesNotThrow(() => R.renderDimensionCard('x', 'x', null, null));
  assert.doesNotThrow(() => R.renderBaselineCard(null, null));
  assert.doesNotThrow(() => R.renderAssetsCard(null));
  assert.doesNotThrow(() => R.renderPricingCard(null));
});

test('renderCardLoading / renderCardError 分支', () => {
  assert.ok(R.renderCardLoading('读配置').includes('读配置'));
  assert.ok(R.renderCardError({ code: 'DEPENDENCY_UNAVAILABLE', offline: true }).includes('127.0.0.1:8787'));
  assert.ok(R.renderCardError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderCardError({ message: '<b>x</b>' }).includes('&lt;b&gt;x&lt;/b&gt;'));
});

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'trial-config.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) process.exitCode = 1;
