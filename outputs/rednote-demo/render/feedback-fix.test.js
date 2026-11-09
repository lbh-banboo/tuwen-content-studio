/* render/feedback-fix.test.js —— feedback-fix 页渲染层（方案 A 纯函数）Node 单测
 * 运行：node render/feedback-fix.test.js   （零依赖，退出码 0=全绿 1=有失败）
 *
 * fixture 形状逐条对齐后端 pydantic 模型（**不以 contracts/openapi.json 为准**）：
 *   FeedbackItemResponseBody  feedback/schemas.py:279-312
 *   ContentVersionBody        content/api.py:682-710
 * ⚠️ ContentVersionBody 的 `title` 在 contracts/openapi.json 里**缺失**，是导出工具
 *    缺陷（tools/contracts/export_openapi.py:59 _strip_titles 递归删掉所有名为
 *    title 的键，只豁免 info 子树），运行时真有 —— 所以 fixture 里必须带 title。
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./feedback-fix.js');

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

const V1 = 'v1111111-1111-4111-8111-111111111111';
const V2 = 'v2222222-2222-4222-8222-222222222222';
const V3 = 'v3333333-3333-4333-8333-333333333333';

const VERSIONS = [
  {
    versionId: V1, versionNo: 1, title: '卸妆后还要洗脸吗',
    blueprintId: 'bp111111-1111-4111-8111-111111111111',
    adoptedTitleCandidateId: null,
    generationContextId: 'gc111111-1111-4111-8111-111111111111',
    payloadHash: 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2',
    schemaVersion: 1, createdAt: '2026-10-15T10:00:00+08:00', createdBy: null
  },
  {
    versionId: V2, versionNo: 2, title: '卸妆后要不要二次清洁',
    blueprintId: 'bp111111-1111-4111-8111-111111111111',
    adoptedTitleCandidateId: 'tc111111-1111-4111-8111-111111111111',
    generationContextId: null,
    payloadHash: 'ffffffffeeeeeeeedddddddd00000000111111112222222233333333aaaaaaaa',
    schemaVersion: 1, createdAt: '2026-10-16T14:30:00+08:00',
    createdBy: 'u1111111-1111-4111-8111-111111111111'
  }
];

function fb(over) {
  return Object.assign({
    id: 'f1111111-1111-4111-8111-111111111111',
    contentItemId: 'cccccccc-1111-4111-8111-111111111111',
    projectId: '00000000-0000-4000-8000-000000000001',
    observedVersionId: V1,
    anchorKind: 'PARAGRAPH',
    anchorState: 'FRESH',
    paragraphRef: 'p3-9a1f2b7c',
    selectedText: '氨基酸表活占比 62%',
    mediaItemId: null,
    category: 'FACT',
    suggestedCategory: null,
    severity: 'BLOCKING',
    scope: 'CURRENT_ONLY',
    state: 'OPEN',
    authorKind: 'USER',
    body: '参数表述需替换为可核实口径',
    rejectReason: null,
    resolvedVersionId: null,
    createdBy: 'u1111111-1111-4111-8111-111111111111',
    createdAt: '2026-10-16T09:12:00+08:00',
    updatedAt: '2026-10-16T09:12:00+08:00',
    lockVersion: 1
  }, over);
}

const FEEDBACK = [
  fb({}),
  fb({
    id: 'f2222222-2222-4222-8222-222222222222',
    category: 'SCENE', severity: 'WARNING', state: 'RESOLVED',
    scope: 'FUTURE_GENERATIONS', resolvedVersionId: V2,
    body: '人群适用需收窄到油性肌',
    suggestedCategory: 'TONE',
    paragraphRef: 'p5-11aa22bb', selectedText: null
  }),
  fb({
    id: 'f3333333-3333-4333-8333-333333333333',
    anchorKind: 'MEDIA', anchorState: 'STALE',
    mediaItemId: 'm1111111-1111-4111-8111-111111111111',
    paragraphRef: 'p2-33cc44dd',
    category: 'MEDIA_CONSISTENCY', severity: 'WARNING', state: 'IN_PROGRESS',
    body: '起泡图与文案描述不一致，需替换', selectedText: null
  }),
  fb({
    id: 'f4444444-4444-4444-8444-444444444444',
    anchorKind: 'WHOLE', paragraphRef: null, selectedText: null,
    category: 'TONE', severity: 'INFO', state: 'REJECTED',
    rejectReason: '当前语气符合品牌调性，本轮不改',
    body: '整篇语气过于口语化', authorKind: 'AI', suggestedCategory: 'TONE'
  })
];

/* ============================ 正常 ============================ */

