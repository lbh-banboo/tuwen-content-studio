/* render/cost.test.js —— cost 页渲染层（方案 A 纯函数）Node 单测
 * 运行：node render/cost.test.js   （零依赖，退出码 0=全绿 1=有失败）
 *
 * fixture 形状逐条对齐后端 pydantic 模型（**不以 contracts/openapi.json 为准**，
 * 因为 tools/contracts/export_openapi.py:59 _strip_titles() 会删掉名为 title 的
 * 响应字段，快照与运行时不一致）：
 *   BudgetBody              billing/schema.py:295-313
 *   LedgerSummaryBody       billing/schema.py:536-558
 *   LedgerEntryResponseBody billing/schema.py:347-359
 *   CostEstimateBody        billing/schema.py:381-396 + EstimateLineBody:366-374
 *   ReservationBody         billing/schema.py:327-340
 *   PricingVersionBody      billing/schema.py:403-418
 *   ReconciliationReportBody billing/schema.py:472-499 + DriftBody:450 + PendingUsageBody:425
 * 金额一律是**字符串**（money.to_wire 补零到 8 位小数）—— fixture 照此写。
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./cost.js');

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

/* ---------------------------- fixtures ---------------------------- */

const BUDGET = {
  projectId: '00000000-0000-4000-8000-000000000001',
  currency: 'CNY',
  limit: '4000.00000000',
  committed: '3000.00000000',
  reserved: '925.02000000',
  available: '74.98000000',
  configured: true
};

const BUDGET_UNCONFIGURED = {
  projectId: '00000000-0000-4000-8000-000000000001',
  currency: 'CNY',
  limit: '0.00000000',
  committed: '0.00000000',
  reserved: '0.00000000',
  available: '0.00000000',
  configured: false
};

const SUMMARY = {
  projectId: '00000000-0000-4000-8000-000000000001',
  byKind: {
    USAGE: '3200.50000000',
    BILL: '100.00000000',
    REVERSAL: '-300.50000000',
    ADJUSTMENT: '12.00000000'
  },
  net: '3012.00000000',
  entryCount: 42,
  truncated: false
};

const SUMMARY_TRUNCATED = Object.assign({}, SUMMARY, { truncated: true, entryCount: 100 });

const ENTRIES = [
  {
    id: 'e1111111-1111-4111-8111-111111111111',
    projectId: '00000000-0000-4000-8000-000000000001',
    entryKind: 'USAGE',
    currency: 'CNY',
    amount: '12.58000000',
    quantity: '12000',
    unit: 'TOKEN',
    description: '标题候选生成 · 第 1 次尝试'
  },
  {
    id: 'e2222222-2222-4222-8222-222222222222',
    projectId: '00000000-0000-4000-8000-000000000001',
    entryKind: 'USAGE',
    currency: 'CNY',
    amount: '8.40000000',
    quantity: '6',
    unit: 'IMAGE',
    description: null
  },
  {
    id: 'e3333333-3333-4333-8333-333333333333',
    projectId: '00000000-0000-4000-8000-000000000001',
    entryKind: 'REVERSAL',
    currency: 'CNY',
    amount: '-8.40000000',
    quantity: null,
    unit: null,
    description: '生成失败全额冲正'
  }
];

const ESTIMATES = [
  {
    id: 'c1111111-1111-4111-8111-111111111111',
    projectId: '00000000-0000-4000-8000-000000000001',
    batchKind: 'CONTENT_GENERATION',
    quantity: 10,
    currency: 'CNY',
    state: 'SETTLED',
    totalAmount: '120.00000000',
    actualAmount: '131.40000000',
    varianceAmount: '11.40000000',
    pricingVersionId: 'pv111111-1111-4111-8111-111111111111',
    lines: [{ unit: 'TOKEN', quantity: '120000', unitPrice: '0.00100000', amount: '120.00000000' }]
  },
  {
    id: 'c2222222-2222-4222-8222-222222222222',
    projectId: '00000000-0000-4000-8000-000000000001',
    batchKind: 'MEDIA_GENERATION',
    quantity: 6,
    currency: 'CNY',
    state: 'RESERVED',
    totalAmount: '48.00000000',
    actualAmount: null,
    varianceAmount: null,
    pricingVersionId: null,
    lines: []
  }
];

