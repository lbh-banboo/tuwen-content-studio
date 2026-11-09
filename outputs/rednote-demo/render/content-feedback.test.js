/* render/content-feedback.test.js —— content-feedback 页渲染层 Node 单测
 * 运行：node render/content-feedback.test.js   （零依赖，退出码 0=全绿 1=有失败）
 *
 * fixture 形状逐条对齐后端 pydantic 模型（**不以 contracts/openapi.json 为准** ——
 * tools/contracts/export_openapi.py:59 _strip_titles() 递归删掉所有名为 title 的键，
 * 只豁免 info 子树，于是 PreviewResponseBody.title（feedback/schemas.py:231）、
 * PreviewTitleCandidateBody.title（:211）、EditedVersionResponseBody.title（:260）、
 * ContentSummaryBody.title（content/api.py:597）在契约快照里**缺失**，
 * 而 required 数组里还留着孤儿 "title" —— 运行时这些字段真实存在。
 * 所以本文件的 fixture **必须带 title**）：
 *   PreviewResponseBody        feedback/schemas.py:215-249
 *   FeedbackItemResponseBody   feedback/schemas.py:279-312
 *   EditedVersionResponseBody  feedback/schemas.py:251-271
 *   ReviewBatchResponseBody    feedback/schemas.py:334-351
 *   MediaSummaryBody           media/api.py:605-618（currentVersionId，不是 No）
 *   ContentSummaryBody         content/api.py:593-601
 *   ContentVersionBody         content/api.py:699-710
 *   RegenerationAcceptedBody   content/api.py:569-583
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./content-feedback.js');

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

const CID = 'cccccccc-1111-4111-8111-111111111111';
const V1 = 'v1111111-1111-4111-8111-111111111111';
const V2 = 'v2222222-2222-4222-8222-222222222222';
const M1 = 'm1111111-1111-4111-8111-111111111111';

const PREVIEW = {
  contentId: CID,
  projectId: '00000000-0000-4000-8000-000000000001',
  currentVersionId: V2,
  versionNo: 2,
  lockVersion: 5,
  etag: '"' + CID + ':5"',
  title: '卸妆后要不要二次清洁',
  body: '第一段正文。\n\n第二段正文。',
  tags: ['卸妆', '氨基酸洁面'],
  bindings: [{ claimId: 'cl1', paragraphRef: 'p1-aaaa1111' }],
  paragraphs: [
    { index: 0, text: '卸妆后是否需要二次清洁，取决于卸妆产品的残留情况。', ref: 'p1-aaaa1111' },
    { index: 1, text: '氨基酸表活占比 62%，温和度较高。', ref: 'p2-bbbb2222' }
  ],
  mediaItems: [
    { mediaItemId: M1, paragraphRef: 'p1-aaaa1111', sortOrder: 0, state: 'READY' },
    { mediaItemId: 'm2222222-2222-4222-8222-222222222222', paragraphRef: null, sortOrder: 1, state: 'PENDING' }
  ],
  titleCandidates: [
    { titleCandidateId: 'tc111111-1111-4111-8111-111111111111', title: '卸妆后要不要二次清洁', isAdopted: true },
    { titleCandidateId: 'tc222222-2222-4222-8222-222222222222', title: '卸妆后还要洗脸吗', isAdopted: false }
  ],
  feedbackCount: 4,
  selfCheckSummary: null
};

function fb(over) {
  return Object.assign({
    id: 'f1111111-1111-4111-8111-111111111111',
    contentItemId: CID,
    projectId: '00000000-0000-4000-8000-000000000001',
    observedVersionId: V1,
    anchorKind: 'PARAGRAPH',
    anchorState: 'FRESH',
    paragraphRef: 'p2-bbbb2222',
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
  fb({ id: 'f2222222-2222-4222-8222-222222222222', category: 'SCENE', severity: 'WARNING',
    state: 'RESOLVED', scope: 'FUTURE_GENERATIONS', resolvedVersionId: V2,
    suggestedCategory: 'TONE', body: '人群适用需收窄到油性肌', lockVersion: 3 }),
  fb({ id: 'f3333333-3333-4333-8333-333333333333', anchorKind: 'MEDIA', anchorState: 'STALE',
    mediaItemId: M1, paragraphRef: null, selectedText: null,
    category: 'MEDIA_CONSISTENCY', severity: 'WARNING', state: 'IN_PROGRESS',
    body: '起泡图与文案不一致', lockVersion: 2 }),
  fb({ id: 'f4444444-4444-4444-8444-444444444444', anchorKind: 'WHOLE', paragraphRef: null,
    selectedText: null, category: 'TONE', severity: 'INFO', state: 'REJECTED',
    rejectReason: '当前语气符合品牌调性，本轮不改', authorKind: 'AI',
    body: '整篇语气过于口语化', lockVersion: 4 })
];

const VERSIONS = [
  { versionId: V1, versionNo: 1, title: '卸妆后还要洗脸吗', blueprintId: null,
    adoptedTitleCandidateId: null, generationContextId: null,
    payloadHash: 'aaaa1111bbbb2222', schemaVersion: 1,
    createdAt: '2026-10-15T10:00:00+08:00', createdBy: null },
  { versionId: V2, versionNo: 2, title: '卸妆后要不要二次清洁', blueprintId: null,
    adoptedTitleCandidateId: 'tc111111-1111-4111-8111-111111111111',
    generationContextId: null, payloadHash: 'cccc3333dddd4444', schemaVersion: 1,
    createdAt: '2026-10-16T14:30:00+08:00', createdBy: 'u1' }
];

const MEDIA = [
  { mediaId: M1, contentId: CID, projectId: '00000000-0000-4000-8000-000000000001',
    state: 'READY', paragraphRef: 'p1-aaaa1111', sortOrder: 0, currentVersionId: 'mv111111-1111-4111-8111-111111111111' },
  { mediaId: 'm2222222-2222-4222-8222-222222222222', contentId: CID,
    projectId: '00000000-0000-4000-8000-000000000001', state: 'PENDING',
    paragraphRef: null, sortOrder: 1, currentVersionId: null }
];

const CONTENTS = [
  { contentId: CID, projectId: '00000000-0000-4000-8000-000000000001',
    title: '卸妆后要不要二次清洁', state: 'GENERATED', currentVersionNo: 2,
    createdAt: '2026-10-15T10:00:00+08:00', updatedAt: '2026-10-16T14:30:00+08:00' },
  { contentId: 'cccccccc-2222-4222-8222-222222222222',
    projectId: '00000000-0000-4000-8000-000000000001', title: null, state: 'FAILED',
    currentVersionNo: 1, createdAt: '2026-10-15T10:00:00+08:00',
    updatedAt: '2026-10-15T11:00:00+08:00' }
];

/* ============================ 请求描述符（本页关键） ============================ */

