/* render/effect-view.test.js —— effect-view 页渲染层（方案 A 纯函数）Node 单测
 * 运行：node render/effect-view.test.js   （零依赖，退出码 0=全绿 1=有失败）
 *
 * ⚠️ 本页数据源是 **M4 草案 + mock**，不是正式契约：
 *    contracts/openapi.json（86 paths）里 metric / effect / experiment 全部零命中。
 * fixture 形状逐条对齐 contracts/draft/m4-openapi.yaml 的 components.schemas
 * 与 tools/dev/mock_m4/samples.json 的真实样本：
 *   Experiment        yaml Experiment（state: DRAFT|RUNNING|CLOSED）
 *   StrategyEvidence  yaml StrategyEvidence（state 4 态 / evidenceGrade 3 级）
 *   MetricDefinition  yaml MetricDefinition（metricCode 14 个 / scope 7 个）
 *   MetricLatest      yaml MetricLatest
 *   PublicationLabel  yaml PublicationLabel（label 4 个，缺失 → PENDING）
 *   Publication       samples.json 的 publications
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./effect-view.js');

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

/* ---------------------------- fixtures（对齐 mock 样本） ---------------------------- */

const PID = '00000000-0000-4000-8000-000000000001';
const EXP = 'exexexex-1111-4111-8111-111111111111';
const PUB = 'pppppppp-1111-4111-8111-111111111111';
const DEF_LIKE = 'dddddddd-1111-4111-8111-111111111111';
const DEF_VIEW = 'dddddddd-2222-4222-8222-222222222222';

const EXPERIMENTS = [
  { id: EXP, projectId: PID, name: '标题风格 A/B（样例）',
    hypothesis: '疑问句标题提升收藏率（虚构假设）', state: 'RUNNING',
    startedAt: '2026-10-07T09:00:00+08:00', closedAt: null }
];

const EVIDENCE = [
  { id: 'svsvsvsv-1111-4111-8111-111111111111', projectId: PID, experimentId: EXP,
    strategyRef: 'title-style:question', state: 'INCONCLUSIVE',
    evidenceGrade: 'INSUFFICIENT', note: '样本量不足，标证据不足，不声称因果。' }
];

const DEFINITIONS = [
  { id: DEF_LIKE, metricCode: 'LIKE_COUNT', scope: 'CUMULATIVE', valueKind: 'NUMERIC',
    unit: '次', label: '点赞数' },
  { id: DEF_VIEW, metricCode: 'VIEW_COUNT', scope: 'LAST_24H', valueKind: 'NUMERIC',
    unit: '次', label: '近 24h 浏览量' }
];

const METRICS = [
  { publicationId: PUB, metricDefinitionId: DEF_LIKE,
    observationId: 'obobobob-1111-4111-8111-111111111111', valueNum: 12000,
    valueText: null, observedAt: '2026-10-07T18:00:00+08:00', label: 'QUALITY',
    rebuiltAt: '2026-10-07T20:35:00+08:00' },
  { publicationId: PUB, metricDefinitionId: DEF_VIEW,
    observationId: 'obobobob-2222-4222-8222-222222222222', valueNum: null,
    valueText: null, observedAt: '2026-10-07T18:00:00+08:00', label: 'PENDING',
    rebuiltAt: '2026-10-07T20:35:00+08:00' }
];

const PUBLICATIONS = [
  { id: PUB, projectId: PID, deliveryId: '22222222-2222-4222-8222-222222222222',
    publicationKey: 'PUB-2026-0001', noteId: 'note-demo-0001',
    noteUrl: 'https://www.xiaohongshu.com/explore/abc123?xsec_token=XYZ',
    urlCanonical: 'https://www.xiaohongshu.com/explore/abc123', state: 'PUBLISHED',
    contentState: 'VERIFIED', publishedAt: '2026-10-07T12:00:00+08:00' }
];

const DELIVERIES = [
  { id: '22222222-2222-4222-8222-222222222222', projectId: PID, kocRecordId: null,
    contentItemId: 'cccccccc-2222-4222-8222-222222222222',
    versionNodeId: 'vvvvvvvv-2222-4222-8222-222222222222', state: 'DELIVERED',
    eligibilityHash: 'sha256:ef567890', replacesDeliveryId: null,
    deliveredAt: '2026-10-07T10:00:00+08:00', cancelledAt: null }
];

/* ============================ 纯计算 ============================ */

