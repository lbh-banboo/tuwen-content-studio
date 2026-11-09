/* render/project-overview.test.js —— 项目总览 / 八步关口（方案 A 纯函数）Node 单测
 * 运行：node render/project-overview.test.js   （零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./project-overview.js');

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

/* 形状与真后端 GET /projects/{id} 一致 */
const PROJECT = {
  id: '41474d88-daaf-55a1-a095-ce83516e8873',
  projectCode: 'PRJ-202609-6Q2ZK7',
  name: '屿见 Yujian 身体乳',
  phase: 'PRODUCTION',
  dueDate: '2026-09-15',
  timezone: 'Asia/Shanghai',
  targetDeliveryCount: 30,
  targetPublicationCount: 30,
  externalPolicy: 'LOCAL_ONLY',
  ownerId: '365bcf69-fc8b-5dc1-9144-00cc975daeb8',
  brandId: '64f93d4f-4a86-5302-b376-9d3182b0cf00',
  productId: '2ddbe922-43f7-55dc-a413-3178f1020e2c',
  description: '**已逾期**（截止 09-15），仍有 3 条待交付。',
  lockVersion: 1,
  permissions: { isAdmin: true, canView: true, canEdit: true }
};

/* 真后端当前返回空数组 */
const EMPTY_APPROVALS = [];

const APPROVALS = [
  {
    approvalId: 'a-old-rejected',
    projectId: PROJECT.id,
    stepNo: 1,
    subjectKind: 'BRIEF',
    subjectVersionId: 'v1',
    decision: 'REJECTED',
    decidedAt: '2026-09-10T10:00:00+08:00'
  },
  {
    approvalId: 'a-step1',
    projectId: PROJECT.id,
    stepNo: 1,
    subjectKind: 'BRIEF',
    subjectVersionId: 'v2',
    decision: 'APPROVED',
    decidedAt: '2026-09-11T11:05:00+08:00'
  },
  {
    approvalId: 'a-step3',
    projectId: PROJECT.id,
    stepNo: 3,
    subjectKind: 'BASELINE',
    subjectVersionId: 'b3',
    decision: 'APPROVED',
    decidedAt: '2026-09-15T18:22:00+08:00'
  },
  {
    approvalId: 'a-step5',
    projectId: PROJECT.id,
    stepNo: 5,
    subjectKind: 'TRIAL',
    subjectVersionId: 't1',
    decision: 'REJECTED',
    decidedAt: '2026-09-16T09:00:00+08:00'
  }
];

const BUDGET = {
  projectId: PROJECT.id,
  currency: 'CNY',
  limit: '0.00000000',
  committed: '0.00000000',
  reserved: '0.00000000',
  available: '0.00000000',
  configured: false
};

/* ---------------------------- gateStatuses ---------------------------- */

test('gateStatuses: 空数组 → 8 个关口全部 pending（未开始）', () => {
  const gates = R.gateStatuses(EMPTY_APPROVALS);
  assert.equal(gates.length, 8);
  assert.deepEqual(gates.map((g) => g.step), [1, 2, 3, 4, 5, 6, 7, 8]);
  assert.equal(gates.every((g) => g.state === 'pending'), true);
  assert.equal(gates.every((g) => g.decidedAt === ''), true);
});

test('gateStatuses: 同一关口取 decidedAt 最新一行', () => {
  const gates = R.gateStatuses(APPROVALS);
  assert.equal(gates[0].state, 'done'); // step 1 最新的 APPROVED 覆盖较早的 REJECTED
  assert.equal(gates[0].approvalId, 'a-step1');
  assert.equal(gates[2].state, 'done');
  assert.equal(gates[4].state, 'rejected');
  assert.equal(gates[1].state, 'pending');
  assert.equal(gates.filter((g) => g.state === 'done').length, 2);
});

test('gateStatuses: 未知 decision → unknown，不当作已批准', () => {
  const gates = R.gateStatuses([{ stepNo: 2, decision: 'DEFERRED', decidedAt: '2026-10-01T10:00:00+08:00' }]);
  assert.equal(gates[1].state, 'unknown');
  assert.equal(gates[1].decision, 'DEFERRED');
});