test('feedbackEtag: 严格等于 "{feedbackId}:{lockVersion}"（concurrency.py:47）', () => {
  assert.equal(R.feedbackEtag(FEEDBACK[0]), '"f1111111-1111-4111-8111-111111111111:1"');
  assert.equal(R.feedbackEtag(FEEDBACK[1]), '"f2222222-2222-4222-8222-222222222222:3"');
  /* lockVersion 为 0 是合法的，不能被当成缺失 */
  assert.equal(R.feedbackEtag(fb({ id: 'x', lockVersion: 0 })), '"x:0"');
  /* 缺 lockVersion / 缺 id 时返回空串，绝不拼出一个假 ETag */
  assert.equal(R.feedbackEtag(fb({ lockVersion: null })), '');
  assert.equal(R.feedbackEtag({ lockVersion: 1 }), '');
  assert.equal(R.feedbackEtag(null), '');
});

test('editRequest: 只带传入的字段（extra="forbid" 下多一个键就 422）', () => {
  const r = R.editRequest(PREVIEW, { title: '新标题' });
  assert.deepEqual(r, {
    method: 'POST',
    path: '/contents/' + CID + '/edits',
    body: { title: '新标题' },
    ifMatch: '"' + CID + ':5"'
  });
  /* 未传的字段不出现在 body 里 —— 服务端语义是「未传沿用上一版」 */
  assert.deepEqual(Object.keys(R.editRequest(PREVIEW, { body: 'x' }).body), ['body']);
  assert.deepEqual(R.editRequest(PREVIEW, {}).body, {});
  assert.deepEqual(R.editRequest(PREVIEW, { tags: ['a', 'b'] }).body, { tags: ['a', 'b'] });
});

test('editRequest: body 里绝不出现 contentId 等多余键（否则 422）', () => {
  const r = R.editRequest(PREVIEW, { title: 't', contentId: CID, etag: 'x', foo: 1 });
  assert.deepEqual(Object.keys(r.body), ['title']);
});

test('editRequest: If-Match 取 preview.etag 原值，不自己拼', () => {
  assert.equal(R.editRequest(PREVIEW, { title: 't' }).ifMatch, PREVIEW.etag);
  assert.equal(R.editRequest({ contentId: 'x' }, { title: 't' }).ifMatch, '');
});

test('advanceRequest: PATCH /feedback/{id} + If-Match = feedbackEtag', () => {
  const r = R.advanceRequest(FEEDBACK[0], 'IN_PROGRESS');
  assert.deepEqual(r, {
    method: 'PATCH',
    path: '/feedback/f1111111-1111-4111-8111-111111111111',
    body: { toState: 'IN_PROGRESS' },
    ifMatch: '"f1111111-1111-4111-8111-111111111111:1"'
  });
});

test('advanceRequest: REJECTED 必带 rejectReason；非 REJECTED 一律不带', () => {
  const rej = R.advanceRequest(FEEDBACK[0], 'REJECTED', { rejectReason: '不改' });
  assert.equal(rej.body.rejectReason, '不改');
  /* 非 REJECTED 即使调用方传了也不发 —— 服务端会归一为 NULL，发了反而误导 */
  const ok = R.advanceRequest(FEEDBACK[0], 'RESOLVED', { rejectReason: '不改' });
  assert.equal(Object.prototype.hasOwnProperty.call(ok.body, 'rejectReason'), false);
  /* rejectReason 超 500 字被截断 */
  const long = R.advanceRequest(FEEDBACK[0], 'REJECTED', { rejectReason: 'x'.repeat(900) });
  assert.equal(long.body.rejectReason.length, 501); // 500 + 省略号
});

test('advanceRequest: 允许回退（RESOLVED → OPEN），因为后端没有转移表', () => {
  const back = R.advanceRequest(FEEDBACK[1], 'OPEN');
  assert.equal(back.body.toState, 'OPEN');
  assert.equal(back.ifMatch, '"f2222222-2222-4222-8222-222222222222:3"');
  /* 自转也不拦 */
  assert.equal(R.advanceRequest(FEEDBACK[0], 'OPEN').body.toState, 'OPEN');
});