test('tallyEvidence: 按 state × evidenceGrade 二维聚合，受控取值域全部预置 0', () => {
  const t = R.tallyEvidence(EVIDENCE);
  assert.equal(t.total, 1);
  assert.equal(t.byState.INCONCLUSIVE, 1);
  assert.equal(t.byState.SUPPORTED, 0);
  assert.equal(t.byGrade.INSUFFICIENT, 1);
  assert.equal(t.byGrade.EXPERIMENT, 0);
  assert.deepEqual(t.unknownStates, []);
  const empty = R.tallyEvidence([]);
  assert.equal(empty.total, 0);
  R.EVIDENCE_STATES.forEach((s) => assert.equal(empty.byState[s], 0));
});

test('tallyEvidence: 非数组 / 非对象行不抛错', () => {
  assert.equal(R.tallyEvidence(null).total, 0);
  assert.equal(R.tallyEvidence('oops').total, 0);
  assert.equal(R.tallyEvidence([null, 42, EVIDENCE[0]]).total, 1);
});

test('stageAvailability: 七段逐段判定，2/3/7 三段恒为 none（草案就没有）', () => {
  const stages = R.stageAvailability({
    experiments: EXPERIMENTS, evidence: EVIDENCE, metrics: METRICS,
    deliveries: DELIVERIES, publications: PUBLICATIONS
  });
  assert.equal(stages.length, 7);
  assert.equal(stages[0].status, 'live');     // 假设
  assert.equal(stages[1].status, 'none');     // 适用任务
  assert.equal(stages[2].status, 'none');     // 分组与预登记
  assert.equal(stages[3].status, 'live');     // 执行核对
  assert.equal(stages[4].status, 'partial');  // 窗口数据
  assert.equal(stages[5].status, 'live');     // 结论分级
  assert.equal(stages[6].status, 'none');     // 用途范围
  assert.ok(stages[2].reason.includes('没有 GET'));
  assert.ok(stages[4].reason.includes('窗口是否已满'));
});

test('stageAvailability: 空数据时数据段落变 empty 而不是 live', () => {
  const stages = R.stageAvailability({});
  assert.equal(stages[0].status, 'empty');
  assert.equal(stages[3].status, 'empty');
  assert.equal(stages[4].status, 'empty');
  assert.equal(stages[5].status, 'empty');
  /* 无字段的三段不受数据影响，仍是 none */
  assert.equal(stages[1].status, 'none');
  assert.equal(stages[2].status, 'none');
  assert.equal(stages[6].status, 'none');
});

test('indexDefinitions: id → 定义映射', () => {
  const idx = R.indexDefinitions(DEFINITIONS);
  assert.equal(idx[DEF_LIKE].metricCode, 'LIKE_COUNT');
  assert.deepEqual(R.indexDefinitions(null), {});
  assert.deepEqual(R.indexDefinitions([{ metricCode: 'X' }]), {});
});

/* ============================ 正常渲染 ============================ */

test('renderSevenStageNav: 七段逐段标注 + 3 段「无数据源」+ mock 标记', () => {
  const html = R.renderSevenStageNav(R.stageAvailability({
    experiments: EXPERIMENTS, evidence: EVIDENCE, metrics: METRICS,
    deliveries: DELIVERIES, publications: PUBLICATIONS
  }));
  assert.ok(html.includes('3 / 7 段有数据'));
  assert.equal((html.match(/无数据源/g) || []).length, 3 + 1); // 3 个标签 + 说明里一次
  assert.ok(html.includes('部分可接'));
  assert.ok(html.includes('mock 数据源 · 正式契约未定义'));
  assert.ok(html.includes('已全部删除'));
});

test('renderExperimentList: name / hypothesis / state / 起止时间全真', () => {
  const html = R.renderExperimentList(EXPERIMENTS);
  assert.ok(html.includes('标题风格 A/B（样例）'));
  assert.ok(html.includes('疑问句标题提升收藏率'));
  assert.ok(html.includes('实验中'));
  assert.ok(html.includes('开始 2026-10-07 09:00'));
  assert.ok(html.includes('未结束'));
  assert.ok(html.includes('第 2 段「适用任务」没有字段'));
  /* 原型的实验编号不得出现 */
  assert.ok(!html.includes('EXP-007'));
  assert.ok(!html.includes('除湿机选购'));
});

