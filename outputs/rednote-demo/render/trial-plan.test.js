/* render/trial-plan.test.js —— 生产清单 / 费用 / 提交校验（方案 A 纯函数）Node 单测
 * 运行：node render/trial-plan.test.js   （零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./trial-plan.js');

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

const READY_DRAFT = Object.assign({}, EMPTY_DRAFT, {
  baselineId: '9e8c90a0-4169-545b-8417-8028c6c400ee',
  totalQuantity: 10,
  sceneItems: [
    { key: 's1', name: '早晚日常', weightPermille: 600, state: 'ENABLED' },
    { key: 's2', name: '运动后', weightPermille: 400, state: 'ENABLED' }
  ],
  formItems: [
    { key: 'f1', name: '静态图', weightPermille: 500, state: 'ENABLED' },
    { key: 'f2', name: 'Live', weightPermille: 500, state: 'ENABLED' }
  ],
  pairings: [{ sceneKey: 's1', formKey: 'f1' }],
  sceneAllocation: {
    total: 10,
    lines: [
      { key: 's1', allocated: 6, weightPermille: 600, capacity: null, sortOrder: 1, floorPart: 6, remainder: 0, tieRank: 1, capacityShaved: false },
      { key: 's2', allocated: 4, weightPermille: 400, capacity: null, sortOrder: 2, floorPart: 4, remainder: 0, tieRank: 2, capacityShaved: false }
    ],
    allocatedTotal: 10, unallocated: 0, rounds: 1, notes: []
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

const BUDGET = {
  projectId: PROJECT.id, currency: 'CNY', limit: '0.00000000',
  committed: '0.00000000', reserved: '0.00000000', available: '0.00000000', configured: false
};

const ESTIMATES = [{
  id: 'eeeeeeee-1111-4111-8111-111111111111',
  projectId: PROJECT.id,
  batchKind: 'TRIAL',
  quantity: 10,
  currency: 'CNY',
  state: 'DRAFT',
  totalAmount: '8.00000000',
  actualAmount: null,
  varianceAmount: null,
  pricingVersionId: 'cccccccc-1111-4111-8111-111111111111',
  lines: [{ unit: 'CALL', quantity: '10', unitPrice: '0.80', amount: '8.00000000' }]
}];

/* ---------------------------- 校验 ---------------------------- */

test('checks: 空草稿 → EDITING 通过，其余三项失败', () => {
  const list = R.checks(EMPTY_DRAFT);
  assert.equal(list.length, 4);
  assert.equal(list[0].ok, true);  // state EDITING
  assert.equal(list[1].ok, false); // totalQuantity null
  assert.equal(list[2].ok, false); // scenes empty
  assert.equal(list[3].ok, false); // forms empty
  assert.ok(list[1].detail.includes('totalQuantity=null'));
  assert.equal(R.canSubmit(EMPTY_DRAFT), false);
});

test('checks: 完整草稿 → 四项全部通过', () => {
  const list = R.checks(READY_DRAFT);
  assert.equal(list.every((c) => c.ok), true);
  assert.equal(R.canSubmit(READY_DRAFT), true);
  assert.ok(list[2].detail.includes('合计 1000‰'));
});

test('checks: 权重不足 1000‰ → 对应项失败', () => {
  const draft = JSON.parse(JSON.stringify(READY_DRAFT));
  draft.sceneItems[0].weightPermille = 500;
  draft.sceneItems[1].weightPermille = 400;
  const list = R.checks(draft);
  assert.equal(list[2].ok, false);
  assert.ok(list[2].detail.includes('合计 900‰'));
});

test('checks: null 不抛错', () => {
  assert.doesNotThrow(() => R.checks(null));
  assert.equal(R.canSubmit(null), false);
});

/* ---------------------------- 页头 / 指标 ---------------------------- */

test('renderHead: 项目 / 提交条件状态', () => {
  const html = R.renderHead(PROJECT, EMPTY_DRAFT);
  assert.ok(html.includes('屿见 Yujian 身体乳'));
  assert.ok(html.includes('提交条件未满足'));
  assert.ok(html.includes('步骤 4 / 8'));
  assert.ok(R.renderHead(PROJECT, READY_DRAFT).includes('提交条件已满足'));
});

test('renderMetrics: 4 张指标卡', () => {
  const html = R.renderMetrics(READY_DRAFT, ESTIMATES, BUDGET);
  assert.equal(occurrences(html, 'class="card metric"'), 4);
  assert.ok(html.includes('计划总量'));
  assert.ok(html.includes('场景分配'));
  assert.ok(html.includes('费用预估'));
  assert.ok(html.includes('预算未配置'));
});

/* ---------------------------- 生产清单 ---------------------------- */