test('advanceRequest: note / resultingVersionId 可选且只在有值时出现', () => {
  const r = R.advanceRequest(FEEDBACK[0], 'RESOLVED', { note: '已改', resultingVersionId: V2 });
  assert.deepEqual(r.body, { toState: 'RESOLVED', note: '已改', resultingVersionId: V2 });
  assert.deepEqual(R.advanceRequest(FEEDBACK[0], 'RESOLVED', {}).body, { toState: 'RESOLVED' });
});

test('createFeedbackRequest: 必填 6 项齐全、无 If-Match、不传 authorKind', () => {
  const r = R.createFeedbackRequest(PREVIEW, {
    anchorKind: 'PARAGRAPH', paragraphRef: 'p2-bbbb2222', category: 'FACT',
    severity: 'BLOCKING', scope: 'CURRENT_ONLY', body: '口径要换'
  });
  assert.equal(r.method, 'POST');
  assert.equal(r.path, '/contents/' + CID + '/feedback');
  assert.equal(r.ifMatch, null);
  assert.equal(r.body.observedVersionId, V2);
  assert.equal(r.body.paragraphRef, 'p2-bbbb2222');
  assert.equal(Object.prototype.hasOwnProperty.call(r.body, 'authorKind'), false);
  assert.equal(Object.prototype.hasOwnProperty.call(r.body, 'createdBy'), false);
});

test('createFeedbackRequest: 锚点三选一，列占用互斥', () => {
  const whole = R.createFeedbackRequest(PREVIEW, {
    anchorKind: 'WHOLE', paragraphRef: 'p1-aaaa1111', mediaItemId: 'm1', body: 'x'
  });
  assert.equal(Object.prototype.hasOwnProperty.call(whole.body, 'paragraphRef'), false);
  assert.equal(Object.prototype.hasOwnProperty.call(whole.body, 'mediaItemId'), false);

  const media = R.createFeedbackRequest(PREVIEW, {
    anchorKind: 'MEDIA', paragraphRef: 'p1-aaaa1111', mediaItemId: M1, body: 'x'
  });
  assert.equal(media.body.mediaItemId, M1);
  assert.equal(Object.prototype.hasOwnProperty.call(media.body, 'paragraphRef'), false);
});

test('createFeedbackRequest: 取值域外的值被收敛到安全默认，不原样发出', () => {
  const r = R.createFeedbackRequest(PREVIEW, {
    anchorKind: 'SECTION', category: 'MYSTERY', severity: 'CRITICAL', scope: 'FOREVER', body: 'x'
  });
  assert.equal(r.body.anchorKind, 'WHOLE');
  assert.equal(r.body.category, 'OTHER');
  assert.equal(r.body.severity, 'INFO');
  assert.equal(r.body.scope, 'CURRENT_ONLY');
});

test('createFeedbackRequest: body / selectedText 按后端上限截断（2000 / 4000）', () => {
  const r = R.createFeedbackRequest(PREVIEW, {
    anchorKind: 'WHOLE', body: 'x'.repeat(5000), selectedText: 'y'.repeat(9000)
  });
  assert.equal(r.body.body.length, R.MAX.body + 1);
  assert.equal(r.body.selectedText.length, R.MAX.selectedText + 1);
});

test('reviewRequest: subjectVersionId 取当前版本、无 If-Match、supersedesId 可选', () => {
  const r = R.reviewRequest(PREVIEW, 'APPROVED', '事实已核对');
  assert.deepEqual(r, {
    method: 'POST', path: '/contents/' + CID + '/reviews',
    body: { subjectVersionId: V2, decision: 'APPROVED', reason: '事实已核对' },
    ifMatch: null
  });
  assert.equal(R.reviewRequest(PREVIEW, 'ELIMINATED', 'r', 'rv1').body.supersedesId, 'rv1');
  /* 取值域外收敛为 ELIMINATED（不敢默认通过） */
  assert.equal(R.reviewRequest(PREVIEW, 'MAYBE', 'r').body.decision, 'ELIMINATED');
});

test('batchReviewRequest: 不传版本、decision 受控、路径带 projectId', () => {
  const r = R.batchReviewRequest('p1', [CID, 'c2'], 'APPROVED', '批量通过');
  assert.deepEqual(r, {
    method: 'POST', path: '/projects/p1/content-reviews',
    body: { contentItemIds: [CID, 'c2'], decision: 'APPROVED', reason: '批量通过' },
    ifMatch: null
  });
  assert.equal(Object.prototype.hasOwnProperty.call(r.body, 'subjectVersionId'), false);
  assert.deepEqual(R.batchReviewRequest('p1', null, 'APPROVED', 'r').body.contentItemIds, []);
});

test('regenerateRequest: 三字段全必填且 scope 受控（TAGS 会被服务层折成 COPY）', () => {
  const r = R.regenerateRequest(PREVIEW, 'TITLE', 'CONTENT_GENERATION', 'mp1');
  assert.deepEqual(r.body, { scope: 'TITLE', batchKind: 'CONTENT_GENERATION', modelProfileId: 'mp1' });
  assert.equal(r.path, '/contents/' + CID + '/regenerations');
  assert.equal(R.regenerateRequest(PREVIEW, 'MYSTERY', 'b', 'm').body.scope, 'FULL');
  assert.equal(R.regenerateRequest(PREVIEW, 'TAGS', 'b', 'm').body.scope, 'TAGS'); // 原样发，由服务端折叠
});