test('indexVersions: versionId → versionNo 映射', () => {
  const idx = R.indexVersions(VERSIONS);
  assert.equal(idx[V1], 1);
  assert.equal(idx[V2], 2);
  assert.deepEqual(R.indexVersions(null), {});
  assert.deepEqual(R.indexVersions([null, 7, {}]), {});
});

test('resolveVersionLabel: 有 resolvedVersionId → v{n}；无 → —', () => {
  const idx = R.indexVersions(VERSIONS);
  assert.equal(R.resolveVersionLabel(FEEDBACK[1], idx), 'v2');
  assert.equal(R.resolveVersionLabel(FEEDBACK[0], idx), '—');
  assert.equal(R.resolveVersionLabel(null, idx), '—');
});

test('observedVersionLabel: observedVersionId 必填，映射成 v{n}', () => {
  const idx = R.indexVersions(VERSIONS);
  assert.equal(R.observedVersionLabel(FEEDBACK[0], idx), 'v1');
});

test('tallyByCategory: 按 8 类受控取值域聚合，pct 由最大值归一', () => {
  const t = R.tallyByCategory(FEEDBACK);
  const byCat = {};
  t.forEach((x) => { byCat[x.category] = x; });
  assert.equal(byCat.FACT.count, 1);
  assert.equal(byCat.TONE.count, 1);
  assert.equal(byCat.SCENE.count, 1);
  assert.equal(byCat.MEDIA_CONSISTENCY.count, 1);
  t.forEach((x) => assert.equal(x.pct, 100)); // 全部 1 条 → 都是 100%
  /* 展示序必须是受控取值域的顺序，不是出现顺序 */
  assert.deepEqual(t.map((x) => x.category),
    ['FACT', 'TONE', 'SCENE', 'MEDIA_CONSISTENCY']);
});

test('unresolvedBlocking: 与后端 SQL 同口径（BLOCKING 且不在终态）', () => {
  assert.equal(R.unresolvedBlocking(FEEDBACK).length, 1);
  assert.equal(R.unresolvedBlocking(FEEDBACK)[0].id, FEEDBACK[0].id);
  /* 已解决的 BLOCKING 不算未解决 */
  assert.equal(R.unresolvedBlocking([fb({ severity: 'BLOCKING', state: 'RESOLVED' })]).length, 0);
  assert.equal(R.unresolvedBlocking([fb({ severity: 'BLOCKING', state: 'REJECTED' })]).length, 0);
  assert.equal(R.unresolvedBlocking([fb({ severity: 'BLOCKING', state: 'IN_PROGRESS' })]).length, 1);
});

test('tallyByScope: 按两种 scope 真实计数', () => {
  const t = R.tallyByScope(FEEDBACK);
  assert.equal(t.CURRENT_ONLY, 3);
  assert.equal(t.FUTURE_GENERATIONS, 1);
  assert.deepEqual(t.unknown, []);
});

test('renderFixList: 每行标签全部来自真字段（分类/严重度/粒度/作用域/状态）', () => {
  const html = R.renderFixList(FEEDBACK, R.indexVersions(VERSIONS));
  assert.ok(html.includes('参数表述需替换为可核实口径'));
  assert.ok(html.includes('事实') && html.includes('阻断') && html.includes('段落'));
  assert.ok(html.includes('待处理') && html.includes('已解决') &&
    html.includes('处理中') && html.includes('不采纳'));
  assert.ok(html.includes('观测于 v1 · 解决于 —'));
  assert.ok(html.includes('观测于 v1 · 解决于 v2'));
  assert.ok(html.includes('仅本条') && html.includes('应用后续生成'));
});

