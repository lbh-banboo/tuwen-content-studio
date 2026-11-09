/* render/knowledge.test.js —— knowledge 页渲染层（方案 A 纯函数）Node 单测
 * 运行：node render/knowledge.test.js   （零依赖，退出码 0=全绿 1=有失败）
 *
 * fixture 形状逐条对齐后端 pydantic 模型与 service 构造的嵌套 dict
 * （**不以 contracts/openapi.json 为准** —— tools/contracts/export_openapi.py:59
 *  _strip_titles() 会删掉所有名为 title 的响应字段，AssetSummaryBody.title /
 *  AssetDetailBody.title / SearchHitBody.assetTitle 相关的 title 在快照里缺失，
 *  运行时真有，见 knowledge/api.py:185、:210）：
 *   AssetSummaryBody   knowledge/api.py:178-193
 *   AssetDetailBody    knowledge/api.py:196-222（version/versions/publicationHistory 为 dict）
 *     version    → api.py:864 _version_detail（**带 body**）
 *     versions[] → api.py:881 _version_view（**无 versionNo**）
 *     publicationHistory[] → service.py:503 _publication_view
 *   VersionItemBody    knowledge/api.py:259-267
 *   ReferenceItemBody  knowledge/api.py:270-281
 *   ProposalItemBody   knowledge/api.py:284-296
 *   SearchResultBody   knowledge/search_api.py:230-236（hits + search）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./knowledge.js');

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

const A1 = 'aa111111-1111-4111-8111-111111111111';
const A2 = 'aa222222-2222-4222-8222-222222222222';
const KV1 = 'kv111111-1111-4111-8111-111111111111';
const KV2 = 'kv222222-2222-4222-8222-222222222222';

const ASSETS = [
  {
    id: A1, category: 'FACT', title: '氨基酸表活占比口径', scope: 'PRODUCT',
    brandId: 'br111111-1111-4111-8111-111111111111',
    productId: 'pd111111-1111-4111-8111-111111111111',
    projectId: null, activeVersionId: KV2, state: 'ACTIVE',
    archivedAt: null, createdAt: '2026-09-20T10:00:00+08:00'
  },
  {
    id: A2, category: 'FEEDBACK_EXPERIMENT', title: '疑问句标题的收藏率证据', scope: 'BRAND',
    brandId: 'br111111-1111-4111-8111-111111111111',
    productId: null, projectId: null, activeVersionId: null, state: 'DRAFT',
    archivedAt: null, createdAt: '2026-10-01T09:00:00+08:00'
  },
  {
    id: 'aa333333-3333-4333-8333-333333333333', category: 'RULE',
    title: '禁用词清单', scope: 'GLOBAL', brandId: null, productId: null, projectId: null,
    activeVersionId: 'kv333333-3333-4333-8333-333333333333', state: 'ARCHIVED',
    archivedAt: '2026-10-10T12:00:00+08:00', createdAt: '2026-08-01T08:00:00+08:00'
  }
];

const DETAIL = {
  assetId: A1, category: 'FACT', title: '氨基酸表活占比口径', scope: 'PRODUCT',
  brandId: 'br111111-1111-4111-8111-111111111111',
  productId: 'pd111111-1111-4111-8111-111111111111',
  projectId: null, state: 'ACTIVE', archivedAt: null, activeVersionId: KV2,
  version: {
    versionId: KV2,
    body: '氨基酸表活占比以配方总表活量为分母计算，口径须与检测报告一致。',
    structuredPayload: { unit: 'percent', basis: 'total_surfactant' },
    sourceSegmentIds: ['ss111111-1111-4111-8111-111111111111',
      'ss222222-2222-4222-8222-222222222222'],
    changeReason: '按第三方检测报告更正分母口径',
    effectiveFrom: '2026-10-01T00:00:00+08:00',
    effectiveUntil: null,
    createdAt: '2026-10-01T00:00:00+08:00'
  },
  versions: [
    { versionId: KV2, versionState: 'ACTIVE', changeReason: '按第三方检测报告更正分母口径',
      createdAt: '2026-10-01T00:00:00+08:00' },
    { versionId: KV1, versionState: 'SUPERSEDED', changeReason: '首次入库',
      createdAt: '2026-09-20T10:00:00+08:00' }
  ],
  publicationHistory: [
    { id: 'ph111111-1111-4111-8111-111111111111', versionId: KV1, action: 'ACTIVATE',
      sequence: 1, actorId: 'u1111111-1111-4111-8111-111111111111', reason: '首次发布',
      createdAt: '2026-09-20T10:05:00+08:00' },
    { id: 'ph222222-2222-4222-8222-222222222222', versionId: KV2, action: 'SUPERSEDE',
      sequence: 2, actorId: 'u1111111-1111-4111-8111-111111111111', reason: '口径更正',
      createdAt: '2026-10-01T00:05:00+08:00' }
  ],
  activeChunkCount: 7
};

const REFERENCES = [
  { parentVersionId: 'cv111111-1111-4111-8111-111111111111', purpose: 'BASELINE_CLAIM',
    projectId: '00000000-0000-4000-8000-000000000001',
    selectedFragmentHash: 'f1e2d3c4b5a69788' },
  { parentVersionId: 'cv222222-2222-4222-8222-222222222222', purpose: 'CONTENT_GENERATION',
    projectId: null, selectedFragmentHash: null }
];

const PROPOSALS = [
  { id: 'pp111111-1111-4111-8111-111111111111', state: 'PENDING', scope: 'PRODUCT',
    projectId: '00000000-0000-4000-8000-000000000001', targetAssetId: A1,
    proposedPayload: { field: 'body', to: '分母改为总表活量' },
    confirmedBy: null, createdAt: '2026-10-17T11:00:00+08:00' },
  { id: 'pp222222-2222-4222-8222-222222222222', state: 'ACCEPTED', scope: 'BRAND',
    projectId: null, targetAssetId: null,
    proposedPayload: { field: 'title', to: '新建：卸妆二次清洁场景' },
    confirmedBy: 'u1111111-1111-4111-8111-111111111111',
    createdAt: '2026-10-16T15:00:00+08:00' }
];

const SEARCH = {
  hits: [
    { chunkId: 'ck111111-1111-4111-8111-111111111111', chunkNo: 0, assetId: A1,
      assetTitle: '氨基酸表活占比口径', versionId: KV2, category: 'FACT', scope: 'PRODUCT',
      text: '氨基酸表活占比以配方总表活量为分母计算。', similarity: 0.8731, distance: 0.1269 },
    { chunkId: 'ck222222-2222-4222-8222-222222222222', chunkNo: 3, assetId: A2,
      assetTitle: '疑问句标题的收藏率证据', versionId: KV1,
      category: 'FEEDBACK_EXPERIMENT', scope: 'BRAND',
      text: '疑问句标题在样例中收藏率略高，但样本量不足。', similarity: 0.5102, distance: 0.4898 }
  ],
  search: {
    profileId: 'ep111111-1111-4111-8111-111111111111', profileName: 'default-zh-v1',
    mode: 'SEMANTIC',
    subject: { brandId: null, productId: null, projectId: '00000000-0000-4000-8000-000000000001' },
    scopeFilterApplied: true, scopeNote: '已按项目可用范围过滤，全局与本品牌资产可见。',
    scanned: 65, limit: 10, indexReady: true, chunkCountForProfile: 412
  }
};

/* ============================ 正常 ============================ */