const RESERVATIONS = [
  {
    id: 'r1111111-1111-4111-8111-111111111111',
    projectId: '00000000-0000-4000-8000-000000000001',
    state: 'HELD',
    currency: 'CNY',
    amount: '48.00000000',
    settledAmount: null,
    ref: 'batch:media-01',
    note: null,
    estimateId: 'c2222222-2222-4222-8222-222222222222'
  },
  {
    id: 'r2222222-2222-4222-8222-222222222222',
    projectId: '00000000-0000-4000-8000-000000000001',
    state: 'EXPIRED',
    currency: 'CNY',
    amount: '10.00000000',
    settledAmount: '0.00000000',
    ref: null,
    note: '租约到期自动过期',
    estimateId: null
  }
];

const PRICING = [
  {
    id: 'pv111111-1111-4111-8111-111111111111',
    providerId: 'pr111111-1111-4111-8111-111111111111',
    modelProfileId: 'mp111111-1111-4111-8111-111111111111',
    state: 'PUBLISHED',
    currency: 'CNY',
    label: 'live-pack 文本 v1.4',
    effectiveFrom: '2026-09-01T00:00:00+08:00',
    lines: [{ unit: 'TOKEN', unitPrice: '0.00100000' }, { unit: 'CALL', unitPrice: '0.02000000' }]
  }
];

const RECONCILIATION = {
  scanned: 18,
  resolved: 16,
  pending: [
    {
      attemptId: 'at111111-1111-4111-8111-111111111111',
      jobId: 'jb111111-1111-4111-8111-111111111111',
      projectId: '00000000-0000-4000-8000-000000000001',
      attemptNo: 2,
      state: 'USAGE_PENDING',
      available: false,
      estimatedAmount: '74.80000000',
      ledgerEntryId: null,
      note: '供应商侧用量回执未到'
    }
  ],
  pendingCount: 2,
  drifts: [
    {
      projectId: '00000000-0000-4000-8000-000000000001',
      table: 'project_budgets',
      accountValue: '3000.00000000',
      recomputedValue: '3012.00000000',
      drift: '-12.00000000',
      healthy: false
    }
  ],
  projectId: '00000000-0000-4000-8000-000000000001',
  truncated: false,
  projectsChecked: 1,
  healthy: false
};

/* ============================ 正常 ============================ */

test('money: CNY/USD 符号 + 千分位 + 两位小数；不用 Intl', () => {
  assert.equal(R.money('3925.02000000', 'CNY'), '¥3,925.02');
  assert.equal(R.money('1234567.5', 'USD'), '$1,234,567.50');
  assert.equal(R.money('-300.50000000', 'CNY'), '-¥300.50');
  assert.equal(R.money('12.00000000', 'JPY'), 'JPY 12.00'); // 未知币种原样前缀
});

test('usageRatio: configured=true 时真算 (committed+reserved)/limit', () => {
  const r = R.usageRatio(BUDGET);
  assert.equal(r.text, '98.1%');
  assert.ok(Math.abs(r.pct - 98.1255) < 0.001);
  assert.equal(r.used, 3925.02);
});

test('sumPositive / sumReversal: 按 billing/types.py 的正负种类分组', () => {
  assert.equal(R.sumPositive(SUMMARY.byKind), 3300.5);  // USAGE + BILL
  assert.equal(R.sumReversal(SUMMARY.byKind), -300.5);  // REVERSAL
  assert.equal(R.sumPositive(null), 0);
  assert.equal(R.sumReversal(undefined), 0);
});

test('renderBudgetCard: 四个金额与使用率都来自数据，原始串进 title', () => {
  const html = R.renderBudgetCard(BUDGET);
  assert.ok(html.includes('98.1%'));
  assert.ok(html.includes('¥4,000.00'));
  assert.ok(html.includes('¥3,000.00'));
  assert.ok(html.includes('¥925.02'));
  assert.ok(html.includes('¥74.98'));
  assert.ok(html.includes('title="925.02000000"')); // 原始串权威值
  assert.ok(html.includes('bar-fill danger'));      // ≥95% 用 danger
});

test('renderSummaryCards: 3 真 2 降级，降级卡显示「无数据源」而不是 0', () => {
  const html = R.renderSummaryCards(SUMMARY, BUDGET);
  assert.ok(html.includes('¥3,300.50'));   // 实际费用 = USAGE + BILL
  assert.ok(html.includes('-¥300.50'));    // 冲正
  assert.ok(html.includes('¥925.02'));     // 进行中占用 ← budget.reserved
  assert.ok(html.includes('待核对'));
  assert.ok(html.includes('单篇通过成本'));
  assert.equal((html.match(/无数据源/g) || []).length, 2);
  assert.ok(!/>0<|¥0\.00/.test(html.split('待核对')[1].split('单篇通过成本')[0]));
});