test('renderFixList: 未解决阻断意见触发与后端同口径的提示', () => {
  const html = R.renderFixList(FEEDBACK, {});
  assert.ok(html.includes('有 1 条未解决的阻断意见'));
  assert.ok(html.includes('UNRESOLVED_BLOCKING_FEEDBACK'));
  assert.ok(html.includes('service.py:874-878'));
});

test('renderFixList: AI 建议分类与运营已确认分类如实并列（两列都是真字段）', () => {
  const html = R.renderFixList([FEEDBACK[1]], {});
  assert.ok(html.includes('AI 建议分类 语气'));
  assert.ok(html.includes('运营已确认为 场景'));
});

test('renderFixList: MEDIA 锚点显示媒体短 id，PARAGRAPH 显示段落 ref', () => {
  const html = R.renderFixList([FEEDBACK[2]], {});
  assert.ok(html.includes('媒体 m1111111…1111'));
  assert.ok(html.includes('段落 p2-33cc44dd'));
});

test('renderScopeCards: 两张卡真实计数 + 教义文案被标注为非字段', () => {
  const html = R.renderScopeCards(FEEDBACK);
  assert.ok(html.includes('CURRENT_ONLY'));
  assert.ok(html.includes('FUTURE_GENERATIONS'));
  assert.ok(html.includes('>3<span class="unit"> 条</span>'));
  assert.ok(html.includes('>1<span class="unit"> 条</span>'));
  assert.ok(html.includes('是教义，不是接口字段'));
  assert.ok(html.includes('BL-v3')); // 明说原型那个编号没有字段
});

test('renderCategoryTally: 条宽由数据算、标题已改名、三点口径差异都写明', () => {
  const html = R.renderCategoryTally(FEEDBACK);
  assert.ok(html.includes('本条内容的反馈分类分布'));
  assert.ok(!html.includes('同类根因近 30 天频次</span>'));
  assert.ok(html.includes('width:100.0%'));
  assert.ok(html.includes('root_cause / issue_code 两个字段在契约里零命中'));
  assert.ok(html.includes('没有 30 天窗口'));
  assert.ok(!html.includes('data-w='));  // 原型写死的条宽不得出现
});

test('renderRejectedList: 只列 REJECTED，且 rejectReason 必须展示', () => {
  const html = R.renderRejectedList(FEEDBACK);
  assert.ok(html.includes('整篇语气过于口语化'));
  assert.ok(html.includes('当前语气符合品牌调性，本轮不改'));
  assert.ok(!html.includes('参数表述需替换')); // OPEN 的不进这个清单
  assert.ok(html.includes('归一为 NULL'));
});

test('renderVersionTimeline: 版本倒序 + 最新标「当前稿」+ 明说无正文 diff', () => {
  const html = R.renderVersionTimeline(VERSIONS);
  const posV2 = html.indexOf('>v2<');
  const posV1 = html.indexOf('>v1<');
  assert.ok(posV2 > -1 && posV1 > posV2); // v2 在 v1 前面
  assert.ok(html.includes('当前稿'));
  assert.ok(html.includes('已采纳标题候选'));
  assert.ok(html.includes('卸妆后要不要二次清洁')); // title 字段真实存在
  assert.ok(html.includes('历史版本正文没有读取端点'));
  assert.ok(html.includes('knowledge/assets/{assetId}?versionId='));
});

test('renderEventRecordDoctrine: 4 类事件 1 有源 3 降级，被淘汰写明不可读', () => {
  const html = R.renderEventRecordDoctrine(FEEDBACK);
  assert.ok(html.includes('不采纳修改建议'));
  assert.ok(html.includes('tag-ok no-dot f10">1 条'));
  assert.equal((html.match(/无数据源/g) || []).length, 3);
  assert.ok(html.includes('没有任何 GET'));
  assert.ok(html.includes('不得把 GENERATED 当「已通过」显示'));
});

/* ============================ 降级 ============================ */

test('renderRootCauseDoctrine: 保留 4 候选表骨架，结论列全部「无数据源」', () => {
  const html = R.renderRootCauseDoctrine();
  assert.ok(html.includes('标准文本 · 非系统数据'));
  assert.equal((html.match(/无数据源/g) || []).length, 4); // 4 行结论列，一行一个
  /* 原型里的具体结论与 issue_code 一律不得出现 */
  assert.ok(!html.includes('已排除'));
  assert.ok(!html.includes('已确认'));
  assert.ok(!html.includes('FAC-01'));
  assert.ok(html.includes('root_cause / issue_code'));
});

