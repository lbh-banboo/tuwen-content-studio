/* ============================================================================
 * render/knowledge.js —— knowledge 页渲染层（方案 A：数据 → HTML 字符串）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读 DOM、不读浏览器全局、不做 I/O、
 * 不取「现在几点」。因此可直接 Node 单测。
 *
 * 双环境导出：
 *   Node/CommonJS  → require('./knowledge.js')
 *   浏览器          → RednoteRender.knowledge
 *
 * ---------------------------------------------------------------------------
 * 本页的核心决策：二分法
 * ---------------------------------------------------------------------------
 * `knowledge.html`（1561 行 / 112KB，全站最大）本质是**一份方法论标准文档**，
 * 不是数据看板。整页约 70% 是教义文本（六域 180 字段 / 七类实体 C1–C7 /
 * SOP 七步四门 / 六条一票否决 F1–F6 / 八条推理禁令 K1–K8 / 策略六态 / 双 Loop）——
 * 这些在后端**零对应**。试图把它们全部接线，就必须为「180 字段」「K1–K8」
 * 编造数据源。
 * ⇒ 教义区**保持静态**，每个卡头统一加
 *   `<span class="tag tag-muted no-dot f10">标准文本 · 非系统数据</span>`；
 *   数据区（5 块）接真后端。
 *
 * ⚠️ 特别记一条，防后人「顺手优化」：
 *   D1–D6 那 6 个 metric 卡（内容 45 / 策略 35 / 效果 32 / 主体 30 / 质量风险 25 /
 *   采集溯源 15）是「**标准里定义了多少字段**」，**不是**「库里有多少条资产」。
 *   把它们改接成 asset 计数就是新造一处「声称与数据矛盾」：
 *   后端 CATEGORIES 只有 6 个（FACT / RESEARCH / SCENE / STYLE / RULE /
 *   FEEDBACK_EXPERIMENT），数量上与六域巧合都是 6，**语义毫不相干**。
 *   真实的 6 类分布请看 renderCategoryTally()，它是另一张卡、另一个标题。
 *
 * ---------------------------------------------------------------------------
 * 数据来源（读的是后端源码；**不以 contracts/openapi.json 为准** ——
 * tools/contracts/export_openapi.py:59 _strip_titles() 会删掉所有名为 title 的
 * 响应字段，AssetSummaryBody.title / AssetDetailBody.title 在快照里缺失，运行时真有）：
 *
 *   GET /knowledge/assets                  knowledge/api.py:315
 *     筛选 category / brandId / productId / projectId / state / query /
 *          includeArchived / limit≤100 / cursor      api.py:325-331
 *     → PaginatedEnvelope[AssetSummaryBody]          api.py:178-193
 *       { id, category, title, scope, brandId, productId, projectId,
 *         activeVersionId, state, archivedAt, createdAt }
 *
 *   GET /knowledge/assets/{assetId}?versionId=        api.py:426（versionId 可选，api.py:435）
 *     → AssetDetailBody                              api.py:196-222
 *       { assetId, category, title, scope, brandId, productId, projectId,
 *         state, archivedAt, activeVersionId, version, versions[],
 *         publicationHistory[], activeChunkCount }
 *       version（api.py:864 _version_detail）=
 *         { versionId, body, structuredPayload, sourceSegmentIds[], changeReason,
 *           effectiveFrom, effectiveUntil, createdAt }
 *         ⚠️ **带 body** —— 知识侧能按版本回看正文，内容侧（GET /contents/{id}）
 *            没有 versionId 参数、拿不到历史正文。这是真实差异，不是漏找。
 *       versions[]（api.py:881 _version_view）=
 *         { versionId, versionState, changeReason, createdAt }
 *         ⚠️ **没有 versionNo** —— 版本历史里不存在「v3」这种序号，
 *            只有 versionId + 投影出的 versionState。不得自造序号。
 *       publicationHistory[]（service.py:503 _publication_view）=
 *         { id, versionId, action, sequence, actorId, reason, createdAt }
 *       activeChunkCount：0 = 索引尚未处理（api.py:221 原文）
 *
 *   GET /knowledge/assets/{assetId}/versions          api.py:581
 *     → PaginatedEnvelope[VersionItemBody]            api.py:259-267
 *       { versionId, versionState, changeReason, createdAt }（同上，无 versionNo）
 *
 *   GET /knowledge/assets/{assetId}/references        api.py:618
 *     → PaginatedEnvelope[ReferenceItemBody]          api.py:270-281
 *       { parentVersionId, purpose, projectId, selectedFragmentHash }
 *       只返回**摘要**、且按权限过滤（service.py:51-56 原文：不能借此读无权项目正文）
 *
 *   GET /knowledge/proposals                          api.py:697（筛选 state / projectId）
 *     → PaginatedEnvelope[ProposalItemBody]           api.py:284-296
 *       { id, state, scope, projectId, targetAssetId, proposedPayload,
 *         confirmedBy, createdAt }
 *
 *   POST /knowledge/proposals/{proposalId}/decisions  api.py:740
 *     请求 DecisionBody { decision: ACCEPT|REJECT, scope?, reason? }   api.py:163-170
 *     → DecisionResultBody { proposalId, state, createdVersionId, activated }  api.py:299-307
 *
 *   POST /knowledge/searches                          knowledge/search_api.py:285
 *     请求 SearchBody { query(必填,≥1), subject{brandId,productId,projectId}(必填),
 *                       mode(默认 SEMANTIC), category?, limit?, profileId? }  search_api.py:155-175
 *     → SearchResultBody { hits[], search }            search_api.py:230-236
 *       SearchHitBody  { chunkId, chunkNo, assetId, assetTitle, versionId,
 *                        category, scope, text, similarity, distance }  search_api.py:183-197
 *       SearchMetaBody { profileId, profileName, mode, subject, scopeFilterApplied,
 *                        scopeNote, scanned, limit, indexReady,
 *                        chunkCountForProfile }                        search_api.py:200-227
 *
 * 受控取值域（knowledge/types.py + embedding_types.py，筛选器一律用这几组）：
 *   CATEGORIES      FACT RESEARCH SCENE STYLE RULE FEEDBACK_EXPERIMENT  types.py:65-70
 *   SCOPES          GLOBAL BRAND PRODUCT PROJECT                        types.py:105-108
 *   ASSET_STATES    DRAFT ACTIVE INDEXING SUPERSEDED ARCHIVED           types.py:215-220
 *   VERSION_STATES  DRAFT ACTIVE SUPERSEDED ARCHIVED                    types.py:192-195
 *   PROPOSAL_STATES PENDING ACCEPTED REJECTED                           types.py:252-254
 *   PROPOSAL_DECISIONS ACCEPT / REJECT                                  types.py:270-273
 *   PUBLICATION_ACTIONS ACTIVATE ARCHIVE RESTORE SUPERSEDE              types.py:147-150
 *   SEARCH_MODES    SEMANTIC / KEYWORD（默认 SEMANTIC）        embedding_types.py:207-215
 *
 * ---------------------------------------------------------------------------
 * 降级登记（每条写明理由）：
 *   D1 六域 180 字段（D1–D6 六个 metric 卡）—— 后端 CATEGORIES 只有 6 个常量，
 *      语义与六域毫不相干，180 字段在库里零对应 ⇒ 保持静态 + 标注。
 *      **不得**改接成 asset 计数（理由见上）。
 *   D2 七类实体 C1–C7、12 种具名关系、SOP 七步四门、F1–F6、K1–K8、
 *      证据等级 A/B/C/D、动态上下文组装六步 —— 后端零对应 ⇒ 静态 + 标注。
 *   D3 资产 5 状态里的「待确认」≠ INDEXING。页面自己也写了「资产状态与策略状态
 *      是两套」。INDEXING 是**索引处理中**（机器），不是**人工待确认** ⇒
 *      状态筛选器用后端的 5 个真值，中文标签如实译，不借用原型的「待确认」。
 *   D4 策略六态（观察信号→相关性线索→候选策略→实验中→已验证策略→失效归档）——
 *      正式契约零对应；m4 草案的 StrategyEvidence.state 只有 4 态
 *      （CANDIDATE / SUPPORTED / UNSUPPORTED / INCONCLUSIVE）且不在正式契约里
 *      ⇒ 静态 + 标注，并注明「草案只有 4 态」。
 *   D5 「44 条选用 / 65 条命中 / 23 条前置剔除」—— scanned（扫过的 chunk 数）
 *      与 hits.length（返回条数）是真的，但**「前置剔除 N 条」没有计数字段**
 *      （只有 scopeFilterApplied 布尔 + scopeNote 文本）⇒ 只渲染真有的那两个数，
 *      剔除数标「无计数字段」。
 *   D6 「7 处口径已更正」清单 / 「当前策略池分布」/「首次通过率趋势」——
 *      无端点 ⇒ 静态 + 标注（口径更正清单是文档自述）。
 *   D7 视图 A / 视图 B 两张表 —— 可用 category 近似划分
 *      （A ≈ FACT+RULE+STYLE，B ≈ FEEDBACK_EXPERIMENT），但「证据」「验证」
 *      「失效原因」三列**无字段** ⇒ 用真实资产表 + category 筛选替代，
 *      不渲染那三列。
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.knowledge = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  /* ---------------------------- 受控取值域 ---------------------------- */

  var CATEGORIES = ['FACT', 'RESEARCH', 'SCENE', 'STYLE', 'RULE', 'FEEDBACK_EXPERIMENT'];
  var CATEGORY_LABEL = {
    FACT: '事实', RESEARCH: '研究', SCENE: '场景',
    STYLE: '风格', RULE: '规则', FEEDBACK_EXPERIMENT: '反馈实验'
  };

  var SCOPES = ['GLOBAL', 'BRAND', 'PRODUCT', 'PROJECT'];
  var SCOPE_LABEL = { GLOBAL: '全局', BRAND: '品牌', PRODUCT: '产品', PROJECT: '项目' };

  /* 5 个真状态。注意：INDEXING 是「索引处理中」（机器），
     不是原型里那个「待确认」（人工）—— 两者不是同一件事，不混用（降级 D3）。 */
  var ASSET_STATES = ['DRAFT', 'ACTIVE', 'INDEXING', 'SUPERSEDED', 'ARCHIVED'];
  var ASSET_STATE_META = {
    DRAFT: { label: '草稿', cls: '' },
    ACTIVE: { label: '生效', cls: 'tag-ok' },
    INDEXING: { label: '索引处理中', cls: 'tag-warn' },
    SUPERSEDED: { label: '被替代', cls: 'tag-muted' },
    ARCHIVED: { label: '已归档', cls: 'tag-muted' }
  };

  var VERSION_STATE_META = {
    DRAFT: { label: '草稿', cls: '' },
    ACTIVE: { label: '生效', cls: 'tag-ok' },
    SUPERSEDED: { label: '被替代', cls: 'tag-muted' },
    ARCHIVED: { label: '已归档', cls: 'tag-muted' }
  };

  var PROPOSAL_STATES = ['PENDING', 'ACCEPTED', 'REJECTED'];
  var PROPOSAL_STATE_META = {
    PENDING: { label: '待确认', cls: 'tag-warn' },
    ACCEPTED: { label: '已采纳', cls: 'tag-ok' },
    REJECTED: { label: '已驳回', cls: 'tag-danger' }
  };

  var PUBLICATION_ACTION_LABEL = {
    ACTIVATE: '激活', ARCHIVE: '归档', RESTORE: '恢复', SUPERSEDE: '替代'
  };

  var SEARCH_MODES = ['SEMANTIC', 'KEYWORD'];
  var SEARCH_MODE_LABEL = { SEMANTIC: '语义检索', KEYWORD: '关键词检索' };

  /* ---------------------------- 基础工具 ---------------------------- */

  function text(value) {
    return value === null || value === undefined ? '' : String(value);
  }

  function escapeHtml(value) {
    return text(value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function truncate(value, max) {
    var s = text(value);
    return s.length > max ? s.slice(0, max) + '…' : s;
  }

  function shortId(value) {
    var s = text(value);
    if (!s) return '';
    return s.length > 16 ? s.slice(0, 8) + '…' + s.slice(-4) : s;
  }

  function fmtTime(value) {
    var s = text(value);
    var m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(s);
    return m ? m[1] + '-' + m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5] : s;
  }

  function rows(value) {
    return Array.isArray(value)
      ? value.filter(function (row) { return row && typeof row === 'object'; })
      : [];
  }

  function card(title, sub, body, extraHead) {
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">' + escapeHtml(title) + '</span>' +
      (sub ? '<span class="card-sub">' + escapeHtml(sub) + '</span>' : '') +
      (extraHead || '') +
      '</div><div class="card-body">' + body + '</div></section>';
  }

  /** 教义区统一标记。方案 §1 页 4 明确指定了这一行的写法，不要改。 */
  function renderDoctrineBadge() {
    return '<span class="tag tag-muted no-dot f10">标准文本 · 非系统数据</span>';
  }

  function noSourceBadge() {
    return '<span class="tag tag-muted no-dot f10">无数据源</span>';
  }

  function note(cls, strong, body) {
    return '<div class="note note-' + cls + '">' +
      '<i class="fa-solid fa-' + (cls === 'warn' ? 'triangle-exclamation' : 'circle-info') + '"></i>' +
      '<div>' + (strong ? '<b>' + escapeHtml(strong) + '</b> ' : '') + escapeHtml(body) + '</div></div>';
  }

  function emptyBlock(message, hint) {
    return '<div class="col" style="align-items:center;gap:6px;padding:20px 0;">' +
      '<i class="fa-solid fa-inbox t-fai"></i>' +
      '<span class="f12 t-mut">' + escapeHtml(message) + '</span>' +
      (hint ? '<span class="f10 t-fai">' + escapeHtml(hint) + '</span>' : '') +
      '</div>';
  }

  function tag(label, cls) {
    return '<span class="tag' + (cls ? ' ' + cls : '') + ' no-dot f10">' + escapeHtml(label) + '</span>';
  }

  /** 受控筛选器：选项一律来自后端取值域，不自创。未知的 active 值保留并标出。 */
  function filterChips(name, options, labels, active, allLabel) {
    var a = text(active);
    var chips = ['<button class="btn btn-sm ' + (a ? 'btn-ghost' : 'btn-primary') +
      '" data-filter="' + escapeHtml(name) + '" data-value="">' +
      escapeHtml(allLabel || '全部') + '</button>'];
    options.forEach(function (opt) {
      chips.push('<button class="btn btn-sm ' + (a === opt ? 'btn-primary' : 'btn-ghost') +
        '" data-filter="' + escapeHtml(name) + '" data-value="' + escapeHtml(opt) + '">' +
        escapeHtml((labels && labels[opt]) || opt) +
        ' <span class="mono f10 t-fai">' + escapeHtml(opt) + '</span></button>');
    });
    if (a && options.indexOf(a) === -1) {
      chips.push('<button class="btn btn-sm btn-primary" data-filter="' + escapeHtml(name) +
        '" data-value="' + escapeHtml(a) + '">' + escapeHtml(truncate(a, 24)) +
        ' <span class="f10 t-fai">（取值域外）</span></button>');
    }
    return '<div class="row" style="gap:6px;flex-wrap:wrap">' + chips.join('') + '</div>';
  }

  /* ---------------------------- 筛选器 ---------------------------- */

  function renderCategoryFilter(activeCategory) {
    return filterChips('category', CATEGORIES, CATEGORY_LABEL, activeCategory, '全部 6 类');
  }

  function renderScopeFilter(activeScope) {
    return filterChips('scope', SCOPES, SCOPE_LABEL, activeScope, '全部范围');
  }

  function renderStateFilter(activeState) {
    var labels = {};
    ASSET_STATES.forEach(function (s) { labels[s] = ASSET_STATE_META[s].label; });
    return filterChips('state', ASSET_STATES, labels, activeState, '全部状态');
  }

  function renderFilters(active) {
    var a = active && typeof active === 'object' ? active : {};
    return card('资产筛选', '选项全部取自后端受控取值域，前端不自创',
      '<div class="col" style="gap:10px">' +
      '<div class="col" style="gap:5px"><span class="f11 t-mut">知识类别 category（6 个）</span>' +
        renderCategoryFilter(a.category) + '</div>' +
      '<div class="col" style="gap:5px"><span class="f11 t-mut">可用范围 scope（4 个）</span>' +
        renderScopeFilter(a.scope) + '</div>' +
      '<div class="col" style="gap:5px"><span class="f11 t-mut">资产状态 state（5 个）</span>' +
        renderStateFilter(a.state) + '</div>' +
      '</div>' +
      note('info', '「待确认」不是后端的状态。',
        '后端 ASSET_STATES 是 DRAFT / ACTIVE / INDEXING / SUPERSEDED / ARCHIVED。' +
        'INDEXING 的含义是**索引处理中**（机器在算 chunk），与原型里那个' +
        '**人工待确认**不是同一件事 —— 人工待确认对应的是知识提案的 PENDING（见提案卡）。' +
        '默认列表不含已归档，需要时传 includeArchived=true。'));
  }

  /* ---------------------------- 资产主表 ---------------------------- */

  function renderAssetTable(assets, activeAssetId) {
    var list = rows(assets);
    var head = '<thead><tr>' +
      '<th>标题 title</th>' +
      '<th style="width:96px">类别</th>' +
      '<th style="width:82px">范围</th>' +
      '<th style="width:104px">状态</th>' +
      '<th style="width:104px">生效版本</th>' +
      '<th style="width:124px">创建时间</th>' +
      '</tr></thead>';

    var body = list.length
      ? list.map(function (row) {
          var sMeta = ASSET_STATE_META[text(row.state)];
          var current = text(row.id) === text(activeAssetId);
          return '<tr' + (current ? ' class="tr-sel"' : '') +
            ' data-action="select-asset" data-asset-id="' + escapeHtml(text(row.id)) + '">' +
            '<td class="f11"><a href="#" data-action="select-asset" data-asset-id="' +
              escapeHtml(text(row.id)) + '" title="' + escapeHtml(text(row.title)) + '">' +
              escapeHtml(truncate(text(row.title) || '（无标题）', 46)) + '</a>' +
              (text(row.archivedAt) ? ' ' + tag('已归档于 ' + fmtTime(row.archivedAt), 'tag-muted') : '') +
            '</td>' +
            '<td>' + (CATEGORY_LABEL[text(row.category)]
              ? tag(CATEGORY_LABEL[text(row.category)], '')
              : tag(truncate(text(row.category) || '未标注', 18), 'tag-muted')) + '</td>' +
            '<td class="f11">' + escapeHtml(SCOPE_LABEL[text(row.scope)] || text(row.scope) || '—') + '</td>' +
            '<td>' + (sMeta ? tag(sMeta.label, sMeta.cls)
              : tag(truncate(text(row.state) || '未标注', 18), 'tag-muted')) + '</td>' +
            '<td class="f11 mono" title="' + escapeHtml(text(row.activeVersionId)) + '">' +
              (text(row.activeVersionId)
                ? escapeHtml(shortId(row.activeVersionId))
                : '<span class="t-mut">从未发布</span>') + '</td>' +
            '<td class="f11 mono">' + escapeHtml(fmtTime(row.createdAt) || '—') + '</td>' +
            '</tr>';
        }).join('')
      : '<tr><td colspan="6">' + emptyBlock('当前筛选下没有知识资产。',
          '换个筛选条件，或先新建资产') + '</td></tr>';

    return card('知识资产', 'GET /knowledge/assets · ' + list.length + ' 条',
      '<div class="table-wrap"><table class="tbl tbl-compact">' + head +
      '<tbody>' + body + '</tbody></table></div>' +
      (list.length === 100
        ? note('warn', '已取满 limit=100。', '本端点是真游标分页，还有更多资产未显示。')
        : ''));
  }

  /** 真实的 6 类分布。**不是**六域 180 字段那 6 个卡（见文件头 D1）。 */
  function renderCategoryTally(assets) {
    var list = rows(assets);
    if (!list.length) {
      return card('知识类别分布（真实计数）', '替代原型的「当前策略池分布」',
        emptyBlock('当前筛选下没有资产，无分布可算。',
          '空集不画 0 条柱 —— 0 会被读成「这一类真的是 0 条」'),
        noSourceBadge());
    }

    var counts = {};
    list.forEach(function (a) {
      var c = text(a.category) || '(未标注)';
      counts[c] = (counts[c] || 0) + 1;
    });
    var keys = CATEGORIES.filter(function (c) { return counts[c]; });
    Object.keys(counts).forEach(function (c) { if (keys.indexOf(c) === -1) keys.push(c); });
    var max = keys.reduce(function (a, c) { return Math.max(a, counts[c]); }, 0);

    var body = keys.map(function (c) {
      var known = CATEGORIES.indexOf(c) !== -1;
      return '<div class="bar-row">' +
        '<span class="f11 t-mut" style="width:120px">' +
          escapeHtml((CATEGORY_LABEL[c] || c)) +
          (known ? '' : '<span class="t-fai">（取值域外）</span>') + '</span>' +
        '<span class="bar"><span class="bar-fill" style="width:' +
          (max > 0 ? (counts[c] / max * 100).toFixed(1) : '0.0') + '%"></span></span>' +
        '<span class="bar-num f11">' + counts[c] + ' 条</span></div>';
    }).join('');

    return card('知识类别分布（真实计数）',
      '替代原型的「当前策略池分布」—— 策略池无端点',
      body +
      note('warn', '这张卡与上面的「六域 180 字段」是两回事。',
        '这里数的是**库里有多少条资产**（按后端 CATEGORIES 的 6 个类别）；' +
        '六域那 6 个卡数的是**标准里定义了多少字段**。两者数量都是 6 纯属巧合，' +
        '语义毫不相干，不要把它们合成一张卡。'));
  }

  /* ---------------------------- 资产详情 ---------------------------- */

  function renderAssetDetail(detail) {
    var d = detail && typeof detail === 'object' ? detail : null;
    if (!d) {
      return card('资产详情', 'GET /knowledge/assets/{assetId}',
        emptyBlock('未选中资产。', '点左侧表格里的标题查看详情'));
    }

    var sMeta = ASSET_STATE_META[text(d.state)];
    var v = d.version && typeof d.version === 'object' ? d.version : null;

    var meta = [
      ['类别 category', CATEGORY_LABEL[text(d.category)] || text(d.category) || '—'],
      ['可用范围 scope', SCOPE_LABEL[text(d.scope)] || text(d.scope) || '—'],
      ['品牌 brandId', shortId(d.brandId) || '—'],
      ['产品 productId', shortId(d.productId) || '—'],
      ['项目 projectId', shortId(d.projectId) || '—'],
      ['生效版本 activeVersionId', shortId(d.activeVersionId) || '从未发布']
    ].map(function (row) {
      return '<div class="row between f12"><span class="t-mut">' + escapeHtml(row[0]) + '</span>' +
        '<span class="mono">' + escapeHtml(row[1]) + '</span></div>';
    }).join('');

    /* activeChunkCount === 0 的含义是「索引尚未处理」（api.py:221 原文），
       不是「这条资产没内容」—— 必须说清，否则 0 会被读成空资产 */
    var chunk = (d.activeChunkCount === 0)
      ? note('warn', 'activeChunkCount = 0。',
          '生效版本还没有 chunk —— 含义是**索引尚未处理**（api.py:221 原文），' +
          '不是「这条资产没有内容」。此时语义检索检不到它。')
      : '<div class="row between f12"><span class="t-mut">生效版本 chunk 数</span>' +
        '<span class="mono">' + escapeHtml(text(d.activeChunkCount) || '—') + '</span></div>';

    var versionBlock = v
      ? '<div class="col" style="gap:6px;margin-top:12px">' +
        '<span class="f11 t-mut">当前版本内容（version 块 · 知识侧可按 versionId 回看正文）</span>' +
        '<div class="row" style="gap:6px;flex-wrap:wrap">' +
          '<span class="code f10" title="' + escapeHtml(text(v.versionId)) + '">' +
            escapeHtml(shortId(v.versionId)) + '</span>' +
          (text(v.effectiveFrom) ? tag('生效自 ' + fmtTime(v.effectiveFrom), '') : '') +
          (text(v.effectiveUntil) ? tag('生效至 ' + fmtTime(v.effectiveUntil), 'tag-warn') : '') +
          tag('来源片段 ' + rows2len(v.sourceSegmentIds) + ' 个', '') +
        '</div>' +
        '<span class="f11 t-mut">变更原因：' +
          escapeHtml(truncate(text(v.changeReason) || '—', 160)) + '</span>' +
        '<div class="f12" style="white-space:pre-wrap;line-height:1.7">' +
          escapeHtml(truncate(text(v.body) || '（正文为空）', 1200)) + '</div>' +
        '</div>'
      : note('info', '没有 version 块。', 'active_version 为 null —— 这条资产从未发布过版本。');

    return card('资产详情 · ' + truncate(text(d.title) || '（无标题）', 32),
      'GET /knowledge/assets/{assetId}',
      '<div class="row mb10" style="gap:6px;flex-wrap:wrap">' +
        (sMeta ? tag(sMeta.label, sMeta.cls) : tag(text(d.state) || '状态未标注', 'tag-muted')) +
        (text(d.archivedAt) ? tag('归档于 ' + fmtTime(d.archivedAt), 'tag-muted') : '') +
        '<span class="code f10" title="' + escapeHtml(text(d.assetId)) + '">' +
          escapeHtml(shortId(d.assetId)) + '</span>' +
      '</div>' +
      '<div class="col" style="gap:6px">' + meta + chunk + '</div>' +
      versionBlock);
  }

  function rows2len(value) {
    return Array.isArray(value) ? value.length : 0;
  }

  /** 版本历史。⚠️ 后端**没有 versionNo** —— 不得自造「v1 / v2」序号。 */
  function renderVersionHistory(versions) {
    var list = rows(versions);
    if (!list.length) {
      return card('版本历史', 'GET /knowledge/assets/{assetId}/versions',
        emptyBlock('这条资产还没有版本。', '新建版本后会出现在这里'));
    }

    var body = list.map(function (v) {
      var meta = VERSION_STATE_META[text(v.versionState)];
      return '<div class="row between" style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.05)">' +
        '<div class="col" style="gap:3px;flex:1">' +
          '<div class="row" style="gap:6px;flex-wrap:wrap">' +
            '<span class="code f10" title="' + escapeHtml(text(v.versionId)) + '">' +
              escapeHtml(shortId(v.versionId)) + '</span>' +
            (meta ? tag(meta.label, meta.cls) : tag(text(v.versionState) || '状态未标注', 'tag-muted')) +
          '</div>' +
          '<span class="f11 t-mut">' + escapeHtml(truncate(text(v.changeReason) || '（无变更原因）', 110)) + '</span>' +
        '</div>' +
        '<span class="f10 t-fai mono">' + escapeHtml(fmtTime(v.createdAt) || '—') + '</span>' +
        '</div>';
    }).join('');

    return card('版本历史', list.length + ' 个版本',
      body +
      note('info', '这里没有「v1 / v2」这种版本序号。',
        'VersionItemBody（knowledge/api.py:259-267）只有 versionId / versionState / ' +
        'changeReason / createdAt —— **没有 versionNo 字段**。版本的先后顺序靠 ' +
        'createdAt 与投影出的 versionState 判断，不得自造序号。' +
        '（对比：内容侧的 ContentVersionBody 是有 versionNo 的，两边口径不同。）'));
  }

  /** 发布事件流 —— 「归档可追溯」的落点。 */
  function renderPublicationHistory(history) {
    var list = rows(history).slice().sort(function (a, b) {
      return Number(a.sequence) - Number(b.sequence);
    });
    if (!list.length) {
      return card('发布事件流', 'AssetDetailBody.publicationHistory',
        emptyBlock('这条资产还没有发布事件。', '激活 / 归档 / 恢复 / 替代都会在这里留痕'));
    }

    var body = list.map(function (h) {
      var label = PUBLICATION_ACTION_LABEL[text(h.action)];
      return '<div class="row" style="align-items:flex-start;gap:8px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.05)">' +
        '<span class="code f10">#' + escapeHtml(text(h.sequence)) + '</span>' +
        '<div class="col" style="flex:1;gap:3px">' +
          '<div class="row" style="gap:6px;flex-wrap:wrap">' +
            (label ? tag(label, '') : tag(text(h.action) || '未标注动作', 'tag-muted')) +
            '<span class="f10 t-fai mono" title="' + escapeHtml(text(h.versionId)) + '">版本 ' +
              escapeHtml(shortId(h.versionId)) + '</span>' +
          '</div>' +
          (text(h.reason)
            ? '<span class="f11 t-mut">' + escapeHtml(truncate(text(h.reason), 140)) + '</span>'
            : '') +
        '</div>' +
        '<div class="col" style="align-items:flex-end;gap:3px">' +
          '<span class="f10 t-fai mono">' + escapeHtml(fmtTime(h.createdAt) || '—') + '</span>' +
          '<span class="f10 t-fai mono" title="' + escapeHtml(text(h.actorId)) + '">' +
            escapeHtml(shortId(h.actorId) || '—') + '</span>' +
        '</div></div>';
    }).join('');

    return card('发布事件流', list.length + ' 条 · 按 sequence 升序',
      body +
      note('info', '归档不删版本、不改旧版本。',
        'ARCHIVE 只置 archived_at，knowledge_versions 表级不可变；' +
        'ARCHIVE 也不清空 active_version_id —— 否则「当前是什么」就没有答案了。' +
        '按 id 直读 GET /knowledge/assets/{id}?versionId=<旧版> 仍返回内容。'));
  }

  /** 被引用清单 —— 只有摘要、且按权限过滤。 */
  function renderReferences(refs) {
    var list = rows(refs);
    if (!list.length) {
      return card('被引用清单', 'GET /knowledge/assets/{assetId}/references',
        emptyBlock('这条资产的生效版本还没有被引用。',
          '注意：清单按权限过滤，无权项目的引用不会出现在这里'));
    }

    var head = '<thead><tr>' +
      '<th style="width:124px">引用方版本</th>' +
      '<th>用途 purpose</th>' +
      '<th style="width:112px">项目</th>' +
      '<th style="width:124px">片段 hash</th>' +
      '</tr></thead>';

    var body = list.map(function (r) {
      return '<tr>' +
        '<td class="f11 mono" title="' + escapeHtml(text(r.parentVersionId)) + '">' +
          escapeHtml(shortId(r.parentVersionId) || '—') + '</td>' +
        '<td class="f11">' + escapeHtml(truncate(text(r.purpose) || '—', 48)) + '</td>' +
        '<td class="f11 mono" title="' + escapeHtml(text(r.projectId)) + '">' +
          (text(r.projectId) ? escapeHtml(shortId(r.projectId)) : '<span class="t-mut">无项目</span>') + '</td>' +
        '<td class="f11 mono" title="' + escapeHtml(text(r.selectedFragmentHash)) + '">' +
          (text(r.selectedFragmentHash)
            ? escapeHtml(truncate(text(r.selectedFragmentHash), 12))
            : '<span class="t-mut">整版引用</span>') + '</td>' +
        '</tr>';
    }).join('');

    return card('被引用清单', list.length + ' 条 · 只返回摘要',
      '<div class="table-wrap"><table class="tbl tbl-compact">' + head +
      '<tbody>' + body + '</tbody></table></div>' +
      note('info', '这个端点刻意只给摘要。',
        '协议原文：受权限过滤的引用摘要，**不能借此读无权项目正文**' +
        '（knowledge/service.py:51-56）。所以这里只有「谁在哪个项目、以什么用途引用了它」，' +
        '没有引用方的正文。清单也只包含当前用户有权看到的项目 —— ' +
        '条数少不等于引用少。'));
  }

  /* ---------------------------- 知识提案（三段式的中段） ---------------------------- */

  function renderProposalList(proposals) {
    var list = rows(proposals);
    if (!list.length) {
      return card('知识提案', 'GET /knowledge/proposals',
        emptyBlock('当前没有知识提案。',
          '提案是「事件 → 变更提案 → 资产版本」三段式的中段'));
    }

    var body = list.map(function (p) {
      var meta = PROPOSAL_STATE_META[text(p.state)];
      var pending = text(p.state) === 'PENDING';
      var payload = p.proposedPayload && typeof p.proposedPayload === 'object'
        ? JSON.stringify(p.proposedPayload)
        : text(p.proposedPayload);

      return '<div class="row between" style="align-items:flex-start;gap:10px;padding:11px 0;border-bottom:1px solid rgba(255,255,255,0.05)">' +
        '<div class="col" style="flex:1;gap:5px">' +
          '<div class="row" style="gap:6px;flex-wrap:wrap">' +
            (meta ? tag(meta.label, meta.cls) : tag(text(p.state) || '状态未标注', 'tag-muted')) +
            tag(SCOPE_LABEL[text(p.scope)] || text(p.scope) || '范围未标注',
              SCOPE_LABEL[text(p.scope)] ? '' : 'tag-muted') +
            '<span class="f10 t-fai mono" title="' + escapeHtml(text(p.targetAssetId)) + '">目标资产 ' +
              (text(p.targetAssetId) ? escapeHtml(shortId(p.targetAssetId)) : '新建') + '</span>' +
          '</div>' +
          '<div class="f11 mono t-mut" style="word-break:break-all">' +
            escapeHtml(truncate(payload || '（提案内容为空）', 260)) + '</div>' +
          '<div class="row" style="gap:10px;flex-wrap:wrap">' +
            '<span class="f10 t-fai mono">' + escapeHtml(fmtTime(p.createdAt) || '—') + '</span>' +
            (text(p.confirmedBy)
              ? '<span class="f10 t-fai mono" title="' + escapeHtml(text(p.confirmedBy)) +
                '">确认人 ' + escapeHtml(shortId(p.confirmedBy)) + '</span>'
              : '') +
          '</div>' +
        '</div>' +
        (pending
          ? '<div class="col" style="gap:5px">' +
            '<button class="btn btn-sm btn-primary" data-action="accept-proposal" data-proposal-id="' +
              escapeHtml(text(p.id)) + '">采纳</button>' +
            '<button class="btn btn-sm btn-ghost" data-action="reject-proposal" data-proposal-id="' +
              escapeHtml(text(p.id)) + '">驳回</button>' +
            '</div>'
          : '<span class="f10 t-fai">已处置</span>') +
        '</div>';
    }).join('');

    var pendingCount = list.filter(function (p) { return text(p.state) === 'PENDING'; }).length;
    return card('知识提案', list.length + ' 条 · 待确认 ' + pendingCount + ' 条',
      body +
      note('info', '「三段式回写」的三段都有真实载体。',
        '事件 → /audit-events；变更提案 → /knowledge/proposals（本卡）；' +
        '资产版本 → /knowledge/assets/{id}/versions。' +
        '人工确认走 POST /knowledge/proposals/{id}/decisions，' +
        'decision 取值域只有 ACCEPT / REJECT。'));
  }

  function renderDecisionResult(result) {
    var r = result && typeof result === 'object' ? result : null;
    if (!r) return '';
    var meta = PROPOSAL_STATE_META[text(r.state)];
    return note(r.activated === true ? 'info' : 'warn',
      '提案已处置：' + ((meta && meta.label) || text(r.state) || '未知状态') + '。',
      '提案 ' + shortId(r.proposalId) +
      (text(r.createdVersionId) ? '；新建版本 ' + shortId(r.createdVersionId) : '；未新建版本') +
      (r.activated === true ? '；该版本已激活为生效版本。' : '；该版本未被激活。'));
  }

  /* ---------------------------- 语义检索 ---------------------------- */

  /**
   * 请求描述符（可单测）。
   * subject 是**必填**（search_api.py:169），三个 id 都可为 null。
   * mode 不传时服务端用默认 SEMANTIC；这里显式传，便于页面展示当前模式。
   */
  function searchRequest(form) {
    var f = form && typeof form === 'object' ? form : {};
    var body = {
      query: text(f.query),
      subject: {
        brandId: text(f.brandId) || null,
        productId: text(f.productId) || null,
        projectId: text(f.projectId) || null
      },
      mode: SEARCH_MODES.indexOf(text(f.mode)) !== -1 ? text(f.mode) : 'SEMANTIC'
    };
    if (text(f.category)) body.category = text(f.category);
    if (f.limit !== null && f.limit !== undefined && text(f.limit) !== '') {
      body.limit = Number(f.limit);
    }
    if (text(f.profileId)) body.profileId = text(f.profileId);
    return { method: 'POST', path: '/knowledge/searches', body: body };
  }

  function decisionRequest(proposalId, decision, reason) {
    var d = text(decision).toUpperCase();
    return {
      method: 'POST',
      path: '/knowledge/proposals/' + encodeURIComponent(text(proposalId)) + '/decisions',
      body: {
        decision: (d === 'ACCEPT' || d === 'REJECT') ? d : 'REJECT',
        reason: text(reason)
      }
    };
  }

  function renderSearchForm(form) {
    var f = form && typeof form === 'object' ? form : {};
    var modes = SEARCH_MODES.map(function (m) {
      return '<button class="btn btn-sm ' + (text(f.mode) === m ? 'btn-primary' : 'btn-ghost') +
        '" data-filter="mode" data-value="' + m + '">' +
        escapeHtml(SEARCH_MODE_LABEL[m]) + ' <span class="mono f10 t-fai">' + m + '</span></button>';
    }).join('');

    return card('语义检索', 'POST /knowledge/searches',
      '<div class="col" style="gap:9px">' +
      '<input class="inp" id="kb-q" type="text" placeholder="输入检索词（必填，≥1 字）" value="' +
        escapeHtml(text(f.query)) + '" />' +
      '<div class="row" style="gap:6px;flex-wrap:wrap">' + modes + '</div>' +
      '<button class="btn btn-primary btn-sm" data-action="search">' +
        '<i class="fa-solid fa-magnifying-glass"></i> 检索</button>' +
      '</div>' +
      note('info', 'mode 取值域只有两个。',
        'SEMANTIC（默认）与 KEYWORD（knowledge/embedding_types.py:207-215）。' +
        'profileId 的取值域在契约里只写了 string、未枚举，本页不传 ⇒ 用服务端默认档案。'));
  }

  function renderSearchMeta(meta) {
    var m = meta && typeof meta === 'object' ? meta : null;
    if (!m) return '';

    var warn = (m.indexReady === false)
      ? note('warn', '索引处理中（indexReady=false）。',
          '这次检索的结果**不完整** —— 部分资产的 chunk 还没算出来。' +
          '不要据此判断「库里没有这条知识」。')
      : '';

    var scopeNote = text(m.scopeNote)
      ? note('info', '可用范围已过滤：', text(m.scopeNote))
      : '';

    var kv = [
      ['检索模式 mode', SEARCH_MODE_LABEL[text(m.mode)] || text(m.mode) || '—'],
      ['嵌入档案 profileName', text(m.profileName) || '—'],
      ['扫过的 chunk 数 scanned', text(m.scanned) || '0'],
      ['档案下的 chunk 总数 chunkCountForProfile', text(m.chunkCountForProfile) || '0'],
      ['范围过滤是否生效 scopeFilterApplied', m.scopeFilterApplied === true ? '是' : '否'],
      ['请求上限 limit', text(m.limit) || '—']
    ].map(function (row) {
      return '<div class="row between f12"><span class="t-mut">' + escapeHtml(row[0]) + '</span>' +
        '<span class="mono">' + escapeHtml(row[1]) + '</span></div>';
    }).join('');

    return card('检索元信息', 'SearchResultBody.search',
      warn + scopeNote + '<div class="col" style="gap:6px">' + kv + '</div>' +
      note('warn', '「前置剔除 N 条」没有计数字段。',
        'scanned（扫过的 chunk 数）与 hits 条数都是真的，但「因范围被前置剔除了多少条」' +
        '后端只给了一个布尔 scopeFilterApplied 与一句文本 scopeNote，**没有计数**。' +
        '所以原型里「44 条选用 / 65 条命中 / 23 条前置剔除」的第三个数无法还原。'));
  }

  function renderSearchResults(result) {
    var r = result && typeof result === 'object' ? result : null;
    if (!r) {
      return card('检索结果', '',
        emptyBlock('还没有检索。', '在上面输入检索词后点「检索」'));
    }

    var hits = rows(r.hits);
    var meta = r.search && typeof r.search === 'object' ? r.search : null;

    if (!hits.length) {
      return card('检索结果', '0 条命中',
        emptyBlock('没有命中任何知识片段。',
          (meta && meta.indexReady === false)
            ? '注意 indexReady=false —— 可能是索引还没算完，不是真的没有'
            : '可换检索词，或放宽 category / 可用范围') +
        ((meta && meta.indexReady === false)
          ? note('warn', '索引处理中。', '0 条命中在这种情况下不代表「库里没有」。')
          : ''));
    }

    var body = hits.map(function (h) {
      return '<div class="row between" style="align-items:flex-start;gap:10px;padding:11px 0;border-bottom:1px solid rgba(255,255,255,0.05)">' +
        '<div class="col" style="flex:1;gap:5px">' +
          '<div class="row" style="gap:6px;flex-wrap:wrap">' +
            '<span class="f12 fw6" title="' + escapeHtml(text(h.assetTitle)) + '">' +
              escapeHtml(truncate(text(h.assetTitle) || '（无标题）', 40)) + '</span>' +
            (CATEGORY_LABEL[text(h.category)]
              ? tag(CATEGORY_LABEL[text(h.category)], '')
              : tag(truncate(text(h.category) || '未标注', 18), 'tag-muted')) +
            tag(SCOPE_LABEL[text(h.scope)] || text(h.scope) || '范围未标注',
              SCOPE_LABEL[text(h.scope)] ? '' : 'tag-muted') +
            '<span class="f10 t-fai mono">片段 #' + escapeHtml(text(h.chunkNo)) + '</span>' +
          '</div>' +
          '<div class="f11 t-mut" style="line-height:1.65">' +
            escapeHtml(truncate(text(h.text) || '（片段为空）', 220)) + '</div>' +
          '<span class="f10 t-fai mono" title="' + escapeHtml(text(h.assetId)) + '">资产 ' +
            escapeHtml(shortId(h.assetId)) + ' · 版本 ' + escapeHtml(shortId(h.versionId)) + '</span>' +
        '</div>' +
        '<div class="col" style="align-items:flex-end;gap:3px">' +
          '<span class="f11 mono">相似度 ' + escapeHtml(fmtSimilarity(h.similarity)) + '</span>' +
          '<span class="f10 t-fai mono">距离 ' + escapeHtml(fmtSimilarity(h.distance)) + '</span>' +
        '</div></div>';
    }).join('');

    return card('检索结果', hits.length + ' 条命中', body);
  }

  /** similarity / distance 是 float。不用 Intl，固定 4 位小数。 */
  function fmtSimilarity(value) {
    if (value === null || value === undefined || text(value) === '') return '—';
    var n = Number(value);
    return isFinite(n) ? n.toFixed(4) : truncate(text(value), 12);
  }

  /* ---------------------------- 教义区（静态 + 统一标注） ---------------------------- */

  function doctrineList(items) {
    return '<div class="col f12 t-mut" style="gap:7px">' +
      items.map(function (s) { return '<span>· ' + escapeHtml(s) + '</span>'; }).join('') +
      '</div>';
  }

  /** 降级 D1：六域 180 字段 —— 标准里的字段数，不是库里的资产数。 */
  function renderSixDomainDoctrine() {
    var domains = [
      ['D1 内容', 45], ['D2 策略', 35], ['D3 效果', 32],
      ['D4 主体', 30], ['D5 质量风险', 25], ['D6 采集溯源', 15]
    ];
    var cards = domains.map(function (d) {
      return '<div class="card metric">' +
        '<div class="metric-label">' + escapeHtml(d[0]) + '</div>' +
        '<div class="metric-value">' + d[1] + '<span class="unit"> 字段</span></div>' +
        '<div class="metric-foot f10 t-mut">标准定义的字段数</div></div>';
    }).join('');

    return card('预设字段 · 六域 180 字段', '方法论标准',
      '<div class="grid g3">' + cards + '</div>' +
      note('warn', '这 6 个数是「标准里定义了多少字段」，不是「库里有多少条资产」。',
        '后端 knowledge/types.py:65-70 的 CATEGORIES 只有 6 个常量' +
        '（FACT / RESEARCH / SCENE / STYLE / RULE / FEEDBACK_EXPERIMENT），' +
        '数量上与六域巧合都是 6，**语义毫不相干**；180 个字段在库里零对应。' +
        '这几张卡刻意**不**改接成 asset 计数 —— 那会新造一处「声称与数据矛盾」。' +
        '库里的真实分布请看另一张卡「知识类别分布（真实计数）」。'),
      renderDoctrineBadge());
  }

  /** 降级 D2：本体地基（C1–C7 / 关联键强度 / K1–K8）。 */
  function renderOntologyDoctrine() {
    return card('本体地基 · 实体与推理禁令', '方法论标准',
      '<div class="col" style="gap:12px">' +
      '<div class="col" style="gap:5px"><span class="f11 fw6">七类实体 C1–C7</span>' +
        doctrineList([
          'C1 主体（品牌 / 产品 / 项目）',
          'C2 内容对象（Brief / 基线 / 内容条目 / 版本）',
          'C3 策略对象（假设 / 实验 / 证据单元）',
          'C4 资产对象（知识资产与版本 —— 资产状态与策略状态是两套，不混用）',
          'C5 效果对象（发布 / 指标观测 / 阈值标签）',
          'C6 质量对象（反馈 / 审核 / 自检）',
          'C7 溯源对象（采集记录 / 来源片段 / 审计事件）'
        ]) + '</div>' +
      '<div class="col" style="gap:5px"><span class="f11 fw6">八条推理禁令 K1–K8</span>' +
        doctrineList([
          'K1 不得由相关性推因果',
          'K2 不得把单条高互动当策略证据',
          'K3 不得跨主体迁移未验证结论',
          'K4 不得用缺失值当 0 参与计算',
          'K5 不得把约数当精确值比较',
          'K6 不得在窗口未满时下结论',
          'K7 不得把人工编辑后的发布稿当生成结果评价',
          'K8 不得在样本量不足时标「已验证」'
        ]) + '</div>' +
      '</div>' +
      note('warn', '实体类型与具名关系在后端零对应。',
        'C1–C7 与 12 种具名关系是方法论命名，契约 86 paths 里没有对应资源。' +
        'K1–K8 是推理纪律，不是可查询的数据 —— 其中 K4（缺失不当 0）、K6（窗口未满不下结论）、' +
        'K8（样本不足不标已验证）在后端确有制度落点：' +
        'MetricObservation 的 valueNum 缺失填 null 而不是 0、' +
        'StrategyEvidence 的「缺失条件 ⇒ INCONCLUSIVE + INSUFFICIENT」红线。'),
      renderDoctrineBadge());
  }

  /** 降级 D2：SOP 七步四门 + F1–F6 + 证据等级。 */
  function renderSopDoctrine() {
    return card('知识库流程 · SOP 七步四门', '方法论标准',
      '<div class="col" style="gap:12px">' +
      '<div class="col" style="gap:5px"><span class="f11 fw6">七步</span>' +
        doctrineList([
          '1 归集 → 2 清洗去重 → 3 结构化打标 → 4 跨域关联 → 5 知识抽取 → 6 质量校验 → 7 入库版本化'
        ]) + '</div>' +
      '<div class="col" style="gap:5px"><span class="f11 fw6">六条一票否决 F1–F6（任一命中即不可用）</span>' +
        doctrineList([
          'F1 来源不可追溯',
          'F2 口径定义缺失',
          'F3 时效已过期',
          'F4 与已生效事实冲突且未裁定',
          'F5 可用范围未界定',
          'F6 人工确认缺失'
        ]) + '</div>' +
      '<div class="col" style="gap:5px"><span class="f11 fw6">证据等级与可用范围 A / B / C / D</span>' +
        doctrineList([
          'A 实验证据 —— 可作为策略依据',
          'B 观察证据 —— 可作为假设来源，不可声称因果',
          'C 单例经验 —— 仅供参考',
          'D 证据不足 —— 不得进入生产约束'
        ]) + '</div>' +
      '</div>' +
      note('warn', '七步、四门、F1–F6、A/B/C/D 四级证据在后端都没有字段。',
        '最接近的真实制度落点有两处：① 知识提案的 PENDING / ACCEPTED / REJECTED ' +
        '三态承载了 F6（人工确认）；② m4 草案 StrategyEvidence.evidenceGrade 的 ' +
        'EXPERIMENT / OBSERVATIONAL / INSUFFICIENT 三级与这里的 A / B / D 语义接近，' +
        '但**只有三级、且只在草案里**，不是正式契约。'),
      renderDoctrineBadge());
  }

  /** 降级 D4：策略六态 —— 正式契约与草案都不存在六态。 */
  function renderStrategyStateDoctrine() {
    return card('策略状态机（建议方案）', '方法论标准',
      doctrineList([
        '观察信号 → 相关性线索 → 候选策略 → 实验中 → 已验证策略 → 失效归档',
        '状态机必须双向：没有降级与回退就会积累僵尸策略',
        '未列出的转移一律拒绝；升级门必须显式配置参数'
      ]) +
      note('warn', '这六个状态在后端不存在。',
        '正式契约 contracts/openapi.json（86 paths）里 metric / effect / experiment / ' +
        'strategy 全部零命中；m4 草案 contracts/draft/m4-openapi.yaml 的 ' +
        'StrategyEvidence.state 只有 **4 态**（CANDIDATE / SUPPORTED / UNSUPPORTED / ' +
        'INCONCLUSIVE），与这里的六态不是一套。另外：资产状态（本页上方的 5 态）' +
        '与策略状态是**两套**，不要混用 —— 原型自己也写了这一条。'),
      renderDoctrineBadge());
  }

  /** 降级 D6：口径更正清单 / 首次通过率趋势 —— 文档自述，无端点。 */
  function renderCorrectionDoctrine() {
    return card('口径更正清单与通过率趋势', '文档自述',
      note('warn', '两块都没有读源。',
        '「N 处口径已更正」是这份文档自己的修订记录，不是系统里的数据；' +
        '「首次通过率趋势」需要审核结论的时间序列，而 content_reviews ' +
        '**没有任何 GET**（feedback/api.py 全文只有 2 个 @router.get）⇒ 读不到。' +
        '原型里写死的那几个百分比与趋势线已全部删除。'),
      noSourceBadge());
  }

  /* ---------------------------- 加载 / 空 / 错误 ---------------------------- */

  function renderLoading(label) {
    return '<section class="card"><div class="card-head"><span class="card-title">读取中</span></div>' +
      '<div class="card-body"><div class="row" style="gap:8px">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">' + escapeHtml(text(label) || '正在读取知识库…') + '</span>' +
      '</div></div></section>';
  }

  function renderEmpty(message) {
    return card('暂无数据', '', emptyBlock(text(message) || '知识库里还没有资产。'));
  }

  function renderError(err) {
    var e = err && typeof err === 'object' ? err : {};
    var code = text(e.code);
    var message = text(e.message) || text(err) || '读取知识库失败。';
    var offline = !!e.offline || code === 'DEPENDENCY_UNAVAILABLE';
    var hint = offline
      ? '连不上后端（页面结构已保留）。确认后端已启动后可重试。'
      : (e.retryable ? '该错误可重试，请稍后再试。' : '请按错误码排查后重试。');
    if (code === 'PERMISSION_DENIED') {
      hint = '知识库的写操作需要组织级 KNOWLEDGE_MANAGE 权限（knowledge/api.py:109）。';
    }

    return '<section class="card"><div class="card-head">' +
      '<span class="card-title">读取失败</span></div>' +
      '<div class="card-body"><div class="row" style="align-items:flex-start;gap:10px">' +
      '<i class="fa-solid fa-triangle-exclamation t-dan"></i>' +
      '<div class="col" style="flex:1;gap:4px">' +
      '<span class="f12 fw6">' + escapeHtml(message) + '</span>' +
      '<span class="f10 t-mut">' + escapeHtml((code ? code + ' · ' : '') + hint) + '</span>' +
      '</div>' +
      '<button class="btn btn-sm btn-ghost" data-action="retry">' +
      '<i class="fa-solid fa-rotate"></i> 重试</button>' +
      '</div></div></section>';
  }

  return {
    /* 纯计算 / 请求描述符 */
    searchRequest: searchRequest,
    decisionRequest: decisionRequest,
    fmtSimilarity: fmtSimilarity,
    /* 数据区 */
    renderFilters: renderFilters,
    renderCategoryFilter: renderCategoryFilter,
    renderScopeFilter: renderScopeFilter,
    renderStateFilter: renderStateFilter,
    renderAssetTable: renderAssetTable,
    renderCategoryTally: renderCategoryTally,
    renderAssetDetail: renderAssetDetail,
    renderVersionHistory: renderVersionHistory,
    renderPublicationHistory: renderPublicationHistory,
    renderReferences: renderReferences,
    renderProposalList: renderProposalList,
    renderDecisionResult: renderDecisionResult,
    renderSearchForm: renderSearchForm,
    renderSearchMeta: renderSearchMeta,
    renderSearchResults: renderSearchResults,
    /* 教义区 */
    renderDoctrineBadge: renderDoctrineBadge,
    renderSixDomainDoctrine: renderSixDomainDoctrine,
    renderOntologyDoctrine: renderOntologyDoctrine,
    renderSopDoctrine: renderSopDoctrine,
    renderStrategyStateDoctrine: renderStrategyStateDoctrine,
    renderCorrectionDoctrine: renderCorrectionDoctrine,
    /* 状态 */
    renderLoading: renderLoading,
    renderEmpty: renderEmpty,
    renderError: renderError,
    escapeHtml: escapeHtml,
    CATEGORIES: CATEGORIES,
    SCOPES: SCOPES,
    ASSET_STATES: ASSET_STATES,
    PROPOSAL_STATES: PROPOSAL_STATES,
    SEARCH_MODES: SEARCH_MODES
  };
});