test('renderCategoryFilter: 6 个受控类别 + 全部，选项来自后端取值域', () => {
  const html = R.renderCategoryFilter('');
  R.CATEGORIES.forEach((c) => assert.ok(html.includes('data-value="' + c + '"'), c));
  assert.equal(R.CATEGORIES.length, 6);
  assert.ok(html.includes('全部 6 类'));
  /* 原型的六域名字不得出现在筛选器里 */
  assert.ok(!html.includes('采集溯源'));
});

test('renderScopeFilter / renderStateFilter: 4 范围 / 5 状态且 active 高亮', () => {
  const scope = R.renderScopeFilter('BRAND');
  assert.ok(scope.includes('btn-primary" data-filter="scope" data-value="BRAND"'));
  const state = R.renderStateFilter('INDEXING');
  R.ASSET_STATES.forEach((s) => assert.ok(state.includes('data-value="' + s + '"'), s));
  assert.ok(state.includes('索引处理中'));
});

test('renderFilters: 明说 INDEXING ≠ 原型的「待确认」（降级 D3）', () => {
  const html = R.renderFilters({});
  assert.ok(html.includes('「待确认」不是后端的状态'));
  assert.ok(html.includes('索引处理中'));
  assert.ok(html.includes('知识提案的 PENDING'));
  assert.ok(html.includes('includeArchived'));
});