test('renderExperimentCounters: 2 真 2 降级，降级显示「—」而不是 0', () => {
  const html = R.renderExperimentCounters(EXPERIMENTS, EVIDENCE);
  assert.ok(html.includes('state === &#39;RUNNING&#39;'));
  assert.ok(html.includes('state === &#39;SUPPORTED&#39;'));
  assert.ok(html.includes('待核对执行'));
  assert.ok(html.includes('窗口未满'));
  assert.equal((html.match(/无数据源/g) || []).length, 2);
  assert.equal((html.match(/metric-value">—</g) || []).length, 2);
  assert.ok(html.includes('metric-value">1<'));  // RUNNING 1 条
  assert.ok(html.includes('metric-value">0<'));  // SUPPORTED 真的是 0
});

test('renderEvidenceList: state × grade 二维 + INSUFFICIENT 显式警示', () => {
  const html = R.renderEvidenceList(EVIDENCE);
  assert.ok(html.includes('title-style:question'));
  assert.ok(html.includes('无法判定'));
  assert.ok(html.includes('证据不足'));
  assert.ok(html.includes('样本量不足，标证据不足，不声称因果。'));
  assert.ok(html.includes('不得**被当作策略依据') || html.includes('不得'));
  assert.ok(html.includes('mock 数据源'));
});

test('renderEvidenceRedline: 红线教义 + 三个真实计数互相印证', () => {
  const html = R.renderEvidenceRedline(EVIDENCE);
  assert.ok(html.includes('不得自动 SUPPORTED'));
  assert.ok(html.includes('不得把高点赞当因果证据'));
  assert.ok(html.includes('>1 条<'));  // INSUFFICIENT 与 INCONCLUSIVE 各 1 条
  assert.ok(html.includes('>0 条<'));  // SUPPORTED 0 条
  assert.ok(html.includes('教义」与「数据」能互相印证'));
});

test('renderMetricLatest: 数值 / 窗口 / 标签 / 两个时间全真', () => {
  const html = R.renderMetricLatest(METRICS, DEFINITIONS);
  assert.ok(html.includes('点赞数'));
  assert.ok(html.includes('LIKE_COUNT'));
  assert.ok(html.includes('12000 次'));
  assert.ok(html.includes('累计'));         // scope = CUMULATIVE
  assert.ok(html.includes('近 24 小时'));   // scope = LAST_24H
  assert.ok(html.includes('优质'));         // label = QUALITY
  assert.ok(html.includes('2026-10-07 18:00'));
  assert.ok(html.includes('2026-10-07 20:35'));
  assert.ok(html.includes('可重建缓存，不是历史权威来源'));
});

test('renderExecutionCheck: 列发布事实，但不下「执行不足」结论', () => {
  const html = R.renderExecutionCheck(DELIVERIES, PUBLICATIONS);
  assert.ok(html.includes('PUB-2026-0001'));
  assert.ok(html.includes('PUBLISHED'));
  assert.ok(html.includes('VERIFIED'));
  assert.ok(html.includes('www.xiaohongshu.com/explore/abc123'));
  assert.ok(html.includes('只列事实，不下「执行不足」的结论'));
  assert.ok(html.includes('KOC 若删掉了实验段落'));
  /* 不得出现「执行不足」「符合预登记」这类结论性断言 */
  assert.ok(!/>执行不足</.test(html));
});

/* ============================ 降级 ============================ */

test('renderWindowUnavailable: 把「能查到的」与「查不到的」分开说清', () => {
  const html = R.renderWindowUnavailable(DEFINITIONS);
  assert.ok(html.includes('无数据源'));
  assert.ok(html.includes('窗口维度是真的'));
  assert.ok(html.includes('LAST_24H'));
  assert.ok(html.includes('「3d」这个窗口不在枚举里'));
  assert.ok(html.includes('没有 windowDays'));
  assert.ok(html.includes('不渲染「窗口未满」这个判定结论'));
  /* 真实出现过的 scope 作为标签列出 */
  assert.ok(html.includes('累计（CUMULATIVE）'));
  assert.ok(html.includes('近 24 小时（LAST_24H）'));
});

test('renderAssignmentsUnavailable: 明说草案只有 POST、没有 GET，且不编任务数', () => {
  const html = R.renderAssignmentsUnavailable();
  assert.ok(html.includes('无数据源'));
  assert.ok(html.includes('只定义了 post'));
  assert.ok(html.includes('m4-openapi.yaml:611'));
  assert.ok(html.includes('缺的只是列表端点'));
  assert.ok(!html.includes('<table'));
  /* 原型里那两个任务数不得出现 */
  assert.ok(!/\b18\b/.test(html) && !/\b17\b/.test(html));
});

test('renderPurposeScopeUnavailable: 明说不解析 note 来反推用途范围', () => {
  const html = R.renderPurposeScopeUnavailable();
  assert.ok(html.includes('无数据源'));
  assert.ok(html.includes('属于编造，本页不做'));
  assert.ok(html.includes('note'));
});

test('renderStrategyStateMachineDoctrine: 标注六态不存在、草案只有 4 态 3 级', () => {
  const html = R.renderStrategyStateMachineDoctrine();
  assert.ok(html.includes('标准文本 · 非系统数据'));
  assert.ok(html.includes('4 态'));
  assert.ok(html.includes('3 级'));
  assert.ok(html.includes('CANDIDATE / SUPPORTED / UNSUPPORTED / INCONCLUSIVE'));
  assert.ok(html.includes('不是一一对应'));
});

test('renderEffectDoctrine: 教义卡带统一标记且承认窗口无字段', () => {
  const html = R.renderEffectDoctrine();
  assert.ok(html.includes('标准文本 · 非系统数据'));
  assert.ok(html.includes('三个效果问题要分开回答'));
  assert.ok(html.includes('没有字段可判'));
});

/* ============================ 空态 ============================ */

test('各渲染函数：空数组 / null / 非数组都落空态且不抛错', () => {
  [R.renderExperimentList, R.renderEvidenceList].forEach((fn) => {
    [[], null, undefined, 'oops', [null, 42]].forEach((input) => {
      assert.ok(/还没有/.test(fn(input)), fn.name + ' 空态缺失');
    });
  });
  assert.ok(R.renderMetricLatest([], DEFINITIONS).includes('还没有指标投影'));
  assert.ok(R.renderMetricLatest(null, null).includes('还没有指标投影'));
  assert.ok(R.renderExecutionCheck([], []).includes('还没有交付或发布记录'));
  assert.ok(R.renderSevenStageNav([]).includes('未计算可用性'));
});

test('空态 · metric-latest 空态提示「顶层 GET /metric-latest 不存在」', () => {
  assert.ok(R.renderMetricLatest([], []).includes('顶层 GET /metric-latest 不存在'));
});

test('空态 · 计数为 0 时显示 0（真的是 0），降级项仍显示「—」', () => {
  const html = R.renderExperimentCounters([], []);
  assert.ok(html.includes('metric-value">0<'));
  assert.equal((html.match(/metric-value">—</g) || []).length, 2);
});

test('空态 · renderWindowUnavailable 在没有定义时不渲染标签行', () => {
  const html = R.renderWindowUnavailable([]);
  assert.ok(html.includes('没有 windowDays'));
  assert.ok(!html.includes('（CUMULATIVE）'));
});

test('空态 · renderEmpty / renderLoading 文案可定制', () => {
  assert.ok(R.renderEmpty('空').includes('空'));
  assert.ok(R.renderEmpty().includes('没有可展示的效果数据。'));
  assert.ok(R.renderLoading().includes('正在读取实验与证据…'));
});

/* ============================ 错误 ============================ */

test('renderError: 离线时指出数据源是 mock 而非正式契约', () => {
  const html = R.renderError({
    code: 'DEPENDENCY_UNAVAILABLE', message: '连不上后端（network_error）。', offline: true
  });
  assert.ok(html.includes('tools/dev/mock_m4'));
  assert.ok(html.includes('正式契约里没有'));
  assert.ok(html.includes('data-action="retry"'));
});

test('renderError: 404 指出真后端上这些草案路径不存在', () => {
  const html = R.renderError({ code: 'RESOURCE_NOT_FOUND', message: '未找到。' });
  assert.ok(html.includes('真后端上这些路径不存在'));
});

test('renderError: 可重试 / 不可重试 / 裸串 / null 四个分支', () => {
  assert.ok(R.renderError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderError({ code: 'INTERNAL_ERROR', retryable: false }).includes('请按错误码排查'));
  assert.ok(R.renderError('裸字符串错误').includes('裸字符串错误'));
  assert.ok(R.renderError(null).includes('读取效果数据失败'));
});

test('renderError: 错误信息里的 HTML 被转义（防注入）', () => {
  const html = R.renderError({ message: '<script>alert(1)</script>' });
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});

/* ============================ 边界 ============================ */

test('边界 · valueNum 与 valueText 都为 null → 「未采集」而不是 0', () => {
  const html = R.renderMetricLatest(METRICS, DEFINITIONS);
  assert.ok(html.includes('未采集'));
  assert.ok(html.includes('class="tr-warn"'));  // 缺失行标色
  /* 绝不能把缺失渲染成 0 */
  assert.ok(!html.includes('num mono f11">0<'));
});

test('边界 · valueNum === 0 是真实值，必须显示 0 而不是「未采集」', () => {
  const html = R.renderMetricLatest([Object.assign({}, METRICS[0], { valueNum: 0 })], DEFINITIONS);
  assert.ok(html.includes('0 次'));
  assert.ok(!html.includes('未采集'));
});

test('边界 · label === PENDING 标为「数据缺失」且不等于「未达阈值」', () => {
  const html = R.renderMetricLatest(METRICS, DEFINITIONS);
  assert.ok(html.includes('待定（数据缺失）'));
  assert.ok(html.includes('不归零、不进 BELOW_THRESHOLD'));
  assert.ok(html.includes('有 1 个指标的标签是 PENDING'));
});

test('边界 · valueText 有值而 valueNum 为 null → 显示文本值', () => {
  const html = R.renderMetricLatest([Object.assign({}, METRICS[1], {
    valueText: '未公开'
  })], DEFINITIONS);
  assert.ok(html.includes('未公开'));
  assert.ok(!html.includes('未采集'));
});

test('边界 · 未知 state / grade / label / scope 降级为中性 tag，不映射成已有值', () => {
  const ev = R.renderEvidenceList([{
    id: 'x', strategyRef: 'r', state: 'ZOMBIE', evidenceGrade: 'MYSTERY', note: null
  }]);
  assert.ok(ev.includes('ZOMBIE') && ev.includes('MYSTERY'));
  assert.ok(!ev.includes('>范围内支持<') && !ev.includes('>实验证据<'));
  assert.ok(ev.includes('受控取值域外的取值'));

  const exp = R.renderExperimentList([{ id: 'x', name: 'N', state: 'PAUSED' }]);
  assert.ok(exp.includes('PAUSED') && !exp.includes('>实验中<'));

  const m = R.renderMetricLatest([Object.assign({}, METRICS[0], { label: 'HOT' })],
    [Object.assign({}, DEFINITIONS[0], { scope: 'LAST_3D' })]);
  assert.ok(m.includes('HOT'));
  assert.ok(m.includes('LAST_3D') && m.includes('（取值域外）'));
});

test('边界 · metricDefinitionId 查不到定义 → 标「未知指标」而不是留空', () => {
  const html = R.renderMetricLatest(METRICS, []);
  assert.ok(html.includes('未知指标'));
  assert.ok(html.includes('dddddddd…1111'));
});

test('边界 · hypothesis / note 为空 → 显式标出而不是静默留白', () => {
  assert.ok(R.renderExperimentList([{ id: 'x', name: 'N', state: 'DRAFT', hypothesis: null }])
    .includes('（hypothesis 为空）'));
  assert.ok(R.renderEvidenceList([{ id: 'x', strategyRef: 'r', state: 'CANDIDATE',
    evidenceGrade: 'OBSERVATIONAL', note: null }]).includes('（note 为空）'));
});

test('边界 · 超长字段截断显示，全量进 title', () => {
  const longId = 'x'.repeat(200);
  const html = R.renderExperimentList([{
    id: longId, name: 'y'.repeat(200), state: 'RUNNING', hypothesis: 'z'.repeat(400)
  }]);
  assert.ok(html.includes('title="' + longId + '"'));
  assert.ok(html.includes('y'.repeat(48) + '…'));
  assert.ok(html.includes('z'.repeat(180) + '…'));
  assert.ok(!html.includes('z'.repeat(220)));
});

test('边界 · 各字段里的 HTML 特殊字符被转义', () => {
  const inj = '"><img src=x onerror=alert(1)>';
  assert.ok(!R.renderExperimentList([{ id: 'x', name: inj, state: 'RUNNING' }]).includes('"><img'));
  assert.ok(!R.renderEvidenceList([{ id: 'x', strategyRef: inj, state: 'CANDIDATE',
    evidenceGrade: 'EXPERIMENT', note: inj }]).includes('"><img'));
  assert.ok(!R.renderExecutionCheck([], [{ id: 'x', publicationKey: inj, state: inj }]).includes('"><img'));
});

test('边界 · urlCanonical 缺失时显示「—」而不是空白 td', () => {
  const html = R.renderExecutionCheck([], [{ id: 'x', publicationKey: 'K', state: 'PUBLISHED',
    contentState: 'VERIFIED', publishedAt: null, urlCanonical: null }]);
  assert.ok(html.includes('t-mut">—'));
});

/* ---------------------------- 纯度（机械证明） ---------------------------- */

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'effect-view.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

test('模块源码不含 Date.now() / new Date() / Intl（纯度 + 单测稳定性）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'effect-view.js'), 'utf8');
  assert.equal(/Date\.now\(\)|new Date\(|Intl\./.test(src), false);
});

/* ---------------------------- 汇总 ---------------------------- */

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) {
  process.exitCode = 1;
}