test('renderKindDistribution: 按 LEDGER_KINDS 真实分布 + 条宽由数据归一', () => {
  const html = R.renderKindDistribution(SUMMARY);
  assert.ok(html.includes('用量（USAGE）'));
  assert.ok(html.includes('冲正（REVERSAL）'));
  assert.ok(html.includes('width:100.0%'));  // 最大值 USAGE 3200.5 → 100%
  assert.ok(html.includes('替代原型的「研究 / 生产 / 报告」分组'));
});

test('renderUnitDistribution: 标题改名、按 entryKind × unit 聚合', () => {
  const html = R.renderUnitDistribution(ENTRIES);
  assert.ok(html.includes('分录类型 × 计量单位分布'));
  assert.ok(!html.includes('任务类型与用量分布</span>')); // 不得沿用原标题
  assert.ok(html.includes('TOKEN（词元）'));
  assert.ok(html.includes('IMAGE（张）'));
  assert.ok(html.includes('（未标注单位）'));
});

test('renderLedgerTable: 6 字段真实渲染 + 明说没有时间列', () => {
  const html = R.renderLedgerTable(ENTRIES);
  assert.ok(html.includes('¥12.58'));
  assert.ok(html.includes('标题候选生成 · 第 1 次尝试'));
  assert.ok(html.includes('没有 createdAt'));
  assert.ok(html.includes('不可游标翻页'));
  assert.ok(html.includes('class="tr-warn"')); // 负数分录标色
  assert.ok(!html.includes('<th>时间'));
});

test('renderEstimates: 预估/实际/差异三列真实；未结算显示「未结算」而非 0', () => {
  const html = R.renderEstimates(ESTIMATES);
  assert.ok(html.includes('¥120.00'));
  assert.ok(html.includes('¥131.40'));
  assert.ok(html.includes('¥11.40'));
  assert.ok(html.includes('未结算'));
  assert.ok(html.includes('已结算') && html.includes('已预留'));
});

test('renderReservations: 四状态标签 + EXPIRED 行标红', () => {
  const html = R.renderReservations(RESERVATIONS);
  assert.ok(html.includes('占用中'));
  assert.ok(html.includes('已过期'));
  assert.ok(html.includes('<tr class="tr-danger">'));
  assert.ok(html.includes('batch:media-01'));
});

test('renderPricingVersions: label/供应商短 id/单位；明说无供应商名称', () => {
  const html = R.renderPricingVersions(PRICING);
  assert.ok(html.includes('live-pack 文本 v1.4'));
  assert.ok(html.includes('TOKEN / CALL'));
  assert.ok(html.includes('已发布'));
  assert.ok(html.includes('provider_id'));     // 蛇形参数名的事实
  assert.ok(html.includes('没有供应商名称'));
});

test('renderReconciliation: 对账报告渲染 pendingCount / drifts / healthy', () => {
  const html = R.renderReconciliation(RECONCILIATION);
  assert.ok(html.includes('账面存在分叉'));
  assert.ok(html.includes('project_budgets'));
  assert.ok(html.includes('-12.00000000'));
  assert.ok(html.includes('USAGE_PENDING'));
  assert.ok(html.includes('供应商侧用量回执未到'));
});

test('reconcileRequest: 请求描述符可单测（method/path/query）', () => {
  const req = R.reconcileRequest('00000000-0000-4000-8000-000000000001');
  assert.deepEqual(req, {
    method: 'POST',
    path: '/billing/reconciliations',
    query: { projectId: '00000000-0000-4000-8000-000000000001', includeDrifts: true },
    body: {}
  });
});

/* ============================ 降级（本批新增门槛） ============================ */

test('renderBudgetModeNotice: 模式字段不存在 —— 不得二选一显示硬限制/提醒', () => {
  const html = R.renderBudgetModeNotice();
  assert.ok(html.includes('无字段'));
  assert.ok(html.includes('无数据源'));
  assert.ok(html.includes('mode / HARD / SOFT / REMIND'));
  /* 关键：不能出现「当前为硬限制模式」这类选定其一的断言 */
  assert.ok(!/当前[^。]*硬限制模式/.test(html));
});

test('renderAttributionUnavailable: 三块归属维度合并降级，明写缺哪些列', () => {
  const html = R.renderAttributionUnavailable();
  assert.ok(html.includes('无数据源'));
  assert.ok(html.includes('batchId'));
  assert.ok(html.includes('actorId'));
  assert.ok(html.includes('providerId'));
  assert.ok(html.includes('解析它来反推归属属于编造'));
  assert.ok(!html.includes('<table')); // 不渲染空表格
});