test('renderAssetTable: 6 列全部真字段，归档资产带归档时间标签', () => {
  const html = R.renderAssetTable(ASSETS, A1);
  assert.ok(html.includes('氨基酸表活占比口径'));
  assert.ok(html.includes('事实') && html.includes('反馈实验') && html.includes('规则'));
  assert.ok(html.includes('产品') && html.includes('品牌') && html.includes('全局'));
  assert.ok(html.includes('生效') && html.includes('草稿') && html.includes('已归档'));
  assert.ok(html.includes('已归档于 2026-10-10 12:00'));
  assert.ok(html.includes('从未发布'));          // activeVersionId 为 null
  assert.ok(html.includes('<tr class="tr-sel"')); // 选中行
});

test('renderCategoryTally: 真实 6 类计数 + 与六域卡的区别写进卡里', () => {
  const html = R.renderCategoryTally(ASSETS);
  assert.ok(html.includes('知识类别分布（真实计数）'));
  assert.ok(html.includes('1 条'));
  assert.ok(html.includes('与上面的「六域 180 字段」是两回事'));
  assert.ok(html.includes('纯属巧合'));
});

test('renderAssetDetail: 元信息 + version 块（含 body）+ chunk 数', () => {
  const html = R.renderAssetDetail(DETAIL);
  assert.ok(html.includes('氨基酸表活占比口径'));
  assert.ok(html.includes('以配方总表活量为分母'));   // version.body 真渲染
  assert.ok(html.includes('来源片段 2 个'));
  assert.ok(html.includes('按第三方检测报告更正分母口径'));
  assert.ok(html.includes('生效自 2026-10-01 00:00'));
  assert.ok(html.includes('>7<'));                     // activeChunkCount
  assert.ok(html.includes('知识侧可按 versionId 回看正文'));
});

test('renderVersionHistory: 无 versionNo 的事实被写明，不自造序号', () => {
  const html = R.renderVersionHistory(DETAIL.versions);
  assert.ok(html.includes('没有 versionNo 字段'));
  assert.ok(html.includes('不得自造序号'));
  assert.ok(html.includes('被替代'));
  assert.ok(!/>v1</.test(html) && !/>v2</.test(html));
});

test('renderPublicationHistory: 按 sequence 升序 + 动作中文 + 归档不删版本', () => {
  const html = R.renderPublicationHistory(DETAIL.publicationHistory);
  const p1 = html.indexOf('#1'), p2 = html.indexOf('#2');
  assert.ok(p1 > -1 && p2 > p1);
  assert.ok(html.includes('激活') && html.includes('替代'));
  assert.ok(html.includes('归档不删版本、不改旧版本'));
});

test('renderReferences: 4 列真字段 + 明说只给摘要且按权限过滤', () => {
  const html = R.renderReferences(REFERENCES);
  assert.ok(html.includes('BASELINE_CLAIM'));
  assert.ok(html.includes('f1e2d3c4b5a6'));
  assert.ok(html.includes('整版引用'));   // selectedFragmentHash 为 null
  assert.ok(html.includes('无项目'));     // projectId 为 null
  assert.ok(html.includes('不能借此读无权项目正文'));
  assert.ok(html.includes('条数少不等于引用少'));
});