/* ============================ describeError 四条分支 ============================ */

test('describeError · 412 内容锁冲突：回带 currentEtag 并提示比较', () => {
  const d = R.describeError({
    code: 'VERSION_CONFLICT', status: 412,
    details: { currentEtag: '"' + CID + ':6"' }
  });
  assert.equal(d.kind, 'content_conflict');
  assert.ok(d.hint.includes('"' + CID + ':6"'));
  assert.equal(d.currentEtag, '"' + CID + ':6"');
});

test('describeError · 412 反馈锁冲突：用专用 reason 与内容锁区分开', () => {
  const d = R.describeError({
    code: 'VERSION_CONFLICT', status: 412,
    details: { reason: 'FEEDBACK_LOCK_VERSION_MISMATCH' }
  });
  assert.equal(d.kind, 'feedback_conflict');
  assert.ok(d.hint.includes('FEEDBACK_LOCK_VERSION_MISMATCH'));
  assert.ok(d.hint.includes('与内容锁冲突是两个不同的 reason'));
});

test('describeError · 428：明确区分「没提供前置条件」与「版本不匹配」', () => {
  const d = R.describeError({ code: 'PRECONDITION_REQUIRED', status: 428, details: { reason: 'if_match_missing' } });
  assert.equal(d.kind, 'precondition');
  assert.ok(d.hint.includes('428 与 412 是两回事'));
  const w = R.describeError({ status: 428, code: 'PRECONDITION_REQUIRED', details: { reason: 'if_match_wildcard_unsupported' } });
  assert.ok(w.hint.includes('不支持通配符'));
});

test('describeError · 409 阻断：列出 blockingFeedbackIds', () => {
  const d = R.describeError({
    code: 'INVALID_TRANSITION', status: 409,
    details: { reason: 'UNRESOLVED_BLOCKING_FEEDBACK', blockingFeedbackIds: [FEEDBACK[0].id, 'f9'] }
  });
  assert.equal(d.kind, 'blocking');
  assert.deepEqual(d.blockingFeedbackIds, [FEEDBACK[0].id, 'f9']);
  assert.ok(d.hint.includes('2 条意见'));
  assert.ok(d.hint.includes('f1111111…1111'));
});

test('describeError · 400 cursor_binding_mismatch 与 422 校验各有文案', () => {
  const c = R.describeError({ code: 'MALFORMED_REQUEST', status: 400, details: { reason: 'cursor_binding_mismatch' } });
  assert.equal(c.kind, 'cursor');
  assert.ok(c.hint.includes('从第一页重新读取'));
  const v = R.describeError({ code: 'VALIDATION_FAILED', status: 422, message: '字段非法' });
  assert.equal(v.kind, 'validation');
  assert.ok(v.hint.includes('extra="forbid"'));
});

test('describeError · 兜底分支：离线 / 可重试 / 裸串 / null', () => {
  assert.ok(R.describeError({ code: 'DEPENDENCY_UNAVAILABLE', offline: true }).hint.includes('连不上后端'));
  assert.ok(R.describeError({ code: 'RATE_LIMITED', retryable: true }).hint.includes('该错误可重试'));
  assert.equal(R.describeError('裸串').title, '裸串');
  assert.equal(R.describeError(null).title, '操作失败。');
});

/* ============================ 纯计算 ============================ */

test('indexVersions / versionLabel: 映射成 v{n}，查不到回退短 id', () => {
  const idx = R.indexVersions(VERSIONS);
  assert.equal(R.versionLabel(V1, idx), 'v1');
  assert.equal(R.versionLabel(V2, idx), 'v2');
  assert.equal(R.versionLabel('v9999999-9999-4999-8999-999999999999', idx), 'v9999999…9999');
  assert.equal(R.versionLabel(null, idx), '—');
});

test('unresolvedBlocking: 与后端 SQL 同口径（BLOCKING 且不在终态）', () => {
  assert.equal(R.unresolvedBlocking(FEEDBACK).length, 1);
  assert.equal(R.unresolvedBlocking([fb({ severity: 'BLOCKING', state: 'RESOLVED' })]).length, 0);
  assert.equal(R.unresolvedBlocking([fb({ severity: 'BLOCKING', state: 'REJECTED' })]).length, 0);
  assert.equal(R.unresolvedBlocking([fb({ severity: 'BLOCKING', state: 'IN_PROGRESS' })]).length, 1);
  assert.deepEqual(R.unresolvedBlocking(null), []);
});

test('tallyFeedback: 按 state / severity / category 聚合 + STALE + AI 计数', () => {
  const t = R.tallyFeedback(FEEDBACK);
  assert.equal(t.total, 4);
  assert.equal(t.byState.OPEN, 1);
  assert.equal(t.byState.RESOLVED, 1);
  assert.equal(t.byState.IN_PROGRESS, 1);
  assert.equal(t.byState.REJECTED, 1);
  assert.equal(t.bySeverity.BLOCKING, 1);
  assert.equal(t.bySeverity.WARNING, 2);
  assert.equal(t.stale, 1);
  assert.equal(t.ai, 1);
  assert.equal(t.unresolvedBlocking, 1);
  assert.equal(t.pending, 2); // OPEN + IN_PROGRESS
});

/* ============================ 正常渲染 ============================ */