test('renderRoundNotice: 不显示「当前第几轮」，只留纪律说明', () => {
  const html = R.renderRoundNotice();
  assert.ok(html.includes('无数据源'));
  assert.ok(html.includes('modules/jobs/models.py:185'));
  assert.ok(!/第\s*1\s*轮/.test(html));
});

/* ============================ 空态 ============================ */

test('renderFixList: 空 / null / undefined / 非数组都落空态', () => {
  [[], null, undefined, 'oops', [null, 42]].forEach((input) => {
    assert.ok(R.renderFixList(input, {}).includes('还没有任何反馈'));
  });
});

test('renderCategoryTally: 空集不画 0% 柱，显式说明理由', () => {
  const html = R.renderCategoryTally([]);
  assert.ok(html.includes('无分布可算'));
  assert.ok(html.includes('0% 会被读成'));
  assert.ok(!html.includes('width:0.0%'));
  assert.deepEqual(R.tallyByCategory([]), []);
});

test('renderRejectedList: 没有 REJECTED 时说明「这是真的 0 条」', () => {
  const html = R.renderRejectedList([FEEDBACK[0]]);
  assert.ok(html.includes('这是真的 0 条'));
});

test('renderVersionTimeline / renderScopeCards: 空输入不抛错', () => {
  assert.ok(R.renderVersionTimeline([]).includes('未获取到版本历史'));
  assert.ok(R.renderVersionTimeline(null).includes('未获取到版本历史'));
  assert.doesNotThrow(() => R.renderScopeCards(null));
  assert.ok(R.renderScopeCards([]).includes('>0<span class="unit"> 条</span>'));
});

test('renderEmpty / renderLoading: 文案可定制且有默认值', () => {
  assert.ok(R.renderEmpty('没有反馈。').includes('没有反馈。'));
  assert.ok(R.renderEmpty().includes('这条内容还没有反馈。'));
  assert.ok(R.renderLoading().includes('正在读取反馈与版本…'));
  assert.ok(R.renderLoading('自定义…').includes('自定义…'));
});

/* ============================ 错误 ============================ */

test('renderError: 离线错误展示 code / message / 重试按钮', () => {
  const html = R.renderError({
    code: 'DEPENDENCY_UNAVAILABLE',
    message: '连不上后端（network_error），且本页未登记静态样例。',
    offline: true
  });
  assert.ok(html.includes('DEPENDENCY_UNAVAILABLE'));
  assert.ok(html.includes('data-action="retry"'));
});

test('renderError: cursor_binding_mismatch 有专门文案，不落通用提示', () => {
  const html = R.renderError({
    code: 'MALFORMED_REQUEST', message: '游标与筛选条件不匹配。',
    details: { reason: 'cursor_binding_mismatch' }
  });
  assert.ok(html.includes('cursor_binding_mismatch'));
  assert.ok(html.includes('从第一页重新读取'));
  assert.ok(!html.includes('请按错误码排查'));
});

test('renderError: 可重试 / 不可重试 / 裸串 / null 四个分支', () => {
  assert.ok(R.renderError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderError({ code: 'RESOURCE_NOT_FOUND', retryable: false }).includes('请按错误码排查'));
  assert.ok(R.renderError('裸字符串错误').includes('裸字符串错误'));
  assert.ok(R.renderError(null).includes('读取反馈数据失败'));
});

test('renderError: 错误信息里的 HTML 被转义（防注入）', () => {
  const html = R.renderError({ message: '<script>alert(1)</script>' });
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});

/* ============================ 边界 ============================ */

test('边界 · anchorState === STALE 必须显式展示，不得隐藏（R5）', () => {
  const html = R.renderFixList([FEEDBACK[2]], {});
  assert.ok(html.includes('锚点已失效 STALE'));
  /* 条数不因 STALE 减少 */
  assert.equal((R.renderFixList(FEEDBACK, {}).match(/fix-row/g) || []).length, 4);
});