test('gateStatuses: 非数组 / 非法行 / 非法 stepNo 不抛错', () => {
  assert.doesNotThrow(() => R.gateStatuses(null));
  assert.equal(R.gateStatuses(null).length, 8);
  const gates = R.gateStatuses([null, 42, { stepNo: 'x' }, { stepNo: 1, decision: 'APPROVED' }]);
  assert.equal(gates[0].state, 'done');
  assert.equal(gates.filter((g) => g.state === 'pending').length, 7);
});

/* ---------------------------- 关口卡 ---------------------------- */

test('renderGateHub: 空数组正常显示「全部未开始」，不白屏', () => {
  const html = R.renderGateHub(EMPTY_APPROVALS);
  assert.ok(html.includes('八步关口'));
  assert.ok(html.includes('已批准 0 / 8'));
  assert.equal(occurrences(html, '未开始'), 9); // 8 行 + 空数组说明
  assert.equal(occurrences(html, 'list-item'), 8);
  assert.ok(html.includes('GET /step-approvals 当前返回空数组'));
  assert.ok(html.includes('不根据项目阶段推断批准状态'));
});

test('renderGateHub: 有批准记录时显示计数 / 驳回 / 时间 / 环节链接', () => {
  const html = R.renderGateHub(APPROVALS);
  assert.ok(html.includes('已批准 2 / 8'));
  assert.ok(html.includes('已驳回 1'));
  assert.ok(html.includes('2026-09-11 11:05'));
  assert.ok(html.includes('brief.html'));
  assert.ok(html.includes('delivery.v2.html'));
  assert.equal(html.includes('当前返回空数组'), false);
  assert.equal(occurrences(html, '已批准'), 3); // 卡头 + 步骤 1/3 状态
});

test('renderGateRow: 未知 decision 显示原始取值', () => {
  const gate = R.gateStatuses([{ stepNo: 2, decision: 'DEFERRED', decidedAt: '2026-10-01T10:00:00+08:00' }])[1];
  assert.ok(R.renderGateRow(gate).includes('状态未识别（DEFERRED）'));
});

/* ---------------------------- 页头 ---------------------------- */

test('renderProjectHead: 名称 / 编号 / 阶段 / 截止 / 目标 / 外部策略', () => {
  const html = R.renderProjectHead(PROJECT);
  assert.ok(html.includes('屿见 Yujian 身体乳'));
  assert.ok(html.includes('PRJ-202609-6Q2ZK7'));
  assert.ok(html.includes('投产期'));
  assert.ok(html.includes('2026-09-15'));
  assert.ok(html.includes('目标交付 30 条'));
  assert.ok(html.includes('目标发布 30 次'));
  assert.ok(html.includes('LOCAL_ONLY'));
});

test('renderProjectHead: null 显示「项目未选择」', () => {
  const html = R.renderProjectHead(null);
  assert.ok(html.includes('项目未选择'));
  assert.ok(html.includes('URL 中的 id 无效'));
});

test('renderProjectHead: 名称里的 HTML 被转义', () => {
  const html = R.renderProjectHead(Object.assign({}, PROJECT, { name: '<script>alert(1)</script>' }));
  assert.equal(html.includes('<script>'), false);
  assert.ok(html.includes('&lt;script&gt;'));
});

/* ---------------------------- 项目信息 ---------------------------- */

test('renderProjectInfoCard: 字段来自项目详情，大 ID 截断进 title', () => {
  const html = R.renderProjectInfoCard(PROJECT);
  assert.ok(html.includes('项目编号') && html.includes('PRJ-202609-6Q2ZK7'));
  assert.ok(html.includes('Asia/Shanghai'));
  assert.ok(html.includes('365bcf69…aeb8'));
  assert.ok(html.includes('title="365bcf69-fc8b-5dc1-9144-00cc975daeb8"'));
  assert.ok(html.includes('64f93d4f…cf00'));
  assert.ok(html.includes('2ddbe922…0e2c'));
  assert.ok(html.includes('项目说明'));
});