test('renderProposalList: PENDING 才有采纳/驳回按钮，已处置的没有', () => {
  const html = R.renderProposalList(PROPOSALS);
  assert.ok(html.includes('待确认 1 条'));
  assert.ok(html.includes('data-action="accept-proposal" data-proposal-id="pp111111-1111-4111-8111-111111111111"'));
  assert.ok(html.includes('data-action="reject-proposal"'));
  assert.ok(html.includes('已处置'));
  assert.ok(!html.includes('data-proposal-id="pp222222-2222-4222-8222-222222222222"'));
  assert.ok(html.includes('三段式回写'));
});

test('renderSearchResults: hits 渲染相似度 / 片段号 / 资产标题', () => {
  const html = R.renderSearchResults(SEARCH);
  assert.ok(html.includes('2 条命中'));
  assert.ok(html.includes('相似度 0.8731'));
  assert.ok(html.includes('距离 0.1269'));
  assert.ok(html.includes('片段 #0') && html.includes('片段 #3'));
  assert.ok(html.includes('疑问句标题的收藏率证据'));
});

test('renderSearchMeta: 真实 meta 字段 + 「前置剔除无计数字段」写明（降级 D5）', () => {
  const html = R.renderSearchMeta(SEARCH.search);
  assert.ok(html.includes('default-zh-v1'));
  assert.ok(html.includes('>65<'));   // scanned
  assert.ok(html.includes('>412<')); // chunkCountForProfile
  assert.ok(html.includes('已按项目可用范围过滤'));
  assert.ok(html.includes('没有计数'));
  assert.ok(html.includes('无法还原'));
});

test('renderSearchForm: mode 只给两个受控值，profileId 不传', () => {
  const html = R.renderSearchForm({ mode: 'KEYWORD' });
  assert.ok(html.includes('data-value="SEMANTIC"'));
  assert.ok(html.includes('data-value="KEYWORD"'));
  assert.ok(html.includes('btn-primary" data-filter="mode" data-value="KEYWORD"'));
  assert.ok(html.includes('用服务端默认档案'));
  assert.equal(R.SEARCH_MODES.length, 2);
});

/* ============================ 请求描述符（可单测） ============================ */

test('searchRequest: subject 必填、mode 默认 SEMANTIC、可选字段按需加', () => {
  assert.deepEqual(R.searchRequest({ query: '氨基酸' }), {
    method: 'POST', path: '/knowledge/searches',
    body: { query: '氨基酸', subject: { brandId: null, productId: null, projectId: null }, mode: 'SEMANTIC' }
  });
  const full = R.searchRequest({
    query: '标题', mode: 'KEYWORD', category: 'RULE', limit: 5,
    projectId: 'p1', profileId: 'ep1'
  });
  assert.deepEqual(full.body, {
    query: '标题', subject: { brandId: null, productId: null, projectId: 'p1' },
    mode: 'KEYWORD', category: 'RULE', limit: 5, profileId: 'ep1'
  });
});

test('searchRequest: 取值域外的 mode 被收敛成默认 SEMANTIC，不原样发出', () => {
  assert.equal(R.searchRequest({ query: 'x', mode: 'FUZZY' }).body.mode, 'SEMANTIC');
  assert.equal(R.searchRequest(null).body.mode, 'SEMANTIC');
  assert.equal(R.searchRequest(null).body.query, '');
});

test('decisionRequest: decision 只能是 ACCEPT / REJECT，路径带 encode', () => {
  assert.deepEqual(R.decisionRequest('pp1', 'ACCEPT', '口径已核对'), {
    method: 'POST', path: '/knowledge/proposals/pp1/decisions',
    body: { decision: 'ACCEPT', reason: '口径已核对' }
  });
  assert.equal(R.decisionRequest('pp1', 'accept').body.decision, 'ACCEPT');
  assert.equal(R.decisionRequest('pp1', 'MAYBE').body.decision, 'REJECT'); // 非法值收敛为 REJECT
  assert.ok(R.decisionRequest('a/b', 'REJECT').path.includes('a%2Fb'));
});