test('renderPreviewPhone: 标题 / 段落 / 标签 / 图位全部来自 preview', () => {
  const html = R.renderPreviewPhone(PREVIEW);
  assert.ok(html.includes('卸妆后要不要二次清洁'));
  assert.ok(html.includes('取决于卸妆产品的残留情况'));
  assert.ok(html.includes('data-paragraph-ref="p1-aaaa1111"'));
  assert.ok(html.includes('#卸妆'));
  assert.ok(html.includes('图位（2）'));
  assert.ok(html.includes('预览不替代真实设备验收'));
  assert.ok(html.includes('手机预览 · v2'));
});

test('renderParagraphAnchors: 每段一个 ref + 提意见按钮', () => {
  const html = R.renderParagraphAnchors(PREVIEW);
  assert.ok(html.includes('p1-aaaa1111') && html.includes('p2-bbbb2222'));
  assert.ok(html.includes('data-action="anchor-paragraph" data-paragraph-ref="p2-bbbb2222"'));
  assert.ok(html.includes('2 段'));
});

test('renderMetaFields: 版本真接，场景与基线标「无数据源」', () => {
  const html = R.renderMetaFields(PREVIEW);
  assert.ok(html.includes('v2'));
  assert.ok(html.includes('2 个图位'));
  assert.equal((html.match(/无数据源/g) || []).length, 2);
  assert.ok(html.includes('BL-v3 / PLAN-v2 是编的，已删除'));
  assert.ok(!html.includes('卸妆后二次清洁</span>')); // 不保留原型编的场景值
});

test('renderFeedbackList: 标签全真 + 版本箭头 + AI 建议分类并列', () => {
  const html = R.renderFeedbackList(FEEDBACK, R.indexVersions(VERSIONS));
  assert.ok(html.includes('事实') && html.includes('阻断') && html.includes('段落'));
  assert.ok(html.includes('v1 → —'));
  assert.ok(html.includes('v1 → v2'));
  assert.ok(html.includes('AI 建议分类 语气 · 运营已确认为 场景'));
  assert.ok(html.includes('AI 提出'));
  assert.ok(html.includes('当前语气符合品牌调性'));
  assert.ok(html.includes('ETag &quot;f1111111-1111-4111-8111-111111111111:1&quot;'));
});

test('renderFeedbackCounters: 4 个计数全部由数据算出', () => {
  const html = R.renderFeedbackCounters(FEEDBACK);
  assert.ok(html.includes('意见总数'));
  assert.ok(html.includes('>4<'));  // total
  assert.ok(html.includes('>2<'));  // pending
  assert.ok(html.includes('>1<'));  // unresolvedBlocking / stale
  assert.ok(html.includes('与后端 SQL 同口径'));
});

test('renderBlockingGate: 有阻断时未满足并列出 id；无阻断时已满足', () => {
  const bad = R.renderBlockingGate(FEEDBACK);
  assert.ok(bad.includes('未满足：有 1 条未解决的阻断意见'));
  assert.ok(bad.includes('f1111111…1111'));
  assert.ok(bad.includes('UNRESOLVED_BLOCKING_FEEDBACK'));
  const good = R.renderBlockingGate([FEEDBACK[1], FEEDBACK[3]]);
  assert.ok(good.includes('已满足：没有未解决的阻断意见'));
});

test('renderContentsTable: 4 列真 3 列降级，且不把 GENERATED 当已通过', () => {
  const html = R.renderContentsTable(CONTENTS, CID);
  assert.ok(html.includes('已生成'));   // GENERATED 的中文是「已生成」
  assert.ok(!html.includes('>已通过<'));
  assert.ok(html.includes('生成失败'));
  assert.ok(html.includes('（无标题）'));
  assert.ok(html.includes('<tr class="tr-sel">'));
  assert.equal((html.match(/无数据源/g) || []).length, 5); // 2 行 × 2 列 + 说明里引用一次
  assert.ok(html.includes('不是「本轮 N 条」'));
  assert.ok(html.includes('batch 零命中'));
});

test('renderMediaStrip: 2 列真 + 明说 currentVersionId 不是 No', () => {
  const html = R.renderMediaStrip(MEDIA);
  assert.ok(html.includes('p1-aaaa1111'));
  assert.ok(html.includes('未对位'));    // paragraphRef 为 null
  assert.ok(html.includes('无版本'));    // currentVersionId 为 null
  assert.ok(html.includes('currentVersionId**（注意是 Id 不是 No）') ||
    html.includes('注意是 Id 不是 No'));
  assert.ok(html.includes('只有 2/4 且语义不等价'));
});

test('renderAdvanceForm: 四个目标状态都可选 + 当前状态标出 + ETag 可见', () => {
  const html = R.renderAdvanceForm(FEEDBACK[1]);
  R.STATES.forEach((s) => assert.ok(html.includes('data-to-state="' + s + '"'), s));
  assert.ok(html.includes('（当前）'));
  assert.ok(html.includes('ETag &quot;f2222222-2222-4222-8222-222222222222:3&quot;'));
  assert.ok(html.includes('没有 from→to 合法转移表'));
  assert.ok(html.includes('归一为 NULL'));
  /* 不得渲染成单向进度条 */
  assert.ok(!html.includes('progress'));
});