test('renderQueueUnavailable: 队列降级卡写明无 HTTP 端点，且不渲染空表格', () => {
  const html = R.renderQueueUnavailable();
  assert.ok(html.includes('modules/jobs/models.py:185'));
  assert.ok(html.includes('零命中'));
  assert.ok(html.includes('空表格会被误读'));
  assert.ok(!html.includes('<table'));
});

test('renderCostDoctrine: 教义卡带「标准文本 · 非系统数据」标记', () => {
  const html = R.renderCostDoctrine();
  assert.ok(html.includes('标准文本 · 非系统数据'));
  assert.ok(html.includes('失败重试'));
});

/* ============================ 空态 ============================ */

test('renderBudgetCard: null / undefined 落空态，不抛错', () => {
  assert.ok(R.renderBudgetCard(null).includes('未获取到预算数据'));
  assert.ok(R.renderBudgetCard(undefined).includes('未获取到预算数据'));
  assert.doesNotThrow(() => R.renderBudgetCard('oops'));
});

test('各表格：空数组 / null / 非数组都落空态且不抛错', () => {
  [R.renderLedgerTable, R.renderEstimates, R.renderReservations, R.renderPricingVersions]
    .forEach((fn) => {
      [[], null, undefined, 'oops', [null, 42]].forEach((input) => {
        const html = fn(input);
        assert.ok(html.includes('暂无'), fn.name + ' 空态缺失');
      });
    });
});

test('renderKindDistribution: byKind 为空对象 → 空态且说明「真的没有分录」', () => {
  const html = R.renderKindDistribution({ byKind: {}, net: '0', entryCount: 0, truncated: false });
  assert.ok(html.includes('账本里暂无分录'));
  assert.ok(html.includes('不是缺字段'));
});

test('renderSummaryCards: summary 缺失时实际费用显示「无数据源」而不是 0', () => {
  const html = R.renderSummaryCards(null, null);
  assert.ok(html.includes('无数据源'));
  assert.ok(!html.includes('¥0.00'));
});

test('renderReconciliation: 未对账时提示「GET 页加载时不发写请求」', () => {
  const html = R.renderReconciliation(null);
  assert.ok(html.includes('尚未对账'));
  assert.ok(html.includes('不发写请求'));
});

test('renderEmpty: 空态卡可自定义文案', () => {
  assert.ok(R.renderEmpty('当前项目没有账本数据。').includes('当前项目没有账本数据。'));
  assert.ok(R.renderEmpty().includes('当前项目没有费用数据。'));
});

/* ============================ 错误 ============================ */

test('renderCardError: 离线错误展示 code / message / 重试按钮', () => {
  const html = R.renderCardError({
    code: 'DEPENDENCY_UNAVAILABLE',
    message: '连不上后端（network_error），且本页未登记静态样例。',
    offline: true
  });
  assert.ok(html.includes('DEPENDENCY_UNAVAILABLE'));
  assert.ok(html.includes('连不上后端'));
  assert.ok(html.includes('data-action="retry"'));
});

test('renderCardError: 可重试 / 不可重试 / 裸串 / null 四个分支', () => {
  assert.ok(R.renderCardError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderCardError({ code: 'RESOURCE_NOT_FOUND', retryable: false }).includes('请按错误码排查'));
  assert.ok(R.renderCardError('裸字符串错误').includes('裸字符串错误'));
  assert.ok(R.renderCardError(null).includes('读取费用数据失败'));
});

test('renderCardError: 错误信息里的 HTML 被转义（防注入）', () => {
  const html = R.renderCardError({ message: '<script>alert(1)</script>' });
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});

/* ============================ 边界 ============================ */

test('边界 · configured=false：使用率留空、明说 0 不代表已批准额度', () => {
  const r = R.usageRatio(BUDGET_UNCONFIGURED);
  assert.equal(r.pct, null);
  const html = R.renderBudgetCard(BUDGET_UNCONFIGURED);
  assert.ok(html.includes('未配置预算（configured=false）'));
  assert.ok(html.includes('0 不代表已批准额度'));
  assert.ok(!html.includes('0.0%'));
  assert.ok(!html.includes('bar-fill ok'));
});

test('边界 · limit=0 且 configured=true：使用率不可计算而不是除零变 0', () => {
  const r = R.usageRatio(Object.assign({}, BUDGET, { limit: '0.00000000' }));
  assert.equal(r.pct, null);
  assert.ok(r.text.includes('不可计算'));
});