test('renderProjectInfoCard: 说明里的 HTML 被转义（保留原文）', () => {
  const html = R.renderProjectInfoCard(Object.assign({}, PROJECT, { description: '<b>x</b> 待交付' }));
  assert.equal(html.includes('<b>x</b>'), false);
  assert.ok(html.includes('&lt;b&gt;x&lt;/b&gt; 待交付'));
});

test('renderProjectInfoCard: 缺字段标「未提供」', () => {
  const html = R.renderProjectInfoCard({ id: 'x' });
  assert.ok(html.includes('未提供'));
  assert.equal(occurrences(html, '未提供') >= 5, true);
});

test('renderProjectInfoCard: null 不抛错', () => {
  assert.doesNotThrow(() => R.renderProjectInfoCard(null));
  assert.ok(R.renderProjectInfoCard(null).includes('未获取到项目详情'));
});

/* ---------------------------- 预算 ---------------------------- */

test('renderBudgetCard: configured=false 给出提醒，金额为接口原值', () => {
  const html = R.renderBudgetCard(BUDGET);
  assert.ok(html.includes('预算未配置'));
  assert.ok(html.includes('configured=false'));
  assert.ok(html.includes('0 不代表已批准额度'));
  assert.ok(html.includes('¥0.00'));
  assert.ok(html.includes('额度') && html.includes('已承诺') && html.includes('已冻结') && html.includes('可用'));
});

test('renderBudgetCard: configured=true 且额度>0 时显示占比条', () => {
  const html = R.renderBudgetCard({
    projectId: 'p', currency: 'CNY', limit: '15000', committed: '1288',
    reserved: '186', available: '13526', configured: true
  });
  assert.ok(html.includes('预算已配置'));
  assert.ok(html.includes('¥15,000.00'));
  assert.ok(html.includes('¥13,526.00'));
  assert.ok(html.includes('width:9.8%'));
});

test('renderBudgetCard: configured 缺失说明状态未知', () => {
  const html = R.renderBudgetCard({ projectId: 'p', currency: 'CNY', limit: '0', available: '0' });
  assert.ok(html.includes('未返回 configured 字段'));
});

test('renderBudgetCard: null 显示「未获取到预算信息」', () => {
  assert.ok(R.renderBudgetCard(null).includes('未获取到预算信息'));
});

test('renderBudgetCard: 金额字段缺失标「未提供」，不补成 0', () => {
  const html = R.renderBudgetCard({ projectId: 'p', configured: true });
  assert.ok(html.includes('未提供'));
  assert.equal(html.includes('¥0.00'), false);
});

/* ---------------------------- 加载 / 错误 ---------------------------- */

test('renderHeadLoading / renderCardLoading: 加载文案', () => {
  assert.ok(R.renderHeadLoading().includes('正在读取项目…'));
  assert.ok(R.renderCardLoading('读批准').includes('读批准'));
  assert.ok(R.renderCardLoading().includes('正在读取…'));
});

test('renderHeadError: 错误 message / code 被转义', () => {
  const html = R.renderHeadError({ code: 'RESOURCE_NOT_FOUND', message: '<img src=x>' });
  assert.ok(html.includes('RESOURCE_NOT_FOUND'));
  assert.equal(html.includes('<img'), false);
  assert.ok(html.includes('&lt;img'));
});

test('renderCardError: 离线 / 可重试 / 不可重试 / null 分支', () => {
  assert.ok(R.renderCardError({ code: 'DEPENDENCY_UNAVAILABLE', offline: true }).includes('127.0.0.1:8787'));
  assert.ok(R.renderCardError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderCardError({ code: 'RESOURCE_NOT_FOUND', retryable: false }).includes('请按错误码排查'));
  assert.ok(R.renderCardError(null).includes('读取失败'));
  assert.equal(occurrences(R.renderCardError(null), 'data-action="retry"'), 1);
});

/* ---------------------------- 纯度（机械证明） ---------------------------- */

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'project-overview.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

/* ---------------------------- 汇总 ---------------------------- */

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) {
  process.exitCode = 1;
}