test('renderEditForm: 预填当前值 + 显示 If-Match + 说明「只提交被改动的字段」', () => {
  const html = R.renderEditForm(PREVIEW);
  assert.ok(html.includes('value="卸妆后要不要二次清洁"'));
  assert.ok(html.includes('卸妆,氨基酸洁面'));
  assert.ok(html.includes('data-action="save-edit"'));
  assert.ok(html.includes('If-Match &quot;' + CID + ':5&quot;'));
  assert.ok(html.includes('不返回更新后的内容'));
  assert.ok(html.includes('extra="forbid"') || html.includes('extra=&quot;forbid&quot;'));
  assert.ok(html.includes('直接编辑 · 当前稿 v2'));
});

test('renderEditForm: preview 缺失落空态；缺 etag 时显式标出', () => {
  assert.ok(R.renderEditForm(null).includes('未获取到预览，无法编辑'));
  assert.ok(R.renderEditForm({ contentId: 'x', versionNo: 1 }).includes('（缺 etag）'));
});

test('renderFeedbackForm: 下拉全部受控 + 字数上限写进 maxlength', () => {
  const html = R.renderFeedbackForm(PREVIEW, { anchorKind: 'PARAGRAPH' });
  R.CATEGORIES.forEach((c) => assert.ok(html.includes('value="' + c + '"'), c));
  R.SEVERITIES.forEach((s) => assert.ok(html.includes('value="' + s + '"'), s));
  R.SCOPES.forEach((s) => assert.ok(html.includes('value="' + s + '"'), s));
  assert.ok(html.includes('maxlength="2000"'));
  assert.ok(html.includes('maxlength="200"'));
  assert.ok(html.includes('意图可以传，身份不能传'));
  assert.ok(html.includes('观测版本固定取当前稿 v2'));
});

test('renderFeedbackForm: MEDIA 锚点给媒体下拉，PARAGRAPH 给 ref 输入', () => {
  const media = R.renderFeedbackForm(PREVIEW, { anchorKind: 'MEDIA' });
  assert.ok(media.includes('id="cf-media"'));
  assert.ok(media.includes('#0 m1111111…1111'));
  assert.ok(!media.includes('id="cf-pref"'));
  const para = R.renderFeedbackForm(PREVIEW, { anchorKind: 'PARAGRAPH' });
  assert.ok(para.includes('id="cf-pref"'));
  assert.ok(!para.includes('id="cf-media"'));
});

test('renderReviewActions: 两个 decision 受控 + 写得进读不回来写明', () => {
  const html = R.renderReviewActions(PREVIEW, CONTENTS);
  assert.ok(html.includes('data-decision="APPROVED"'));
  assert.ok(html.includes('data-decision="ELIMINATED"'));
  assert.ok(html.includes('批量通过项目下 2 条内容'));
  assert.ok(html.includes('没有任何 GET'));
  assert.ok(html.includes('人类可读诊断串，不是错误码'));
  assert.ok(html.includes('不预先禁用按钮'));
});

test('renderBatchReviewResult: 逐条成功/失败 + error 当诊断串展示', () => {
  const html = R.renderBatchReviewResult({
    results: [
      { contentItemId: CID, ok: true, review: { id: 'rv111111-1111-4111-8111-111111111111' }, error: null },
      { contentItemId: 'c2', ok: false, review: null, error: '存在未解决的阻断反馈，无法通过' }
    ]
  });
  assert.ok(html.includes('1 / 2 成功'));
  assert.ok(html.includes('存在未解决的阻断反馈'));
  assert.ok(html.includes('tr-danger'));
  assert.ok(html.includes('不是错误码'));
  assert.equal(R.renderBatchReviewResult(null), '');
});

test('renderEditResult: 明说返回的是新版本摘要、不含更新后的内容', () => {
  const html = R.renderEditResult({
    id: 'nv111111-1111-4111-8111-111111111111', versionNo: 3, title: '新标题',
    payloadHash: 'eeee5555ffff6666', schemaVersion: 1,
    createdAt: '2026-10-18T10:00:00+08:00', createdBy: 'u1'
  });
  assert.ok(html.includes('新版本 v3'));
  assert.ok(html.includes('不返回更新后的内容'));
  assert.equal(R.renderEditResult(null), '');
});

test('renderRegenResult: 拿到 jobId 但如实写「进度不可查」，不画进度条', () => {
  const html = R.renderRegenResult({
    contentId: CID, scope: 'TITLE', taskKinds: ['CONTENT_COPY'],
    job: { jobId: 'jb111111-1111-4111-8111-111111111111', kind: 'CONTENT_COPY', created: true }
  });
  assert.ok(html.includes('进度不可查'));
  assert.ok(html.includes('jb111111…1111'));
  assert.ok(!html.includes('bar-fill'));
  assert.ok(R.renderRegenResult({ scope: 'FULL', taskKinds: [], job: { jobId: 'j', created: false } })
    .includes('幂等命中'));
});

/* ============================ 降级 ============================ */

test('renderSelfCheckGrid: 8 类词表保留、结果全部「—」且标「无数据源」', () => {
  const html = R.renderSelfCheckGrid(PREVIEW);
  R.CATEGORIES.forEach((c) => assert.ok(html.includes('>' + c + '</span>'), c));
  assert.ok(html.includes('无数据源'));
  assert.ok(html.includes('恒为 null'));
  assert.ok(html.includes('WP30'));
  /* 原型编造的那组数字一个都不能出现 */
  assert.ok(!html.includes('71%'));
  assert.ok(!html.includes('1440×1920'));
  assert.ok(!html.includes('禁用词检测 0 命中'));
  assert.equal((html.match(/无自检结果/g) || []).length, 8);
});