test('renderDecisionResult: 回显 state / createdVersionId / activated', () => {
  const html = R.renderDecisionResult({
    proposalId: 'pp111111-1111-4111-8111-111111111111', state: 'ACCEPTED',
    createdVersionId: KV2, activated: true
  });
  assert.ok(html.includes('已采纳'));
  assert.ok(html.includes('已激活为生效版本'));
  const html2 = R.renderDecisionResult({ proposalId: 'x', state: 'REJECTED', createdVersionId: null, activated: false });
  assert.ok(html2.includes('已驳回') && html2.includes('未新建版本'));
  assert.equal(R.renderDecisionResult(null), '');
});

/* ============================ 教义区（二分法的另一半） ============================ */

test('教义卡全部带「标准文本 · 非系统数据」统一标记', () => {
  [R.renderSixDomainDoctrine, R.renderOntologyDoctrine,
    R.renderSopDoctrine, R.renderStrategyStateDoctrine].forEach((fn) => {
    assert.ok(fn().includes('<span class="tag tag-muted no-dot f10">标准文本 · 非系统数据</span>'),
      fn.name + ' 缺统一标记');
  });
});

test('renderSixDomainDoctrine: 6 个卡保持「字段数」语义，且禁止改接 asset 计数', () => {
  const html = R.renderSixDomainDoctrine();
  assert.ok(html.includes('45<span class="unit"> 字段</span>'));
  assert.ok(html.includes('标准定义的字段数'));
  assert.ok(html.includes('不是「库里有多少条资产」'));
  assert.ok(html.includes('刻意**不**改接成 asset 计数') ||
    html.includes('刻意') && html.includes('改接成 asset 计数'));
  assert.ok(html.includes('180 个字段在库里零对应'));
  assert.ok(!html.includes('条资产</')); // 不出现资产计数单位
});

test('renderOntologyDoctrine: C1–C7 与 K1–K8 齐全，并指出 K4/K6/K8 的真实落点', () => {
  const html = R.renderOntologyDoctrine();
  ['C1', 'C7', 'K1', 'K8'].forEach((k) => assert.ok(html.includes(k), k));
  assert.ok(html.includes('零对应'));
  assert.ok(html.includes('INCONCLUSIVE'));
  assert.ok(html.includes('缺失填 null 而不是 0'));
});

test('renderSopDoctrine: 七步 / F1–F6 / A-D 四级 + 说明后端无字段', () => {
  const html = R.renderSopDoctrine();
  assert.ok(html.includes('归集') && html.includes('入库版本化'));
  ['F1', 'F6'].forEach((k) => assert.ok(html.includes(k), k));
  assert.ok(html.includes('在后端都没有字段'));
  assert.ok(html.includes('只有三级、且只在草案里'));
});

test('renderStrategyStateDoctrine: 明说六态不存在、草案只有 4 态、两套状态不混用', () => {
  const html = R.renderStrategyStateDoctrine();
  assert.ok(html.includes('这六个状态在后端不存在'));
  assert.ok(html.includes('只有 **4 态**') || html.includes('4 态'));
  assert.ok(html.includes('CANDIDATE / SUPPORTED / UNSUPPORTED / INCONCLUSIVE'));
  assert.ok(html.includes('两套'));
});

test('renderCorrectionDoctrine: 口径更正清单与通过率趋势都标「无数据源」', () => {
  const html = R.renderCorrectionDoctrine();
  assert.ok(html.includes('无数据源'));
  assert.ok(html.includes('没有任何 GET'));
  assert.ok(html.includes('已全部删除'));
  /* 原型里写死的百分比不得出现 */
  assert.ok(!/\d+(\.\d+)?%/.test(html));
});

/* ============================ 空态 ============================ */

test('各数据区：空数组 / null / 非数组都落空态且不抛错', () => {
  [R.renderAssetTable, R.renderVersionHistory, R.renderPublicationHistory,
    R.renderReferences, R.renderProposalList].forEach((fn) => {
    [[], null, undefined, 'oops', [null, 42]].forEach((input) => {
      const html = fn(input);
      assert.ok(/没有|还没有/.test(html), fn.name + ' 空态缺失');
    });
  });
});

test('renderAssetDetail: null 落「未选中资产」，不抛错', () => {
  assert.ok(R.renderAssetDetail(null).includes('未选中资产'));
  assert.doesNotThrow(() => R.renderAssetDetail('oops'));
});