test('边界 · truncated=true：必须显式渲染「net 非全量净额」', () => {
  assert.equal(R.renderTruncationWarning(SUMMARY), ''); // false 时不出现
  const warn = R.renderTruncationWarning(SUMMARY_TRUNCATED);
  assert.ok(warn.includes('net 不是全量净额'));
  assert.ok(R.renderSummaryCards(SUMMARY_TRUNCATED, BUDGET).includes('truncated=true'));
  assert.ok(R.renderKindDistribution(SUMMARY_TRUNCATED).includes('net 不是全量净额'));
});

test('边界 · 未知 entryKind 降级为中性 tag，不映射成已有类型', () => {
  const html = R.renderLedgerTable([{
    id: 'x', entryKind: 'FROZEN', currency: 'CNY', amount: '1.00000000'
  }]);
  assert.ok(html.includes('FROZEN'));
  assert.ok(!html.includes('用量（USAGE）'));
});

test('边界 · byKind 里出现受控取值域外的键：原样列出、不归并', () => {
  const html = R.renderKindDistribution({
    byKind: { USAGE: '10.00000000', MYSTERY: '5.00000000' }, net: '15', entryCount: 2, truncated: false
  });
  assert.ok(html.includes('MYSTERY（未知类型）'));
});

test('边界 · 受控取值域外的 unit 触发显式警示', () => {
  const html = R.renderUnitDistribution([{
    id: 'x', entryKind: 'USAGE', currency: 'CNY', amount: '1.00000000', unit: 'PIXEL'
  }]);
  assert.ok(html.includes('受控取值域外的计量单位'));
  assert.ok(html.includes('PIXEL'));
});

test('边界 · 超长 id 截断显示、全量进 title', () => {
  const longId = 'x'.repeat(200);
  const html = R.renderLedgerTable([{
    id: longId, entryKind: 'USAGE', currency: 'CNY', amount: '1.00000000'
  }]);
  assert.ok(!html.includes('>' + longId + '<'));
  assert.ok(html.includes('title="' + longId + '"'));
  assert.ok(html.includes('xxxxxxxx…xxxx'));
});

test('边界 · description / label 里的 HTML 特殊字符被转义', () => {
  const html = R.renderLedgerTable([{
    id: 'x', entryKind: 'USAGE', currency: 'CNY', amount: '1.00000000',
    description: '"><img src=x onerror=alert(1)>'
  }]);
  assert.ok(!html.includes('"><img'));
  assert.ok(html.includes('&quot;&gt;&lt;img'));
});

test('边界 · 非法金额串不抛错，原样截断显示', () => {
  assert.equal(R.money('abc', 'CNY'), 'abc');
  assert.equal(R.money('', 'CNY'), '—');
  assert.equal(R.money(null, 'CNY'), '—');
});

test('边界 · ledger-entries 取满 100 条时提示可能截断', () => {
  const many = Array.from({ length: 100 }, (_, i) => ({
    id: 'e' + i, entryKind: 'USAGE', currency: 'CNY', amount: '1.00000000'
  }));
  assert.ok(R.renderLedgerTable(many).includes('已取满 limit=100'));
  assert.ok(!R.renderLedgerTable(many.slice(0, 99)).includes('已取满 limit=100'));
});

test('边界 · 对账 truncated=true 时 pendingCount 标为下界', () => {
  const html = R.renderReconciliation(Object.assign({}, RECONCILIATION, { truncated: true }));
  assert.ok(html.includes('truncated=true · 未扫全'));
  assert.ok(html.includes('下界'));
});

test('边界 · 预估 varianceAmount 为 0 时不染色、仍显示 ¥0.00（0 是真实值）', () => {
  const html = R.renderEstimates([Object.assign({}, ESTIMATES[0], {
    varianceAmount: '0.00000000', actualAmount: '120.00000000'
  })]);
  assert.ok(html.includes('¥0.00'));
  assert.ok(!html.includes('t-dan">¥0.00') && !html.includes('t-ok">¥0.00'));
});

/* ---------------------------- 纯度（机械证明） ---------------------------- */

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'cost.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

test('模块源码不含 Date.now() / new Date() / Intl（纯度 + 单测稳定性）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'cost.js'), 'utf8');
  assert.equal(/Date\.now\(\)|new Date\(|Intl\./.test(src), false);
});

/* ---------------------------- 汇总 ---------------------------- */

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) {
  process.exitCode = 1;
}