test('renderSelfCheckGrid: WP30 上线后真分支已就位（selfCheckSummary 非空）', () => {
  const html = R.renderSelfCheckGrid(Object.assign({}, PREVIEW, {
    selfCheckSummary: { FACT: { label: '1 阻断' }, TONE: { label: '通过' } }
  }));
  assert.ok(html.includes('1 阻断'));
  assert.ok(html.includes('通过'));
  assert.ok(html.includes('selfCheckSummary 非空'));
  assert.ok(!html.includes('无数据源'));
});

test('renderGateChecks: 4 条关卡 2 真 2 降级，降级的绝不显示「已满足」', () => {
  const html = R.renderGateChecks(FEEDBACK, CONTENTS);
  assert.ok(html.includes('无待处理阻断意见'));
  assert.ok(html.includes('全部意见完成处置'));
  assert.equal((html.match(/无数据源/g) || []).length, 3); // 2 个标签 + 说明里引用一次
  assert.ok(html.includes('显示已满足就是在替后端做承诺'));
  assert.ok(html.includes('没有「已通过 / 已淘汰」'));
  /* 后两条不能出现「已满足」标签 */
  const tail = html.split('每个方向至少 1 条通过')[1];
  assert.ok(!tail.includes('>已满足<'));
});

test('renderRegenScopeOptions: 受控 4 选项 + 提交被 modelProfileId 挡住的理由', () => {
  const html = R.renderRegenScopeOptions('TITLE');
  R.REGEN_SCOPES.forEach((s) => assert.ok(html.includes('data-regen-scope="' + s + '"'), s));
  assert.ok(html.includes('btn-primary" data-regen-scope="TITLE"'));
  assert.ok(html.includes('提交不了'));
  assert.ok(html.includes('modelProfileId'));
  assert.ok(html.includes('没有 api.py'));
  assert.ok(html.includes('TAGS 会被服务层折成 COPY'));
  assert.ok(html.includes('不画假进度条'));
});

test('renderUploadUnavailable / renderDirectionUnavailable: 都标「无数据源」且不画空表', () => {
  [R.renderUploadUnavailable(), R.renderDirectionUnavailable()].forEach((html) => {
    assert.ok(html.includes('无数据源'));
    assert.ok(!html.includes('<table'));
  });
  assert.ok(R.renderDirectionUnavailable().includes('scene'));
  assert.ok(R.renderDirectionUnavailable().includes('已删除'));
  assert.ok(R.renderUploadUnavailable().includes('4 步分片'));
});

test('renderReviewDoctrine: 教义卡带统一标记', () => {
  const html = R.renderReviewDoctrine();
  assert.ok(html.includes('标准文本 · 非系统数据'));
  assert.ok(html.includes('不允许仅点击'));
});

/* ============================ 空态 ============================ */

test('各渲染函数：空数组 / null / 非数组都落空态且不抛错', () => {
  [R.renderFeedbackList, R.renderContentsTable, R.renderMediaStrip].forEach((fn) => {
    [[], null, undefined, 'oops', [null, 42]].forEach((input) => {
      assert.ok(/还没有|没有/.test(fn(input)), fn.name + ' 空态缺失');
    });
  });
  assert.ok(R.renderPreviewPhone(null).includes('未获取到预览'));
  assert.ok(R.renderParagraphAnchors({}).includes('没有段落锚点'));
  assert.ok(R.renderAdvanceForm(null).includes('未选中意见'));
  assert.ok(R.renderMetaFields(null).includes('未获取到预览'));
});

test('空态 · 零意见时关卡显示「已满足」而不是「无数据源」（真的 0 条）', () => {
  assert.ok(R.renderBlockingGate([]).includes('已满足'));
  const t = R.tallyFeedback([]);
  assert.equal(t.total, 0);
  assert.equal(t.unresolvedBlocking, 0);
});

test('空态 · preview 的 paragraphs / tags / mediaItems 为空数组时各有专门文案', () => {
  const html = R.renderPreviewPhone(Object.assign({}, PREVIEW, {
    paragraphs: [], tags: [], mediaItems: []
  }));
  assert.ok(html.includes('paragraphs 为空数组'));
  assert.ok(html.includes('（无标签）'));
  assert.ok(html.includes('mediaItems 为空数组'));
});

test('空态 · renderEmpty / renderLoading 文案可定制', () => {
  assert.ok(R.renderEmpty('空').includes('空'));
  assert.ok(R.renderEmpty().includes('没有可展示的内容。'));
  assert.ok(R.renderLoading().includes('正在读取内容与意见…'));
});

/* ============================ 错误 ============================ */

test('renderError: 412 / 428 / 409 三条错误路径各渲染专门文案而非通用错误', () => {
  const c412 = R.renderError({ code: 'VERSION_CONFLICT', status: 412, details: { currentEtag: '"x:9"' } });
  assert.ok(c412.includes('内容已被别人改过'));
  assert.ok(c412.includes('"x:9"') || c412.includes('&quot;x:9&quot;'));
  const c428 = R.renderError({ code: 'PRECONDITION_REQUIRED', status: 428, details: {} });
  assert.ok(c428.includes('缺少并发前置条件'));
  const c409 = R.renderError({
    code: 'INVALID_TRANSITION', status: 409,
    details: { reason: 'UNRESOLVED_BLOCKING_FEEDBACK', blockingFeedbackIds: ['f1'] }
  });
  assert.ok(c409.includes('存在未解决的阻断意见，无法标记已通过'));
  assert.ok(c409.includes('data-action="retry"'));
});