test('renderSearchResults: 未检索 / 0 命中两种空态区分开', () => {
  assert.ok(R.renderSearchResults(null).includes('还没有检索'));
  const zero = R.renderSearchResults({ hits: [], search: SEARCH.search });
  assert.ok(zero.includes('没有命中任何知识片段'));
  assert.ok(!zero.includes('还没有检索')); // 检索过但 0 命中 ≠ 没检索过
});

test('renderCategoryTally: 空集不画 0 条柱，显式说明理由', () => {
  const html = R.renderCategoryTally([]);
  assert.ok(html.includes('无分布可算'));
  assert.ok(html.includes('0 会被读成'));
  assert.ok(!html.includes('width:0.0%'));
});

test('renderSearchMeta: null 返回空串（无 meta 不渲染空卡）', () => {
  assert.equal(R.renderSearchMeta(null), '');
  assert.equal(R.renderSearchMeta(undefined), '');
});

/* ============================ 错误 ============================ */

test('renderError: 离线错误展示 code / message / 重试按钮', () => {
  const html = R.renderError({
    code: 'DEPENDENCY_UNAVAILABLE', message: '连不上后端（network_error）。', offline: true
  });
  assert.ok(html.includes('DEPENDENCY_UNAVAILABLE'));
  assert.ok(html.includes('data-action="retry"'));
});

test('renderError: PERMISSION_DENIED 指出需要 KNOWLEDGE_MANAGE 权限', () => {
  const html = R.renderError({ code: 'PERMISSION_DENIED', message: '无权操作。' });
  assert.ok(html.includes('KNOWLEDGE_MANAGE'));
});

test('renderError: 可重试 / 不可重试 / 裸串 / null 四个分支', () => {
  assert.ok(R.renderError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderError({ code: 'RESOURCE_NOT_FOUND', retryable: false }).includes('请按错误码排查'));
  assert.ok(R.renderError('裸字符串错误').includes('裸字符串错误'));
  assert.ok(R.renderError(null).includes('读取知识库失败'));
});

test('renderError: 错误信息里的 HTML 被转义（防注入）', () => {
  const html = R.renderError({ message: '<script>alert(1)</script>' });
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});

/* ============================ 边界 ============================ */

test('边界 · indexReady=false 必须显式警示，0 命中不得被读成「库里没有」', () => {
  const meta = Object.assign({}, SEARCH.search, { indexReady: false });
  assert.ok(R.renderSearchMeta(meta).includes('索引处理中（indexReady=false）'));
  assert.ok(R.renderSearchMeta(meta).includes('结果**不完整') ||
    R.renderSearchMeta(meta).includes('不完整'));
  const zero = R.renderSearchResults({ hits: [], search: meta });
  assert.ok(zero.includes('不代表「库里没有」'));
});

test('边界 · activeChunkCount===0 标为「索引尚未处理」而不是「没有内容」', () => {
  const html = R.renderAssetDetail(Object.assign({}, DETAIL, { activeChunkCount: 0 }));
  assert.ok(html.includes('activeChunkCount = 0'));
  assert.ok(html.includes('索引尚未处理'));
  assert.ok(html.includes('不是「这条资产没有内容」'));
});

test('边界 · version 为 null（从未发布）→ 明确说明而不是空白', () => {
  const html = R.renderAssetDetail(Object.assign({}, DETAIL, { version: null }));
  assert.ok(html.includes('从未发布过版本'));
});

test('边界 · 未知 category / scope / state / versionState 降级为中性 tag', () => {
  const html = R.renderAssetTable([{
    id: 'x', category: 'MYSTERY', scope: 'GALAXY', state: 'FROZEN',
    title: 'T', activeVersionId: null, archivedAt: null, createdAt: null
  }]);
  assert.ok(html.includes('MYSTERY') && html.includes('GALAXY') && html.includes('FROZEN'));
  assert.ok(!html.includes('>事实<') && !html.includes('>生效<'));
  const vh = R.renderVersionHistory([{ versionId: 'v', versionState: 'ZOMBIE', changeReason: null, createdAt: null }]);
  assert.ok(vh.includes('ZOMBIE'));
});