test('边界 · 未知 category / severity / state 降级为中性 tag，不映射成已有值', () => {
  const html = R.renderFixList([fb({
    category: 'MYSTERY', severity: 'CRITICAL', state: 'FROZEN', anchorKind: 'SECTION'
  })], {});
  assert.ok(html.includes('MYSTERY'));
  assert.ok(html.includes('CRITICAL'));
  assert.ok(html.includes('FROZEN'));
  assert.ok(html.includes('SECTION'));
  assert.ok(!html.includes('>事实<') && !html.includes('>阻断<') && !html.includes('>待处理<'));
});

test('边界 · 受控取值域外的 category 在分布里标「（取值域外）」且排在已知之后', () => {
  const t = R.tallyByCategory([fb({ category: 'MYSTERY' }), fb({ category: 'FACT' })]);
  assert.deepEqual(t.map((x) => x.category), ['FACT', 'MYSTERY']);
  assert.equal(t[1].known, false);
  assert.ok(R.renderCategoryTally([fb({ category: 'MYSTERY' })]).includes('（取值域外）'));
});

test('边界 · 受控取值域外的 scope 不计入两格、单独警示', () => {
  const html = R.renderScopeCards([fb({ scope: 'GLOBAL_FOREVER' })]);
  assert.ok(html.includes('受控取值域外的 scope'));
  assert.ok(html.includes('GLOBAL_FOREVER'));
  assert.ok(html.includes('>0<span class="unit"> 条</span>'));
});

test('边界 · resolvedVersionId 指向 versions 里没有的 id → 回退短 id，不猜版本号', () => {
  const label = R.resolveVersionLabel(fb({ resolvedVersionId: V3 }), R.indexVersions(VERSIONS));
  assert.equal(label, 'v3333333…3333');
  assert.ok(!/^v\d+$/.test(label));
});

test('边界 · 超长 id / 超长正文截断显示，全量进 title', () => {
  const longId = 'x'.repeat(200);
  const html = R.renderFixList([fb({ id: longId, body: 'y'.repeat(900) })], {});
  assert.ok(!html.includes('>' + longId + '<'));
  assert.ok(html.includes('title="' + longId + '"'));
  assert.ok(html.includes('y'.repeat(220) + '…'));
  assert.ok(!html.includes('y'.repeat(260)));
});

test('边界 · 字段里的 HTML 特殊字符被转义（body / rejectReason / paragraphRef）', () => {
  const html = R.renderFixList([fb({
    state: 'REJECTED',
    body: '"><img src=x onerror=alert(1)>',
    rejectReason: '<svg onload=alert(2)>',
    paragraphRef: '<b>p1</b>'
  })], {});
  assert.ok(!html.includes('"><img'));
  assert.ok(!html.includes('<svg'));
  assert.ok(!html.includes('<b>p1</b>'));
  assert.ok(html.includes('&quot;&gt;&lt;img'));
});

test('边界 · REJECTED 但 rejectReason 为空 → 标为异常而不是静默留白', () => {
  const html = R.renderFixList([fb({ state: 'REJECTED', rejectReason: null })], {});
  assert.ok(html.includes('后端保证 REJECTED 必有理由，此处为空属异常'));
});

test('边界 · 反馈取满 100 条时提示还有更多', () => {
  const many = Array.from({ length: 100 }, (_, i) => fb({ id: 'f' + i }));
  assert.ok(R.renderFixList(many, {}).includes('已取满 limit=100'));
  assert.ok(!R.renderFixList(many.slice(0, 99), {}).includes('已取满 limit=100'));
});

test('边界 · versionNo 缺失的版本行不进索引，时间线也不抛错', () => {
  assert.deepEqual(R.indexVersions([{ versionId: 'x' }]), {});
  assert.doesNotThrow(() => R.renderVersionTimeline([{ versionId: 'x', versionNo: null }]));
});

/* ---------------------------- 纯度（机械证明） ---------------------------- */

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'feedback-fix.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

test('模块源码不含 Date.now() / new Date() / Intl（纯度 + 单测稳定性）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'feedback-fix.js'), 'utf8');
  assert.equal(/Date\.now\(\)|new Date\(|Intl\./.test(src), false);
});

/* ---------------------------- 汇总 ---------------------------- */

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) {
  process.exitCode = 1;
}