test('renderError: 错误信息里的 HTML 被转义（防注入）', () => {
  const html = R.renderError({ message: '<script>alert(1)</script>' });
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});

test('renderError: details 里的注入也被转义（currentEtag / blockingFeedbackIds）', () => {
  const html = R.renderError({
    code: 'VERSION_CONFLICT', status: 412,
    details: { currentEtag: '"><img src=x onerror=alert(1)>' }
  });
  assert.ok(!html.includes('"><img'));
});

/* ============================ 边界 ============================ */

test('边界 · STALE 必须展示、条数不减（R5）', () => {
  const html = R.renderFeedbackList(FEEDBACK, {});
  assert.ok(html.includes('锚点已失效 STALE'));
  assert.equal((html.match(/class="fb-row"/g) || []).length, 4);
  assert.ok(html.includes('STALE 只打标记不过滤'));
});

test('边界 · 未知枚举值降级为中性 tag，不映射成已有值', () => {
  const html = R.renderFeedbackList([fb({
    category: 'MYSTERY', severity: 'CRITICAL', state: 'FROZEN',
    anchorKind: 'SECTION', scope: 'FOREVER'
  })], {});
  ['MYSTERY', 'CRITICAL', 'FROZEN', 'SECTION', 'FOREVER'].forEach((v) => {
    assert.ok(html.includes(v), v);
  });
  assert.ok(!html.includes('>事实<') && !html.includes('>阻断<') && !html.includes('>待处理<'));
});

test('边界 · 未知 content state 不被映射成四态之一', () => {
  const html = R.renderContentsTable([{ contentId: 'x', state: 'ZOMBIE', title: 'T' }]);
  assert.ok(html.includes('ZOMBIE'));
  assert.ok(!html.includes('>已生成<'));
});

test('边界 · 超长 id / 正文截断显示，全量进 title', () => {
  const longId = 'x'.repeat(200);
  const html = R.renderFeedbackList([fb({ id: longId, body: 'y'.repeat(900) })], {});
  assert.ok(html.includes('y'.repeat(220) + '…'));
  assert.ok(!html.includes('y'.repeat(260)));
  assert.ok(html.includes('xxxxxxxx…xxxx'));
});

test('边界 · 各字段里的 HTML 特殊字符被转义', () => {
  const inj = '"><img src=x onerror=alert(1)>';
  assert.ok(!R.renderPreviewPhone(Object.assign({}, PREVIEW, { title: inj })).includes('"><img'));
  assert.ok(!R.renderFeedbackList([fb({ body: inj })], {}).includes('"><img'));
  assert.ok(!R.renderContentsTable([{ contentId: 'x', title: inj, state: 'DRAFT' }]).includes('"><img'));
  assert.ok(!R.renderParagraphAnchors({ paragraphs: [{ index: 0, text: inj, ref: inj }] }).includes('"><img'));
});

test('边界 · 意见取满 100 条时提示还有更多', () => {
  const many = Array.from({ length: 100 }, (_, i) => fb({ id: 'f' + i }));
  assert.ok(R.renderFeedbackList(many, {}).includes('已取满 limit=100'));
  assert.ok(!R.renderFeedbackList(many.slice(0, 99), {}).includes('已取满 limit=100'));
  assert.ok(R.renderContentsTable(Array.from({ length: 100 }, (_, i) => ({
    contentId: 'c' + i, state: 'DRAFT'
  }))).includes('已取满 limit=100'));
});

test('边界 · REJECTED 但 rejectReason 为空 → 标为异常而不是静默留白', () => {
  const html = R.renderFeedbackList([fb({ state: 'REJECTED', rejectReason: null })], {});
  assert.ok(html.includes('后端保证 REJECTED 必有理由，此处属异常'));
});

test('边界 · 缺 lockVersion 时推进表单显示「无法构造 ETag」而不是拼假值', () => {
  const html = R.renderAdvanceForm(fb({ lockVersion: null }));
  assert.ok(html.includes('缺 lockVersion，无法构造'));
  assert.equal(R.advanceRequest(fb({ lockVersion: null }), 'OPEN').ifMatch, '');
});

test('边界 · mediaItems / paragraphs 乱序输入仍按 sortOrder / index 升序渲染', () => {
  const html = R.renderPreviewPhone(Object.assign({}, PREVIEW, {
    paragraphs: [PREVIEW.paragraphs[1], PREVIEW.paragraphs[0]],
    mediaItems: [PREVIEW.mediaItems[1], PREVIEW.mediaItems[0]]
  }));
  assert.ok(html.indexOf('p1-aaaa1111') < html.indexOf('p2-bbbb2222'));
  assert.ok(html.indexOf('>#0 ') < html.indexOf('>#1 '));
});

/* ---------------------------- 纯度（机械证明） ---------------------------- */

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'content-feedback.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

test('模块源码不含 Date.now() / new Date() / Intl（纯度 + 单测稳定性）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'content-feedback.js'), 'utf8');
  assert.equal(/Date\.now\(\)|new Date\(|Intl\./.test(src), false);
});

/* ---------------------------- 汇总 ---------------------------- */

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) {
  process.exitCode = 1;
}