test('边界 · 筛选器里取值域外的 active 值保留并标「取值域外」', () => {
  const html = R.renderCategoryFilter('MYSTERY');
  assert.ok(html.includes('MYSTERY'));
  assert.ok(html.includes('（取值域外）'));
});

test('边界 · 受控取值域外的 category 在分布里排在已知之后并标出', () => {
  const html = R.renderCategoryTally([{ id: 'x', category: 'MYSTERY' }, { id: 'y', category: 'FACT' }]);
  assert.ok(html.indexOf('事实') < html.indexOf('MYSTERY'));
  assert.ok(html.includes('（取值域外）'));
});

test('边界 · 超长 id / 超长正文截断显示，全量进 title', () => {
  const longId = 'x'.repeat(200);
  const html = R.renderAssetTable([{
    id: longId, category: 'FACT', scope: 'GLOBAL', state: 'ACTIVE',
    title: 'y'.repeat(200), activeVersionId: longId, archivedAt: null, createdAt: null
  }]);
  assert.ok(html.includes('title="' + 'y'.repeat(200) + '"'));
  assert.ok(html.includes('y'.repeat(46) + '…'));
  assert.ok(html.includes('xxxxxxxx…xxxx'));
  const detail = R.renderAssetDetail(Object.assign({}, DETAIL, {
    version: Object.assign({}, DETAIL.version, { body: 'z'.repeat(2000) })
  }));
  assert.ok(detail.includes('z'.repeat(1200) + '…'));
  assert.ok(!detail.includes('z'.repeat(1400)));
});

test('边界 · 字段里的 HTML 特殊字符被转义（title / body / scopeNote / payload）', () => {
  const inj = '"><img src=x onerror=alert(1)>';
  assert.ok(!R.renderAssetTable([{ id: 'x', title: inj, category: 'FACT', scope: 'GLOBAL', state: 'ACTIVE' }]).includes('"><img'));
  assert.ok(!R.renderAssetDetail(Object.assign({}, DETAIL, {
    version: Object.assign({}, DETAIL.version, { body: inj })
  })).includes('"><img'));
  assert.ok(!R.renderSearchMeta(Object.assign({}, SEARCH.search, { scopeNote: inj })).includes('"><img'));
  assert.ok(!R.renderProposalList([Object.assign({}, PROPOSALS[0], {
    proposedPayload: { to: inj }
  })]).includes('"><img'));
});

test('边界 · similarity 非数值 / 缺失不抛错', () => {
  assert.equal(R.fmtSimilarity(null), '—');
  assert.equal(R.fmtSimilarity(''), '—');
  assert.equal(R.fmtSimilarity('abc'), 'abc');
  assert.equal(R.fmtSimilarity(0), '0.0000');  // 0 是真实值，不是缺失
});

test('边界 · 资产取满 100 条时提示还有更多', () => {
  const many = Array.from({ length: 100 }, (_, i) => ({
    id: 'a' + i, category: 'FACT', scope: 'GLOBAL', state: 'ACTIVE', title: 'T' + i
  }));
  assert.ok(R.renderAssetTable(many).includes('已取满 limit=100'));
  assert.ok(!R.renderAssetTable(many.slice(0, 99)).includes('已取满 limit=100'));
});

test('边界 · publicationHistory 的 sequence 乱序输入仍按升序渲染', () => {
  const html = R.renderPublicationHistory([
    DETAIL.publicationHistory[1], DETAIL.publicationHistory[0]
  ]);
  assert.ok(html.indexOf('#1') < html.indexOf('#2'));
});

/* ---------------------------- 纯度（机械证明） ---------------------------- */

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'knowledge.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

test('模块源码不含 Date.now() / new Date() / Intl（纯度 + 单测稳定性）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'knowledge.js'), 'utf8');
  assert.equal(/Date\.now\(\)|new Date\(|Intl\./.test(src), false);
});

/* ---------------------------- 汇总 ---------------------------- */

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) {
  process.exitCode = 1;
}