test('renderPlanLinesCard: 空分配 → 空态', () => {
  const html = R.renderPlanLinesCard(EMPTY_DRAFT);
  assert.ok(html.includes('生产清单尚未生成'));
});

test('renderPlanLinesCard: 两维行 / 分配 / 权重 / 余数', () => {
  const html = R.renderPlanLinesCard(READY_DRAFT);
  assert.equal(occurrences(html, '<tr'), 5); // 表头 + 4 行
  assert.ok(html.includes('场景') && html.includes('形式'));
  assert.ok(html.includes('s1') && html.includes('f2'));
  assert.ok(html.includes('600‰'));
  assert.ok(html.includes('场景 total=10 / 未分配 0'));
  assert.ok(html.includes('形式 total=10 / 未分配 0'));
});

/* ---------------------------- 提交校验卡 ---------------------------- */

test('renderChecklistCard: 不可提交 / 可提交', () => {
  const no = R.renderChecklistCard(EMPTY_DRAFT);
  assert.ok(no.includes('不可提交'));
  assert.ok(no.includes('state=EDITING'));
  assert.ok(no.includes('totalQuantity=null'));
  assert.ok(no.includes('sceneItems 为空'));
  assert.ok(no.includes('POST /projects/{id}/plan-draft/submissions'));
  assert.ok(no.includes('If-Match'));

  const yes = R.renderChecklistCard(READY_DRAFT);
  assert.ok(yes.includes('可提交'));
  assert.equal(occurrences(yes, 'fa-check'), 4);
});

/* ---------------------------- 费用 / 预算 ---------------------------- */

test('renderEstimatesCard: 空 → 空态 + POST 提示', () => {
  const html = R.renderEstimatesCard([]);
  assert.ok(html.includes('暂无费用预估'));
  assert.ok(html.includes('POST /cost-estimates'));
  assert.ok(html.includes('不预占预算'));
});

test('renderEstimatesCard: 预估行 / 金额 / 批次 / 状态', () => {
  const html = R.renderEstimatesCard(ESTIMATES);
  assert.ok(html.includes('eeeeeeee…1111'));
  assert.ok(html.includes('试产 · 10 条'));
  assert.ok(html.includes('草稿'));
  assert.ok(html.includes('¥8.00'));
  assert.ok(html.includes('>1<')); // lines 数
  assert.ok(html.includes('—'));  // 实际 / 差异 null
});

test('renderBudgetCard: configured=false 提醒 + 四项金额', () => {
  const html = R.renderBudgetCard(BUDGET);
  assert.ok(html.includes('未配置'));
  assert.ok(html.includes('configured=false'));
  assert.ok(html.includes('¥0.00'));
  assert.ok(html.includes('已承诺') && html.includes('已冻结') && html.includes('可用'));
});

test('renderBudgetCard: configured=true / null', () => {
  const html = R.renderBudgetCard({
    currency: 'CNY', limit: '15000', committed: '1288', reserved: '186', available: '13526', configured: true
  });
  assert.ok(html.includes('已配置'));
  assert.ok(html.includes('¥15,000.00'));
  assert.ok(html.includes('¥13,526.00'));
  assert.ok(R.renderBudgetCard(null).includes('未获取到预算信息'));
});

/* ---------------------------- 转义 / 错误 / 纯度 ---------------------------- */

test('名称 / 预估 ID 里的 HTML 被转义', () => {
  const html = R.renderHead({ name: '<img src=x>', projectCode: '"><b>x</b>' }, EMPTY_DRAFT) +
    R.renderEstimatesCard([{
      id: '<script>x</script>', batchKind: '<b>k</b>', quantity: 1, state: 'DRAFT',
      currency: 'CNY', totalAmount: '1', lines: []
    }]);
  assert.equal(html.includes('<img'), false);
  assert.equal(html.includes('<script>'), false);
  assert.equal(html.includes('<b>k</b>'), false);
});

test('null 输入不抛错', () => {
  assert.doesNotThrow(() => R.renderHead(null, null));
  assert.doesNotThrow(() => R.renderMetrics(null, null, null));
  assert.doesNotThrow(() => R.renderPlanLinesCard(null));
  assert.doesNotThrow(() => R.renderChecklistCard(null));
  assert.doesNotThrow(() => R.renderEstimatesCard(null));
});

test('renderCardLoading / renderCardError 分支', () => {
  assert.ok(R.renderCardLoading('读清单').includes('读清单'));
  assert.ok(R.renderCardError({ code: 'DEPENDENCY_UNAVAILABLE', offline: true }).includes('127.0.0.1:8787'));
  assert.ok(R.renderCardError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderCardError({ message: '<b>x</b>' }).includes('&lt;b&gt;x&lt;/b&gt;'));
});

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'trial-plan.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) process.exitCode = 1;
