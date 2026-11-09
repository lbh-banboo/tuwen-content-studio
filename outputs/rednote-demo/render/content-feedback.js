/* ============================================================================
 * render/content-feedback.js —— content-feedback 页渲染层（方案 A）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读 DOM、不读浏览器全局、不做 I/O、
 * 不取「现在几点」。因此可直接 Node 单测。
 *
 * 双环境导出：
 *   Node/CommonJS  → require('./content-feedback.js')
 *   浏览器          → RednoteRender.contentFeedback
 *
 * ---------------------------------------------------------------------------
 * 本页的关键设计：4 个写动作全部做成「请求描述符」纯函数
 * ---------------------------------------------------------------------------
 * editRequest / advanceRequest / reviewRequest / batchReviewRequest 返回
 * `{method, path, body, ifMatch}`，挂载层退化成一行：
 *   var r = R.advanceRequest(item, 'RESOLVED'); await API.patch(r.path, r.body, {ifMatch: r.ifMatch});
 * 这样 URL / body / If-Match 的拼装全部进了可单测区。
 * **If-Match 拼错在浏览器里只表现为一个 412，很难归因** —— 这是本页唯一
 * 能机械证明乐观锁拼对了的地方。
 *
 * ---------------------------------------------------------------------------
 * 数据来源（WP29 七端点 + content/media；读的是后端源码，**不以
 * contracts/openapi.json 为准** —— tools/contracts/export_openapi.py:59
 * _strip_titles() 会删掉所有名为 title 的响应字段，PreviewResponseBody.title /
 * PreviewTitleCandidateBody.title / EditedVersionResponseBody.title /
 * ContentSummaryBody.title / ContentVersionBody.title 在快照里全部缺失，
 * 运行时真有，见 feedback/schemas.py:231、:211、:260 与 content/api.py:597、:701）：
 *
 * ① GET /contents/{contentId}/preview              feedback/api.py:360
 *      → PreviewResponseBody                       feedback/schemas.py:215-249
 *        { contentId, projectId, currentVersionId, versionNo, lockVersion, etag,
 *          title, body, tags[], bindings[], paragraphs[], mediaItems[],
 *          titleCandidates[], feedbackCount, selfCheckSummary }
 *        paragraphs[] → { index, text, ref }        schemas.py:189-195
 *          ref 形如 p<序号>-<8位哈希>，正是「选中段落提意见」的锚点
 *        mediaItems[] → { mediaItemId, paragraphRef, sortOrder, state }  schemas.py:198-204
 *        titleCandidates[] → { titleCandidateId, title, isAdopted }      schemas.py:207-212
 *        **etag 在响应体里也给了一份**（feedback/api.py:384 原文：让前端不必读 header）
 *        ⇒ api.js 不需要新增读 header 的能力。
 *        ⚠️ selfCheckSummary **恒为 null**（schemas.py:241 原文：自检属 WP30，
 *          本波不编造数据）。
 *
 * ② POST /contents/{contentId}/edits               feedback/api.py:417
 *      请求 ContentEditRequestBody                  feedback/schemas.py:75-92
 *        4 字段**全可选**（title / body / tags / bindings），未传沿用上一版，
 *        按字段合并；extra="forbid"（多传一个 contentId 就 422）
 *      → 201 EditedVersionResponseBody              feedback/schemas.py:251-271
 *        返回**新版本摘要**（id / versionNo / title / payloadHash / schemaVersion /
 *        createdAt / createdBy / blueprintId / adoptedTitleCandidateId /
 *        generationContextId），**不返回更新后的内容**
 *      必带 If-Match（preview.etag）。缺失→428、`*`→428、格式非法→400、
 *      不匹配→412 且 details.currentEtag 回带当前值（platform/concurrency.py:106-160）
 *
 * ③ GET /contents/{contentId}/feedback             feedback/api.py:507
 *      → PaginatedEnvelope[FeedbackItemResponseBody]  feedback/schemas.py:279-312
 *      真游标分页（服务层 keyset，created_at DESC, id DESC）。
 *      筛选参数只有 state / category / anchorKind（api.py:520-522）——
 *      **没有 severity 筛选**，阻断过滤必须在本模块按字段做。
 *      游标绑定筛选：换了筛选还用旧游标 → 400 cursor_binding_mismatch
 *
 * ④ POST /contents/{contentId}/feedback            feedback/api.py:561
 *      请求 FeedbackCreateRequestBody               feedback/schemas.py:95-123
 *        必填 observedVersionId / anchorKind / category / severity / scope / body
 *        可选 paragraphRef / selectedText / mediaItemId / suggestedCategory
 *        **没有 authorKind / createdBy**（端点固定记 USER + 当前用户）
 *      无 If-Match（新建不覆盖任何行）
 *
 * ⑤ PATCH /feedback/{feedbackId}                   feedback/api.py:631
 *      请求 FeedbackAdvanceRequestBody              feedback/schemas.py:126-144
 *        toState + 可选 note / rejectReason / resultingVersionId
 *      必带 If-Match，ETag = '"{feedbackId}:{lockVersion}"'
 *        （platform/concurrency.py:47 build_etag + _ETAG_SEPARATOR=':'）
 *      冲突 412 且 details.reason = FEEDBACK_LOCK_VERSION_MISMATCH
 *        （feedback/types.py:166，专用常量，与内容锁冲突可区分）
 *      ⚠️ 状态机**没有 from→to 合法转移表**（feedback/service.py:700 只做
 *        _required_choice(to_state, allowed=FEEDBACK_STATES)）⇒ RESOLVED→OPEN
 *        回退、OPEN→OPEN 自转都会成功。所以页面**不得**画成单向四步进度条，
 *        要渲染成「当前状态 + 四个可选目标状态」。
 *
 * ⑥ POST /contents/{contentId}/reviews             feedback/api.py:710
 *      请求 ReviewDecisionRequestBody               feedback/schemas.py:147-164
 *        必填 subjectVersionId / decision / reason，可选 supersedesId
 *      → 201 ReviewResponseBody                     feedback/schemas.py:320-331
 *      APPROVED 会被未解决的 BLOCKING 反馈挡住 → **409 INVALID_TRANSITION**，
 *      details.reason = UNRESOLVED_BLOCKING_FEEDBACK + details.blockingFeedbackIds[]
 *        （feedback/service.py:873-889）
 *      无 If-Match（审核行不可变，改判靠追加 + supersedesId）
 *
 * ⑦ POST /projects/{projectId}/content-reviews     feedback/api.py:766
 *      请求 ReviewBatchRequestBody                  feedback/schemas.py:167-181
 *        contentItemIds[] + decision + reason（**不传版本**，服务端按「当前版本」逐条绑定）
 *      → **200**（不是 201）ReviewBatchResponseBody  feedback/schemas.py:334-351
 *        { results: [{ contentItemId, ok, review?, error? }] }
 *        **error 是人类可读诊断串，不是错误码**
 *
 * 另外用到：
 *   GET /contents/{contentId}/media                media/api.py:1162
 *     → PaginatedEnvelope[MediaSummaryBody]         media/api.py:605-618
 *       { mediaId, contentId, projectId, state, paragraphRef, sortOrder,
 *         currentVersionId }
 *       ⚠️ 是 **currentVersionId（uuid|null）**，不是 currentVersionNo。
 *       文件名 / 尺寸 / 文件大小 / 「AI 生成 vs 人工替换」**均无字段**。
 *   GET /contents/{contentId}/versions             content/api.py:1127
 *   GET /projects/{projectId}/contents             content/api.py:978
 *     → PaginatedEnvelope[ContentSummaryBody]      content/api.py:593-601
 *       { contentId, projectId, title, state, currentVersionNo, createdAt, updatedAt }
 *   POST /contents/{contentId}/regenerations       content/api.py:1188
 *     请求 RegenerationBody                        content/api.py:463-484
 *       **scope + batchKind + modelProfileId 三个全必填**，extra="forbid"
 *
 * 受控取值域（feedback/types.py，所有下拉一律用这几组，不自创）：
 *   anchorKind  WHOLE / PARAGRAPH / MEDIA（三选一，列占用互斥）  types.py:35-37
 *   category    FACT TONE STRUCTURE SCENE DUPLICATE
 *               MEDIA_CONSISTENCY TECH_FORMAT OTHER             types.py:49-56
 *               —— 正是 demo 那 8 类中文的英文对应；本页直接采用这 8 个
 *                  FEEDBACK_CATEGORY_* 常量（已入库、已有 CHECK 约束）
 *   severity    BLOCKING / WARNING / INFO                       types.py:77-79
 *   scope       CURRENT_ONLY / FUTURE_GENERATIONS                types.py:95-96
 *   state       OPEN / IN_PROGRESS / RESOLVED / REJECTED         types.py:108-111
 *   authorKind  USER / AI                                        types.py:130-131
 *   decision    APPROVED / ELIMINATED                            types.py:141-142
 *   anchorState FRESH / STALE（只读、读时判定、不落库）  feedback/service.py:112-113
 *     R5：STALE 照常返回、不过滤（service.py:433-436）⇒ 必须展示标记，不得隐藏
 *   长度上限  body ≤2000 / selectedText ≤4000 / paragraphRef ≤200
 *             / rejectReason ≤500                                types.py:155-158
 *   重做范围  TITLE / COPY / TAGS / FULL                   content/types.py:107-110
 *     ⚠️ TAGS 会被服务层折成 COPY（content/types.py:104 + service.py:943）
 *
 * ---------------------------------------------------------------------------
 * 降级登记（原型 9 处硬编码声称逐条处置）：
 *   D1 「1 项阻断事实问题」「1 条未解决的关键事实问题」—— 可真算
 *      （severity==='BLOCKING' 且 state ∉ {RESOLVED,REJECTED}，与后端
 *      service.py:874-878 的 SQL 判据同口径）⇒ **由数据驱动，不保留数字**。
 *   D2 「4 / 10」翻页器、「本轮 10 条」、「全部 10 / 待审核 3 / 修改中 1 /
 *      已通过 5 / 已淘汰 1」—— 后三个无读源（审核不可读，§审核只能写不能读）
 *      ⇒ 只渲染能从 feedback.state 算出的计数，审核态整组降级。
 *   D3 「v2」「v2（v1 已存档）」—— 来自 preview.versionNo，真接。
 *   D4 AI 自检 8 卡的「1 阻断 / 通过 / 1 提示 / 相似度 71% /
 *      1440×1920 · JPG · 均 <2MB / 禁用词检测 0 命中」—— 全部是编的，
 *      selfCheckSummary === null ⇒ **词表保留**（它是真的受控取值域），
 *      **结果整组降级**并写明「自检属 WP30，尚无数据」。
 *   D5 「BL-v3」「PLAN-v2」—— preview 不返回 baselineId / planId，无字段。
 *   D6 「2 项未满足」+ 5 条方向进度（健身后油汗清洁 0 条通过 等）—— 无 scene
 *      字段 ⇒ 方向通过情况整块降级。
 *   D7 「BATCH-T01 第 1 轮 · 10 条 / 审核中 6/10」「BATCH-T02 未创建」——
 *      GET /projects/{id}/contents 只有 limit + cursor，契约里 batch 零命中
 *      ⇒ 「本轮」概念无法成立，只显示「项目下全部内容（最近 100 条）」。
 *   D8 元信息 4 字段里的「场景」与「形式·媒体」分类 —— 场景无字段；
 *      形式只能给出 mediaItems.length，「文案+静态图」这种分类无字段。
 *   D9 素材上传（4 步分片 + 幂等 + 完成回调）—— 本批不做写，保留说明。
 *   D10 重做提交 —— RegenerationBody 的 modelProfileId **必填**，而
 *      model_profiles 表在 modules/gateway（models.py:163）、该模块**无 api.py**、
 *      main.py 无 gateway_router ⇒ **拿不到任何合法 modelProfileId**。
 *      所以受控范围选项照实渲染，但提交按钮标为不可用并写明原因。
 *      regenerateRequest() 仍然提供（参数齐备即可用），等端点上线。
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.contentFeedback = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  /* ---------------------------- 受控取值域 ---------------------------- */

  /* 8 类自检 / 反馈分类 —— 直接采用 feedback/types.py 的 FEEDBACK_CATEGORY_*。
     主会话已裁定：与 WP30 对齐，不会有两套词表。 */
  var CATEGORIES = ['FACT', 'TONE', 'STRUCTURE', 'SCENE', 'DUPLICATE',
    'MEDIA_CONSISTENCY', 'TECH_FORMAT', 'OTHER'];
  var CATEGORY_LABEL = {
    FACT: '事实', TONE: '语气', STRUCTURE: '结构', SCENE: '场景',
    DUPLICATE: '重复', MEDIA_CONSISTENCY: '文图一致', TECH_FORMAT: '技术格式', OTHER: '其他'
  };

  var ANCHOR_KINDS = ['WHOLE', 'PARAGRAPH', 'MEDIA'];
  var ANCHOR_KIND_LABEL = { WHOLE: '整篇', PARAGRAPH: '段落', MEDIA: '媒体' };

  var SEVERITIES = ['BLOCKING', 'WARNING', 'INFO'];
  var SEVERITY_META = {
    BLOCKING: { label: '阻断', cls: 'tag-danger' },
    WARNING: { label: '警告', cls: 'tag-warn' },
    INFO: { label: '提示', cls: '' }
  };

  var SCOPES = ['CURRENT_ONLY', 'FUTURE_GENERATIONS'];
  var SCOPE_LABEL = { CURRENT_ONLY: '仅本条', FUTURE_GENERATIONS: '应用后续生成' };

  var STATES = ['OPEN', 'IN_PROGRESS', 'RESOLVED', 'REJECTED'];
  var STATE_META = {
    OPEN: { label: '待处理', cls: 'tag-warn' },
    IN_PROGRESS: { label: '处理中', cls: 'tag-purple' },
    RESOLVED: { label: '已解决', cls: 'tag-ok' },
    REJECTED: { label: '不采纳', cls: 'tag-danger' }
  };
  var SETTLED_STATES = ['RESOLVED', 'REJECTED'];

  var DECISIONS = ['APPROVED', 'ELIMINATED'];
  var DECISION_LABEL = { APPROVED: '标记已通过', ELIMINATED: '标记淘汰' };

  /* content_items.state 的 4 个取值（content/types.py:147-150）——
     **里面没有「已通过 / 已淘汰」**，不得把 GENERATED 当「已通过」显示。 */
  var CONTENT_STATE_META = {
    DRAFT: { label: '草稿', cls: '' },
    STRUCTURED: { label: '已结构化', cls: '' },
    GENERATED: { label: '已生成', cls: 'tag-purple' },
    FAILED: { label: '生成失败', cls: 'tag-danger' }
  };

  var REGEN_SCOPES = ['TITLE', 'COPY', 'TAGS', 'FULL'];
  var REGEN_SCOPE_LABEL = {
    TITLE: '仅标题', COPY: '仅正文', TAGS: '仅标签', FULL: '整篇'
  };

  var MAX = { body: 2000, selectedText: 4000, paragraphRef: 200, rejectReason: 500 };

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

  function doctrineBadge() {
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

  function options(values, labels, selected) {
    return values.map(function (v) {
      return '<option value="' + escapeHtml(v) + '"' +
        (text(selected) === v ? ' selected' : '') + '>' +
        escapeHtml((labels && labels[v]) ? labels[v] + '（' + v + '）' : v) + '</option>';
    }).join('');
  }

  /* ==========================================================================
   * 一、请求描述符（纯函数，本页的关键设计）
   * ======================================================================== */

  /** 反馈行的 ETag：'"{feedbackId}:{lockVersion}"'（concurrency.py:47 build_etag） */
  function feedbackEtag(item) {
    var it = item && typeof item === 'object' ? item : {};
    var id = text(it.id);
    var lv = it.lockVersion;
    if (!id || lv === null || lv === undefined || text(lv) === '') return '';
    return '"' + id + ':' + lv + '"';
  }

  /**
   * ② 直接编辑。4 字段全可选、未传沿用上一版；extra="forbid" ⇒ body 里
   * 只能出现这 4 个键，多一个（比如 contentId）就 422。
   */
  function editRequest(preview, patch) {
    var p = preview && typeof preview === 'object' ? preview : {};
    var src = patch && typeof patch === 'object' ? patch : {};
    var body = {};
    if (Object.prototype.hasOwnProperty.call(src, 'title') && src.title !== undefined) {
      body.title = text(src.title);
    }
    if (Object.prototype.hasOwnProperty.call(src, 'body') && src.body !== undefined) {
      body.body = text(src.body);
    }
    if (Array.isArray(src.tags)) body.tags = src.tags.map(text);
    if (Array.isArray(src.bindings)) body.bindings = src.bindings;
    return {
      method: 'POST',
      path: '/contents/' + encodeURIComponent(text(p.contentId)) + '/edits',
      body: body,
      ifMatch: text(p.etag)
    };
  }

  /** ④ 新建意见。无 If-Match（新建不覆盖任何行）。 */
  function createFeedbackRequest(preview, form) {
    var p = preview && typeof preview === 'object' ? preview : {};
    var f = form && typeof form === 'object' ? form : {};
    var kind = ANCHOR_KINDS.indexOf(text(f.anchorKind)) !== -1 ? text(f.anchorKind) : 'WHOLE';
    var body = {
      observedVersionId: text(f.observedVersionId) || text(p.currentVersionId),
      anchorKind: kind,
      category: CATEGORIES.indexOf(text(f.category)) !== -1 ? text(f.category) : 'OTHER',
      severity: SEVERITIES.indexOf(text(f.severity)) !== -1 ? text(f.severity) : 'INFO',
      scope: SCOPES.indexOf(text(f.scope)) !== -1 ? text(f.scope) : 'CURRENT_ONLY',
      body: truncate(text(f.body), MAX.body)
    };
    /* 锚点三选一、列占用互斥：PARAGRAPH 只带 paragraphRef，
       MEDIA 只带 mediaItemId，WHOLE 两者都不带 */
    if (kind === 'PARAGRAPH' && text(f.paragraphRef)) {
      body.paragraphRef = truncate(text(f.paragraphRef), MAX.paragraphRef);
    }
    if (kind === 'MEDIA' && text(f.mediaItemId)) body.mediaItemId = text(f.mediaItemId);
    if (text(f.selectedText)) body.selectedText = truncate(text(f.selectedText), MAX.selectedText);
    if (CATEGORIES.indexOf(text(f.suggestedCategory)) !== -1) {
      body.suggestedCategory = text(f.suggestedCategory);
    }
    return {
      method: 'POST',
      path: '/contents/' + encodeURIComponent(text(p.contentId)) + '/feedback',
      body: body,
      ifMatch: null
    };
  }

  /**
   * ⑤ 推进状态。必带 If-Match = feedbackEtag(item)。
   * to_state === REJECTED 时 rejectReason 必填（≤500）；
   * 非 REJECTED 时服务端把 reject_reason 归一为 NULL —— 所以这里**不传**它，
   * 传了也会被清掉，传了反而让人以为能保留。
   */
  function advanceRequest(item, toState, opts) {
    var it = item && typeof item === 'object' ? item : {};
    var o = opts && typeof opts === 'object' ? opts : {};
    var to = STATES.indexOf(text(toState)) !== -1 ? text(toState) : text(toState);
    var body = { toState: to };
    if (text(o.note)) body.note = text(o.note);
    if (to === 'REJECTED') body.rejectReason = truncate(text(o.rejectReason), MAX.rejectReason);
    if (text(o.resultingVersionId)) body.resultingVersionId = text(o.resultingVersionId);
    return {
      method: 'PATCH',
      path: '/feedback/' + encodeURIComponent(text(it.id)),
      body: body,
      ifMatch: feedbackEtag(it)
    };
  }

  /** ⑥ 单条审核。无 If-Match（审核行不可变，改判靠追加 + supersedesId）。 */
  function reviewRequest(preview, decision, reason, supersedesId) {
    var p = preview && typeof preview === 'object' ? preview : {};
    var body = {
      subjectVersionId: text(p.currentVersionId),
      decision: DECISIONS.indexOf(text(decision)) !== -1 ? text(decision) : 'ELIMINATED',
      reason: text(reason)
    };
    if (text(supersedesId)) body.supersedesId = text(supersedesId);
    return {
      method: 'POST',
      path: '/contents/' + encodeURIComponent(text(p.contentId)) + '/reviews',
      body: body,
      ifMatch: null
    };
  }

  /** ⑦ 批量审核。**不传版本**（服务端按「当前版本」逐条绑定），返回 200。 */
  function batchReviewRequest(projectId, contentItemIds, decision, reason) {
    return {
      method: 'POST',
      path: '/projects/' + encodeURIComponent(text(projectId)) + '/content-reviews',
      body: {
        contentItemIds: (Array.isArray(contentItemIds) ? contentItemIds : []).map(text),
        decision: DECISIONS.indexOf(text(decision)) !== -1 ? text(decision) : 'ELIMINATED',
        reason: text(reason)
      },
      ifMatch: null
    };
  }

  /**
   * 重做。三个字段全必填（content/api.py:482-484，extra="forbid"）。
   * modelProfileId 目前拿不到合法值（降级 D10），所以挂载层不启用提交，
   * 但这个描述符留着：端点一上线，参数齐备即可用。
   */
  function regenerateRequest(preview, scope, batchKind, modelProfileId) {
    var p = preview && typeof preview === 'object' ? preview : {};
    return {
      method: 'POST',
      path: '/contents/' + encodeURIComponent(text(p.contentId)) + '/regenerations',
      body: {
        scope: REGEN_SCOPES.indexOf(text(scope)) !== -1 ? text(scope) : 'FULL',
        batchKind: text(batchKind),
        modelProfileId: text(modelProfileId)
      },
      ifMatch: null
    };
  }

  /**
   * 错误分支文案。412 / 428 / 409 / 400 四条路径各有专门说明 ——
   * 通用「操作失败」会让人去猜，而这四种的处置动作完全不同。
   */
  function describeError(err) {
    var e = err && typeof err === 'object' ? err : {};
    var code = text(e.code);
    var status = Number(e.status);
    var d = e.details && typeof e.details === 'object' ? e.details : {};
    var reason = text(d.reason);

    if (code === 'VERSION_CONFLICT' || status === 412) {
      if (reason === 'FEEDBACK_LOCK_VERSION_MISMATCH') {
        return {
          kind: 'feedback_conflict',
          title: '这条意见已被别人改过',
          hint: '反馈行的 lockVersion 变了（details.reason = FEEDBACK_LOCK_VERSION_MISMATCH，' +
            '与内容锁冲突是两个不同的 reason）。请重新读取意见列表后再推进状态。',
          currentEtag: text(d.currentEtag)
        };
      }
      return {
        kind: 'content_conflict',
        title: '内容已被别人改过',
        hint: '当前稿的 ETag 与你读到的不一致。' +
          (text(d.currentEtag)
            ? '服务端回带了当前值 ' + text(d.currentEtag) + '，请对比后再提交。'
            : '请重新读取预览后再提交。'),
        currentEtag: text(d.currentEtag)
      };
    }

    if (code === 'PRECONDITION_REQUIRED' || status === 428) {
      var why = reason === 'if_match_wildcard_unsupported'
        ? 'If-Match 不支持通配符 *，必须给具体 ETag。'
        : (reason === 'if_match_empty'
          ? 'If-Match 为空。'
          : '这次请求没带 If-Match 头。');
      return {
        kind: 'precondition',
        title: '缺少并发前置条件',
        hint: why + ' 428 与 412 是两回事：428 是「你没提供前置条件」（去读一次就行），' +
          '412 是「版本不匹配」（要把你的编辑与新版本比较）。',
        currentEtag: ''
      };
    }

    if (code === 'INVALID_TRANSITION' || status === 409) {
      var ids = Array.isArray(d.blockingFeedbackIds) ? d.blockingFeedbackIds : [];
      if (reason === 'UNRESOLVED_BLOCKING_FEEDBACK' || ids.length) {
        return {
          kind: 'blocking',
          title: '存在未解决的阻断意见，无法标记已通过',
          hint: '服务端拒绝（409 INVALID_TRANSITION，details.reason = ' +
            'UNRESOLVED_BLOCKING_FEEDBACK）。涉及 ' + ids.length + ' 条意见：' +
            ids.map(shortId).join('、') + '。把它们处置为已解决或不采纳后再试。',
          blockingFeedbackIds: ids.map(text)
        };
      }
      return {
        kind: 'transition',
        title: '状态转移被拒绝',
        hint: text(e.message) || '服务端认为当前状态不允许这次转移。',
        blockingFeedbackIds: []
      };
    }

    if (reason === 'cursor_binding_mismatch') {
      return {
        kind: 'cursor',
        title: '游标与筛选条件不匹配',
        hint: '换了筛选条件却沿用了旧游标（400 cursor_binding_mismatch）。' +
          '请从第一页重新读取，而不是继续翻页 —— 后端刻意拒绝而不是静默返回错位的一页。',
        currentEtag: ''
      };
    }

    if (code === 'VALIDATION_FAILED' || status === 400 || status === 422) {
      return {
        kind: 'validation',
        title: '请求被校验拒绝',
        hint: (text(e.message) || '字段校验未通过。') +
          ' 注意编辑端点是 extra="forbid"：body 里只能出现 title / body / tags / bindings 四个键。',
        currentEtag: ''
      };
    }

    return {
      kind: 'other',
      title: text(e.message) || text(err) || '操作失败。',
      hint: (code ? code + ' · ' : '') +
        (e.offline || code === 'DEPENDENCY_UNAVAILABLE'
          ? '连不上后端。确认后端已启动后可重试。'
          : (e.retryable ? '该错误可重试。' : '请按错误码排查后重试。')),
      currentEtag: ''
    };
  }

  /* ==========================================================================
   * 二、纯计算
   * ======================================================================== */

  function indexVersions(versions) {
    var index = {};
    rows(versions).forEach(function (v) {
      var id = text(v.versionId);
      if (id && v.versionNo !== null && v.versionNo !== undefined) index[id] = v.versionNo;
    });
    return index;
  }

  function versionLabel(id, index) {
    var s = text(id);
    if (!s) return '—';
    var map = index && typeof index === 'object' ? index : {};
    return Object.prototype.hasOwnProperty.call(map, s) ? 'v' + map[s] : shortId(s);
  }

  /** 未解决的阻断意见 —— 与后端 service.py:874-878 的 SQL 判据同口径。 */
  function unresolvedBlocking(items) {
    return rows(items).filter(function (it) {
      return text(it.severity) === 'BLOCKING' && SETTLED_STATES.indexOf(text(it.state)) === -1;
    });
  }

  function tallyFeedback(items) {
    var list = rows(items);
    var byState = {}, bySeverity = {}, byCategory = {};
    var stale = 0, ai = 0;
    STATES.forEach(function (s) { byState[s] = 0; });
    SEVERITIES.forEach(function (s) { bySeverity[s] = 0; });
    list.forEach(function (it) {
      var st = text(it.state), sv = text(it.severity), ct = text(it.category);
      byState[st] = (byState[st] || 0) + 1;
      bySeverity[sv] = (bySeverity[sv] || 0) + 1;
      byCategory[ct] = (byCategory[ct] || 0) + 1;
      if (text(it.anchorState) === 'STALE') stale += 1;
      if (text(it.authorKind) === 'AI') ai += 1;
    });
    return {
      total: list.length,
      byState: byState,
      bySeverity: bySeverity,
      byCategory: byCategory,
      stale: stale,
      ai: ai,
      unresolvedBlocking: unresolvedBlocking(list).length,
      pending: list.filter(function (it) {
        return SETTLED_STATES.indexOf(text(it.state)) === -1;
      }).length
    };
  }

  /* ==========================================================================
   * 三、渲染
   * ======================================================================== */

  /** 手机预览：标题 / 正文段落 / 标签 / 图位。全部来自 preview。 */
  function renderPreviewPhone(preview) {
    var p = preview && typeof preview === 'object' ? preview : null;
    if (!p) {
      return card('手机预览', 'GET /contents/{id}/preview',
        emptyBlock('未获取到预览。', '接口未返回 PreviewResponseBody'));
    }

    var paragraphs = rows(p.paragraphs).slice().sort(function (a, b) {
      return Number(a.index) - Number(b.index);
    });
    var tags = Array.isArray(p.tags) ? p.tags : [];
    var media = rows(p.mediaItems);

    var paraHtml = paragraphs.length
      ? paragraphs.map(function (par) {
          return '<p class="f12" style="line-height:1.75;margin:0 0 9px" ' +
            'data-paragraph-ref="' + escapeHtml(text(par.ref)) + '">' +
            escapeHtml(text(par.text)) + '</p>';
        }).join('')
      : '<p class="f12 t-mut">（正文没有分段 —— paragraphs 为空数组）</p>';

    var tagHtml = tags.length
      ? tags.map(function (t) { return tag('#' + truncate(text(t), 20), ''); }).join(' ')
      : '<span class="f11 t-mut">（无标签）</span>';

    var mediaHtml = media.length
      ? '<div class="row" style="gap:6px;flex-wrap:wrap">' +
        media.slice().sort(function (a, b) { return Number(a.sortOrder) - Number(b.sortOrder); })
          .map(function (m) {
            return '<span class="code f10" title="' + escapeHtml(text(m.mediaItemId)) + '">#' +
              escapeHtml(text(m.sortOrder)) + ' ' + escapeHtml(text(m.state) || '状态未标注') + '</span>';
          }).join('') +
        '</div>'
      : '<span class="f11 t-mut">（无图位 —— mediaItems 为空数组）</span>';

    return card('手机预览 · v' + text(p.versionNo),
      'GET /contents/{id}/preview · 正文与段落都是真字段',
      '<div class="col" style="gap:10px">' +
      '<span class="f13 fw6" style="line-height:1.5">' +
        escapeHtml(truncate(text(p.title) || '（无标题）', 120)) + '</span>' +
      '<div>' + paraHtml + '</div>' +
      '<div class="row" style="gap:5px;flex-wrap:wrap">' + tagHtml + '</div>' +
      '<div class="col" style="gap:5px"><span class="f11 t-mut">图位（' + media.length + '）</span>' +
        mediaHtml + '</div>' +
      '</div>' +
      note('info', '预览不替代真实设备验收。',
        '这里渲染的是接口返回的 title / body 分段 / tags / mediaItems，' +
        '不是小红书端的真实排版。上线前仍需在真机上看一遍。'));
  }

  /** 段落锚点选择器：ref 形如 p<序号>-<8位哈希>，是提意见时的锚点。 */
  function renderParagraphAnchors(preview) {
    var p = preview && typeof preview === 'object' ? preview : {};
    var paragraphs = rows(p.paragraphs).slice().sort(function (a, b) {
      return Number(a.index) - Number(b.index);
    });
    if (!paragraphs.length) {
      return card('段落锚点', 'preview.paragraphs[].ref',
        emptyBlock('没有段落锚点。', '正文未分段时只能按「整篇」提意见'));
    }

    var body = paragraphs.map(function (par) {
      return '<div class="row between" style="gap:8px;padding:7px 0;border-bottom:1px solid rgba(255,255,255,0.05)">' +
        '<div class="col" style="flex:1;gap:2px">' +
          '<span class="code f10">' + escapeHtml(text(par.ref)) + '</span>' +
          '<span class="f11 t-mut">' + escapeHtml(truncate(text(par.text), 70)) + '</span>' +
        '</div>' +
        '<button class="btn btn-sm btn-ghost" data-action="anchor-paragraph" ' +
          'data-paragraph-ref="' + escapeHtml(text(par.ref)) + '">对此段提意见</button>' +
        '</div>';
    }).join('');

    return card('段落锚点', paragraphs.length + ' 段 · ref 即锚点', body);
  }

  /** 元信息 4 字段：2 真 2 降级（降级 D5 / D8）。 */
  function renderMetaFields(preview) {
    var p = preview && typeof preview === 'object' ? preview : null;
    if (!p) return card('元信息', '', emptyBlock('未获取到预览。'));

    var media = rows(p.mediaItems);
    var fields = [
      ['版本', 'v' + text(p.versionNo) + '（currentVersionId ' + (shortId(p.currentVersionId) || '—') + '）', true],
      ['形式 · 媒体', media.length + ' 个图位', 'partial'],
      ['场景', '无数据源', false],
      ['基线 · 方案', '无数据源', false]
    ];

    var body = fields.map(function (f) {
      var cls = f[2] === true ? '' : (f[2] === 'partial' ? 'tag-warn' : 'tag-muted');
      return '<div class="row between f12" style="padding:7px 0;border-bottom:1px solid rgba(255,255,255,0.05)">' +
        '<span class="t-mut">' + escapeHtml(f[0]) + '</span>' +
        '<span class="row" style="gap:6px;align-items:center">' +
          '<span class="mono f11">' + escapeHtml(f[1]) + '</span>' +
          (f[2] === true ? tag('真数据', 'tag-ok') : tag(f[2] === 'partial' ? '部分可接' : '无字段', cls)) +
        '</span></div>';
    }).join('');

    return card('元信息', '4 字段 · 2 真 2 降级',
      body +
      note('warn', '「场景」与「基线 · 方案」没有字段。',
        'preview 不返回 baselineId / planId；ContentDetailBody 有 blueprint / contexts ' +
        '但那不是 baselineId / planId。「场景」（如「卸妆后二次清洁」）在内容侧完全没有字段 —— ' +
        '原型里的 BL-v3 / PLAN-v2 是编的，已删除。' +
        '「形式 · 媒体」只能给出图位个数，「文案 + 静态图」这种形式分类也无字段。'));
  }

  /**
   * AI 自检 8 类卡。降级 D4：词表是真的（= FEEDBACK_CATEGORIES），**结果没有**。
   * WP30 上线后只需在这里补一个真分支 + 几条单测，v2.html 一行不改。
   */
  function renderSelfCheckGrid(preview) {
    var p = preview && typeof preview === 'object' ? preview : {};
    var summary = p.selfCheckSummary;
    var hasData = summary && typeof summary === 'object' && Object.keys(summary).length > 0;

    var cards = CATEGORIES.map(function (c) {
      var cell = hasData ? summary[c] : null;
      var valueHtml = (cell && typeof cell === 'object')
        ? escapeHtml(truncate(text(cell.label !== undefined ? cell.label : JSON.stringify(cell)), 30))
        : '—';
      return '<div class="card metric">' +
        '<div class="metric-label">' + escapeHtml(CATEGORY_LABEL[c]) +
          ' <span class="mono f10 t-fai">' + c + '</span></div>' +
        '<div class="metric-value" style="font-size:16px">' + valueHtml + '</div>' +
        '<div class="metric-foot f10 t-mut">' +
          (hasData ? '自检结果' : '无自检结果') + '</div></div>';
    }).join('');

    return card('AI 自检 · 8 类',
      '分类词表取自后端受控取值域 FEEDBACK_CATEGORY_*',
      '<div class="grid g4">' + cards + '</div>' +
      (hasData
        ? note('info', '自检结果已返回。', 'selfCheckSummary 非空，上面的值来自接口。')
        : note('warn', '8 类的词表是真的，自检结果没有。',
            'preview.selfCheckSummary **恒为 null**（feedback/schemas.py:241 原文：' +
            '自检属 WP30，本波不编造数据）。所以这 8 格全部显示「—」而不是 0 或「通过」。' +
            '原型里那组自检结论（阻断/通过/提示的条数、相似度百分比、图片尺寸与体积、' +
            '禁用词命中数）**全部是编的**，已整组删除。' +
            'WP30 上线后这张卡会自动有值 —— 渲染分支已就位。')),
      hasData ? '' : noSourceBadge());
  }

  /** 意见列表。STALE 必须展示、rejectReason 必须展示、版本映射成 v{n}。 */
  function renderFeedbackList(items, versionIndex) {
    var list = rows(items);
    if (!list.length) {
      return card('意见列表', 'GET /contents/{id}/feedback',
        emptyBlock('这条内容还没有意见。', '用右侧表单添加第一条'));
    }

    var index = versionIndex || {};
    var body = list.map(function (it) {
      var sevMeta = SEVERITY_META[text(it.severity)];
      var stMeta = STATE_META[text(it.state)];
      var tags = [];
      tags.push(CATEGORY_LABEL[text(it.category)]
        ? tag(CATEGORY_LABEL[text(it.category)], '')
        : tag(truncate(text(it.category) || '未标注分类', 18), 'tag-muted'));
      tags.push(sevMeta ? tag(sevMeta.label, sevMeta.cls)
        : tag(truncate(text(it.severity) || '未标注严重度', 18), 'tag-muted'));
      tags.push(ANCHOR_KIND_LABEL[text(it.anchorKind)]
        ? tag(ANCHOR_KIND_LABEL[text(it.anchorKind)], '')
        : tag(truncate(text(it.anchorKind) || '未标注粒度', 18), 'tag-muted'));
      tags.push(SCOPE_LABEL[text(it.scope)]
        ? tag(SCOPE_LABEL[text(it.scope)], '')
        : tag(truncate(text(it.scope) || '未标注作用域', 20), 'tag-muted'));
      if (text(it.anchorState) === 'STALE') tags.push(tag('锚点已失效 STALE', 'tag-warn'));
      if (text(it.authorKind) === 'AI') tags.push(tag('AI 提出', 'tag-purple'));

      var suggested = text(it.suggestedCategory);
      return '<div class="fb-row" data-feedback-id="' + escapeHtml(text(it.id)) + '">' +
        '<div class="row between" style="align-items:flex-start;gap:10px">' +
          '<div class="col" style="flex:1;gap:5px">' +
            '<span class="f12">' + escapeHtml(truncate(text(it.body) || '（正文为空）', 220)) + '</span>' +
            '<div class="row" style="gap:6px;flex-wrap:wrap">' + tags.join('') + '</div>' +
            (text(it.paragraphRef)
              ? '<span class="f10 t-mut mono">段落 ' + escapeHtml(truncate(text(it.paragraphRef), 28)) + '</span>'
              : '') +
            (text(it.mediaItemId)
              ? '<span class="f10 t-mut mono" title="' + escapeHtml(text(it.mediaItemId)) +
                '">媒体 ' + escapeHtml(shortId(it.mediaItemId)) + '</span>'
              : '') +
            (text(it.selectedText)
              ? '<span class="f10 t-mut">选中「' + escapeHtml(truncate(text(it.selectedText), 50)) + '」</span>'
              : '') +
            (suggested
              ? '<span class="f10 t-mut">AI 建议分类 ' +
                escapeHtml(CATEGORY_LABEL[suggested] || suggested) + ' · 运营已确认为 ' +
                escapeHtml(CATEGORY_LABEL[text(it.category)] || text(it.category) || '—') + '</span>'
              : '') +
            '<span class="f10 t-fai mono">' +
              escapeHtml(versionLabel(it.observedVersionId, index)) + ' → ' +
              escapeHtml(versionLabel(it.resolvedVersionId, index)) + ' · ' +
              escapeHtml(fmtTime(it.createdAt)) + '</span>' +
          '</div>' +
          '<div class="col" style="align-items:flex-end;gap:4px">' +
            (stMeta ? tag(stMeta.label, stMeta.cls) : tag(text(it.state) || '状态未标注', 'tag-muted')) +
            '<span class="f10 t-fai mono" title="ETag ' + escapeHtml(feedbackEtag(it)) + '">' +
              escapeHtml(shortId(it.id)) + '</span>' +
            '<button class="btn btn-sm btn-ghost" data-action="open-advance" ' +
              'data-feedback-id="' + escapeHtml(text(it.id)) + '">推进状态</button>' +
          '</div>' +
        '</div>' +
        (text(it.state) === 'REJECTED'
          ? '<div class="note note-warn" style="margin-top:6px"><i class="fa-solid fa-ban"></i>' +
            '<div><b>不采纳说明：</b>' +
            escapeHtml(truncate(text(it.rejectReason) || '（空 —— 后端保证 REJECTED 必有理由，此处属异常）', 300)) +
            '</div></div>'
          : '') +
        '</div>';
    }).join('');

    return card('意见列表', list.length + ' 条 · 新到旧',
      body +
      (list.length === 100
        ? note('warn', '已取满 limit=100。', '本端点是真游标分页，还有更多意见未显示。')
        : '') +
      note('info', 'STALE 的意见照常列出，条数不因锚点失效而减少。',
        '锚点新鲜度是**读时判定**、不落库（feedback/service.py:112-113）；' +
        'R5 规定 STALE 只打标记不过滤（service.py:433-436）—— 「当时有人提过」是历史事实。'));
  }

  function renderFeedbackCounters(items) {
    var t = tallyFeedback(items);
    function metric(label, value, foot, badge) {
      return '<div class="card metric">' +
        '<div class="metric-label">' + escapeHtml(label) + (badge ? ' ' + badge : '') + '</div>' +
        '<div class="metric-value">' + escapeHtml(text(value)) + '</div>' +
        '<div class="metric-foot f10 t-mut">' + escapeHtml(foot) + '</div></div>';
    }

    return '<div class="grid g4">' +
      metric('意见总数', t.total, 'feedback 列表条数') +
      metric('待处置', t.pending, 'state ∉ {RESOLVED, REJECTED}') +
      metric('未解决阻断', t.unresolvedBlocking, 'BLOCKING 且未处置 · 与后端 SQL 同口径') +
      metric('锚点已失效', t.stale, 'anchorState === STALE（读时判定）') +
      '</div>';
  }

  /** 唯一能真算的那条关卡，与后端 service.py:874-878 同口径。 */
  function renderBlockingGate(items) {
    var blocking = unresolvedBlocking(items);
    var ok = blocking.length === 0;
    return card('关卡 · 无待处理阻断意见',
      '可真算 · 与后端 SQL 判据同口径',
      '<div class="row" style="gap:10px;align-items:flex-start">' +
      '<i class="fa-solid fa-' + (ok ? 'circle-check t-ok' : 'circle-xmark t-dan') + '"></i>' +
      '<div class="col" style="flex:1;gap:4px">' +
        '<span class="f12 fw6">' + (ok ? '已满足：没有未解决的阻断意见。'
          : '未满足：有 ' + blocking.length + ' 条未解决的阻断意见。') + '</span>' +
        (ok ? ''
          : '<span class="f10 t-mut mono">' +
            escapeHtml(blocking.map(function (b) { return shortId(b.id); }).join('、')) + '</span>') +
        '<span class="f10 t-mut">判据：severity === BLOCKING 且 state 不在 {RESOLVED, REJECTED} 里。' +
          '标记「已通过」时服务端用同一条 SQL 判断，不满足就回 409 INVALID_TRANSITION' +
          '（details.reason = UNRESOLVED_BLOCKING_FEEDBACK）。</span>' +
      '</div></div>');
  }

  /** 4 条关卡：1 真 3 降级（降级 D2 / D6 / D4）。 */
  function renderGateChecks(items, contents) {
    var blocking = unresolvedBlocking(items);
    var list = rows(items);
    var pending = list.filter(function (it) {
      return SETTLED_STATES.indexOf(text(it.state)) === -1;
    });

    var checks = [
      {
        label: '无待处理阻断意见',
        ok: blocking.length === 0,
        detail: blocking.length === 0 ? '没有未解决的阻断意见' : blocking.length + ' 条未解决',
        live: true
      },
      {
        label: '全部意见完成处置',
        ok: pending.length === 0,
        detail: pending.length === 0 ? '没有待处置意见' : pending.length + ' 条待处置',
        live: true
      },
      {
        label: '每个方向至少 1 条通过',
        ok: null,
        detail: '无 scene / 方向字段，且审核结论不可读',
        live: false
      },
      {
        label: '全部完成 AI 自检',
        ok: null,
        detail: 'selfCheckSummary 恒为 null（WP30 未交付）',
        live: false
      }
    ];

    var body = checks.map(function (c) {
      var icon = c.live
        ? (c.ok ? 'circle-check t-ok' : 'circle-xmark t-dan')
        : 'circle-question t-fai';
      return '<div class="row between" style="gap:8px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.05)">' +
        '<span class="row" style="gap:7px;align-items:center">' +
          '<i class="fa-solid fa-' + icon + '"></i>' +
          '<span class="f12">' + escapeHtml(c.label) + '</span></span>' +
        '<span class="row" style="gap:6px;align-items:center">' +
          '<span class="f10 t-mut">' + escapeHtml(c.detail) + '</span>' +
          (c.live ? tag(c.ok ? '已满足' : '未满足', c.ok ? 'tag-ok' : 'tag-danger')
            : tag('无数据源', 'tag-muted')) +
        '</span></div>';
    }).join('');

    return card('进入扩产的关卡校验', '4 条 · 2 可真算 2 无读源',
      body +
      note('warn', '后两条查不到，不等于满足。',
        '「每方向 ≥1 通过」需要两样东西：内容的 scene / 方向字段（没有），' +
        '以及审核结论（content_reviews 无任何 GET，feedback/api.py 全文只有 2 个 @router.get）。' +
        '「全部完成自检」需要 selfCheckSummary（恒为 null）。' +
        '这两条显示「无数据源」而不是「已满足」—— 显示已满足就是在替后端做承诺。' +
        '另外 content_items.state 的 4 个取值（DRAFT / STRUCTURED / GENERATED / FAILED，' +
        'content/types.py:147-150）里**没有「已通过 / 已淘汰」**，不得把 GENERATED 当已通过。'));
  }

  /** 本轮内容表。降级 D7：没有 batch 资源，「本轮」概念不成立。 */
  function renderContentsTable(contents, activeContentId) {
    var list = rows(contents);
    var head = '<thead><tr>' +
      '<th style="width:112px">内容 ID</th>' +
      '<th>标题</th>' +
      '<th class="num" style="width:62px">版本</th>' +
      '<th style="width:96px">生成状态</th>' +
      '<th style="width:108px">AI 自检</th>' +
      '<th style="width:108px">人工状态</th>' +
      '<th style="width:124px">更新时间</th>' +
      '</tr></thead>';

    var body = list.length
      ? list.map(function (row) {
          var meta = CONTENT_STATE_META[text(row.state)];
          var current = text(row.contentId) === text(activeContentId);
          return '<tr' + (current ? ' class="tr-sel"' : '') + '>' +
            '<td class="f11 mono"><a href="#" data-action="select-content" data-content-id="' +
              escapeHtml(text(row.contentId)) + '" title="' + escapeHtml(text(row.contentId)) + '">' +
              escapeHtml(shortId(row.contentId) || '—') + '</a></td>' +
            '<td class="f11" title="' + escapeHtml(text(row.title)) + '">' +
              (text(row.title)
                ? escapeHtml(truncate(text(row.title), 40))
                : '<span class="t-mut">（无标题）</span>') + '</td>' +
            '<td class="num mono f11">' +
              (text(row.currentVersionNo) ? 'v' + escapeHtml(text(row.currentVersionNo)) : '—') + '</td>' +
            '<td>' + (meta ? tag(meta.label, meta.cls)
              : tag(truncate(text(row.state) || '未标注', 18), 'tag-muted')) + '</td>' +
            '<td>' + tag('无数据源', 'tag-muted') + '</td>' +
            '<td>' + tag('无数据源', 'tag-muted') + '</td>' +
            '<td class="f11 mono">' + escapeHtml(fmtTime(row.updatedAt) || '—') + '</td>' +
            '</tr>';
        }).join('')
      : '<tr><td colspan="7">' + emptyBlock('这个项目下还没有内容条目。',
          'GET /projects/{id}/contents 返回空数组') + '</td></tr>';

    return card('项目下的内容（最近 ' + list.length + ' 条）',
      'GET /projects/{id}/contents · 7 列中 4 列真 3 列降级',
      '<div class="table-wrap"><table class="tbl tbl-compact">' + head +
      '<tbody>' + body + '</tbody></table></div>' +
      note('warn', '这不是「本轮 N 条」—— 没有批次概念。',
        'GET /projects/{id}/contents 只有 limit + cursor 两个参数，' +
        '契约 86 paths 里 batch 零命中 ⇒ 没有 batch 资源，无法按批次筛选。' +
        '所以标题写的是「项目下的内容」而不是「本轮 10 条」。' +
        '「AI 自检」列无数据（selfCheckSummary 恒 null）；' +
        '「人工状态」列无数据（审核结论不可读）—— 两列都标「无数据源」而不是留空或填 0。' +
        '「生成结果」「场景」「形式」「反馈数」四列已整列删除：无字段、补不上。') +
      (list.length === 100
        ? note('warn', '已取满 limit=100。', '项目下可能还有更多内容未显示。')
        : ''));
  }

  /** 图位条。2/5 列可接；文件名 / 尺寸 / 大小 / 来源均无字段。 */
  function renderMediaStrip(mediaList) {
    var list = rows(mediaList).slice().sort(function (a, b) {
      return Number(a.sortOrder) - Number(b.sortOrder);
    });
    if (!list.length) {
      return card('媒体图位', 'GET /contents/{id}/media',
        emptyBlock('这条内容还没有媒体。', '生成媒体后会出现在这里'));
    }

    var head = '<thead><tr>' +
      '<th class="num" style="width:56px">序号</th>' +
      '<th style="width:140px">段落对位</th>' +
      '<th style="width:104px">状态</th>' +
      '<th style="width:112px">当前版本</th>' +
      '<th>差异类型 / 说明 / 处置</th>' +
      '</tr></thead>';

    var body = list.map(function (m) {
      return '<tr>' +
        '<td class="num mono f11">' + escapeHtml(text(m.sortOrder)) + '</td>' +
        '<td class="f11 mono">' +
          (text(m.paragraphRef)
            ? escapeHtml(truncate(text(m.paragraphRef), 22))
            : '<span class="t-mut">未对位</span>') + '</td>' +
        '<td class="f11">' + escapeHtml(truncate(text(m.state) || '未标注', 18)) + '</td>' +
        '<td class="f11 mono" title="' + escapeHtml(text(m.currentVersionId)) + '">' +
          (text(m.currentVersionId)
            ? escapeHtml(shortId(m.currentVersionId))
            : '<span class="t-mut">无版本</span>') + '</td>' +
        '<td>' + tag('无数据源', 'tag-muted') + '</td>' +
        '</tr>';
    }).join('');

    return card('媒体图位', list.length + ' 个 · 按 sortOrder 升序',
      '<div class="table-wrap"><table class="tbl tbl-compact">' + head +
      '<tbody>' + body + '</tbody></table></div>' +
      note('warn', '媒体的文件名 / 尺寸 / 文件大小 / 来源都没有字段。',
        'MediaSummaryBody（media/api.py:605-618）只有 mediaId / contentId / projectId / ' +
        'state / paragraphRef / sortOrder / **currentVersionId**（注意是 Id 不是 No）。' +
        '「AI 生成 vs 人工替换」、1440×1920 这类尺寸、文件大小都要逐张请求 ' +
        'GET /media/{mediaId} 的 payload 才可能有 —— 本页不做 N+1 请求。' +
        '「差异类型四选一」（外观错 / 文图冲突 / 构图不佳 / 交付格式问题）**零字段**：' +
        '最接近的是 feedback.category 里的 MEDIA_CONSISTENCY 与 TECH_FORMAT，' +
        '只有 2/4 且语义不等价，不做映射。'));
  }

  /** 重做范围 4 + 1 选项（受控）。降级 D10：提交被 modelProfileId 挡住。 */
  function renderRegenScopeOptions(selectedScope) {
    var items = REGEN_SCOPES.map(function (s) {
      return '<button class="btn btn-sm ' + (text(selectedScope) === s ? 'btn-primary' : 'btn-ghost') +
        '" data-regen-scope="' + s + '">' + escapeHtml(REGEN_SCOPE_LABEL[s]) +
        ' <span class="mono f10 t-fai">' + s + '</span></button>';
    }).join('');

    return card('重做范围', 'POST /contents/{id}/regenerations · 受控取值域 4 个',
      '<div class="row" style="gap:6px;flex-wrap:wrap">' + items + '</div>' +
      '<div class="row" style="gap:6px;flex-wrap:wrap;margin-top:8px">' +
      '<button class="btn btn-sm btn-ghost" disabled data-regen-scope="MEDIA_ONE">' +
        '单张媒体 <span class="mono f10 t-fai">POST /media/{mediaId}/regenerations</span></button>' +
      '</div>' +
      note('warn', '选项是真的，但现在提交不了。',
        'RegenerationBody（content/api.py:463-484）三个字段**全必填**：' +
        'scope / batchKind / **modelProfileId**，且 extra="forbid"。' +
        '而 model_profiles 表在 modules/gateway/models.py:163，该模块**没有 api.py**、' +
        'main.py 里也没有 gateway_router ⇒ 前端拿不到任何合法 modelProfileId，' +
        '提交必然 422。所以这里只展示受控取值域，不启用提交。' +
        '另外两条已核实的事实：① TAGS 会被服务层折成 COPY（content/types.py:104）；' +
        '② 响应 202 带 job.jobId，但 **jobId 查不到后续状态**（modules/jobs 无 api.py），' +
        '所以提交后只能如实写「已提交，进度不可查」，不画假进度条。'),
      noSourceBadge());
  }

  /**
   * 方式 1 · 直接编辑。4 字段全可选、未传沿用上一版。
   * 预填当前值，但**只有被改动的字段**才会进 editRequest 的 body ——
   * 这一点由挂载层按「输入框值 !== 当前值」判断，纯函数这里只负责把
   * 当前值与上限渲染出来。
   */
  function renderEditForm(preview) {
    var p = preview && typeof preview === 'object' ? preview : null;
    if (!p) {
      return card('直接编辑', 'POST /contents/{id}/edits',
        emptyBlock('未获取到预览，无法编辑。'));
    }
    var tags = Array.isArray(p.tags) ? p.tags : [];
    return card('直接编辑 · 当前稿 v' + text(p.versionNo),
      'POST /contents/{id}/edits · 必带 If-Match',
      '<div class="col" style="gap:9px">' +
      '<input class="inp" id="cf-title" type="text" placeholder="标题" value="' +
        escapeHtml(text(p.title)) + '" />' +
      '<textarea class="inp" id="cf-bodytext" rows="4" placeholder="正文">' +
        escapeHtml(text(p.body)) + '</textarea>' +
      '<input class="inp" id="cf-tags" type="text" placeholder="标签（逗号分隔）" value="' +
        escapeHtml(tags.join(',')) + '" />' +
      '<div class="row" style="gap:6px;align-items:center">' +
        '<button class="btn btn-primary btn-sm" data-action="save-edit">' +
          '<i class="fa-solid fa-floppy-disk"></i> 保存为新版本</button>' +
        '<span class="f10 t-fai mono">If-Match ' + escapeHtml(text(p.etag) || '（缺 etag）') + '</span>' +
      '</div></div>' +
      note('info', '只提交被改动的字段。',
        '编辑端点的 4 个字段**全可选**，未传的沿用上一版（按字段合并）；' +
        '请求体是 extra="forbid"，多传一个键（比如 contentId）就 422。' +
        '保存成功返回 **201 + 新版本摘要**，**不返回更新后的内容** —— ' +
        '所以页面会重新 GET 一次 preview。' +
        'If-Match 取自 preview.etag（响应体里就带了一份，不必读 header）：' +
        '缺头→428、通配符 *→428、格式非法→400、不匹配→412 且回带 details.currentEtag。'));
  }

  /** 新建意见表单。所有下拉都用受控取值域 + 字数上限。 */
  function renderFeedbackForm(preview, form) {
    var p = preview && typeof preview === 'object' ? preview : {};
    var f = form && typeof form === 'object' ? form : {};
    var kind = ANCHOR_KINDS.indexOf(text(f.anchorKind)) !== -1 ? text(f.anchorKind) : 'WHOLE';

    var anchorExtra = '';
    if (kind === 'PARAGRAPH') {
      anchorExtra = '<input class="inp" id="cf-pref" type="text" maxlength="' + MAX.paragraphRef +
        '" placeholder="段落 ref（≤' + MAX.paragraphRef + ' 字，形如 p3-9a1f2b7c）" value="' +
        escapeHtml(text(f.paragraphRef)) + '" />';
    } else if (kind === 'MEDIA') {
      var media = rows(p.mediaItems).slice().sort(function (a, b) {
        return Number(a.sortOrder) - Number(b.sortOrder);
      });
      anchorExtra = '<select class="inp" id="cf-media">' +
        (media.length
          ? media.map(function (m) {
              return '<option value="' + escapeHtml(text(m.mediaItemId)) + '">#' +
                escapeHtml(text(m.sortOrder)) + ' ' + escapeHtml(shortId(m.mediaItemId)) + '</option>';
            }).join('')
          : '<option value="">（这条内容没有媒体）</option>') +
        '</select>';
    }

    return card('添加意见', 'POST /contents/{id}/feedback · 无 If-Match（新建不覆盖任何行）',
      '<div class="col" style="gap:9px">' +
      '<div class="row" style="gap:6px;flex-wrap:wrap">' +
        ANCHOR_KINDS.map(function (k) {
          return '<button class="btn btn-sm ' + (kind === k ? 'btn-primary' : 'btn-ghost') +
            '" data-anchor-kind="' + k + '">' + escapeHtml(ANCHOR_KIND_LABEL[k]) +
            ' <span class="mono f10 t-fai">' + k + '</span></button>';
        }).join('') +
      '</div>' +
      anchorExtra +
      '<select class="inp" id="cf-category">' + options(CATEGORIES, CATEGORY_LABEL, f.category) + '</select>' +
      '<select class="inp" id="cf-severity">' + options(SEVERITIES, {
        BLOCKING: '阻断', WARNING: '警告', INFO: '提示'
      }, f.severity) + '</select>' +
      '<select class="inp" id="cf-scope">' + options(SCOPES, SCOPE_LABEL, f.scope) + '</select>' +
      '<textarea class="inp" id="cf-body" rows="3" maxlength="' + MAX.body +
        '" placeholder="意见正文（≤' + MAX.body + ' 字）"></textarea>' +
      '<button class="btn btn-primary btn-sm" data-action="add-feedback">' +
        '<i class="fa-solid fa-plus"></i> 提交意见</button>' +
      '</div>' +
      note('info', '作者身份不由前端传。',
        'FeedbackCreateRequestBody 里**没有 authorKind / createdBy** —— ' +
        '端点固定记 USER + 当前登录用户。意图可以传，身份不能传。' +
        '锚点三选一且列占用互斥：整篇不带 paragraphRef / mediaItemId，' +
        '段落只带 paragraphRef，媒体只带 mediaItemId。' +
        '观测版本固定取当前稿 ' + ('v' + text(p.versionNo)) + '。'));
  }

  /**
   * 推进状态表单。渲染成「当前状态 + 四个可选目标状态」，
   * **不是单向四步进度条** —— 后端没有 from→to 合法转移表（service.py:700）。
   */
  function renderAdvanceForm(item) {
    var it = item && typeof item === 'object' ? item : null;
    if (!it) {
      return card('推进意见状态', 'PATCH /feedback/{id}',
        emptyBlock('未选中意见。', '在意见列表里点「推进状态」'));
    }

    var current = text(it.state);
    var buttons = STATES.map(function (s) {
      var meta = STATE_META[s];
      return '<button class="btn btn-sm ' + (current === s ? 'btn-ghost' : 'btn-primary') +
        '" data-action="advance" data-feedback-id="' + escapeHtml(text(it.id)) +
        '" data-to-state="' + s + '">' + escapeHtml(meta.label) +
        ' <span class="mono f10 t-fai">' + s + '</span>' +
        (current === s ? '（当前）' : '') + '</button>';
    }).join('');

    return card('推进意见状态 · ' + shortId(it.id), 'PATCH /feedback/{id} · 必带 If-Match',
      '<div class="col" style="gap:9px">' +
      '<div class="row" style="gap:6px;align-items:center">' +
        '<span class="f11 t-mut">当前状态</span>' +
        (STATE_META[current] ? tag(STATE_META[current].label, STATE_META[current].cls)
          : tag(current || '状态未标注', 'tag-muted')) +
        '<span class="f10 t-fai mono">ETag ' + escapeHtml(feedbackEtag(it) || '（缺 lockVersion，无法构造）') + '</span>' +
      '</div>' +
      '<div class="row" style="gap:6px;flex-wrap:wrap">' + buttons + '</div>' +
      '<textarea class="inp" id="cf-reject" rows="2" maxlength="' + MAX.rejectReason +
        '" placeholder="不采纳理由（仅 REJECTED 必填，≤' + MAX.rejectReason + ' 字）"></textarea>' +
      '</div>' +
      note('warn', '四个目标状态都可选，包括「回退」。',
        '后端**没有 from→to 合法转移表**：service.advance_feedback（feedback/service.py:700）' +
        '只校验 toState 在 4 个取值域里，所以 RESOLVED → OPEN 的回退、OPEN → OPEN 的自转' +
        '都会成功。页面因此不画单向进度条 —— 画了就是编造一条后端不存在的约束。' +
        '唯一的附加规则：toState === REJECTED 时 rejectReason 必填（≤500 字）；' +
        '**推进到非 REJECTED 时服务端会把 reject_reason 归一为 NULL**，' +
        '即「改判为已解决」会清空之前的不采纳理由。'));
  }

  /** 审核动作（单条 + 批量）。 */
  function renderReviewActions(preview, contents) {
    var p = preview && typeof preview === 'object' ? preview : {};
    var list = rows(contents);
    var single = DECISIONS.map(function (d) {
      return '<button class="btn btn-sm ' + (d === 'APPROVED' ? 'btn-primary' : 'btn-ghost') +
        '" data-action="review" data-decision="' + d + '">' +
        escapeHtml(DECISION_LABEL[d]) + ' <span class="mono f10 t-fai">' + d + '</span></button>';
    }).join('');

    return card('审核结论', 'POST /contents/{id}/reviews（201）/ POST /projects/{id}/content-reviews（200）',
      '<div class="col" style="gap:9px">' +
      '<div class="row" style="gap:6px;flex-wrap:wrap">' + single + '</div>' +
      '<input class="inp" id="cf-reason" type="text" placeholder="审核理由（必填）" />' +
      '<button class="btn btn-sm btn-ghost" data-action="batch-review" data-decision="APPROVED">' +
        '批量通过项目下 ' + list.length + ' 条内容</button>' +
      '</div>' +
      note('warn', '写得进去，读不回来。',
        'content_reviews 有表（feedback/models.py:336）、有这两个写端点，' +
        '但**没有任何 GET** —— 所以提交后页面无法回显「已通过 N 条 / 已淘汰 N 条」，' +
        '只能显示这一次提交的返回值。' +
        '另外：标记 APPROVED 会被未解决的 BLOCKING 意见挡住（409 INVALID_TRANSITION，' +
        'details.blockingFeedbackIds 回带是哪几条）—— 这不是前端校验，是服务端的硬规则，' +
        '所以页面不预先禁用按钮，而是把 409 的真实原因展示出来。' +
        '批量端点**不传版本**：服务端按每条内容的「当前版本」逐条绑定，返回 200 + ' +
        'results[{contentItemId, ok, review?, error?}]，其中 error 是**人类可读诊断串，不是错误码**。'));
  }

  function renderBatchReviewResult(result) {
    var r = result && typeof result === 'object' ? result : null;
    if (!r) return '';
    var results = rows(r.results);
    if (!results.length) {
      return note('info', '批量审核已提交。', '服务端返回的 results 为空数组。');
    }
    var okCount = results.filter(function (x) { return x.ok === true; }).length;
    var body = results.map(function (x) {
      return '<tr' + (x.ok === true ? '' : ' class="tr-danger"') + '>' +
        '<td class="f11 mono" title="' + escapeHtml(text(x.contentItemId)) + '">' +
          escapeHtml(shortId(x.contentItemId) || '—') + '</td>' +
        '<td>' + (x.ok === true ? tag('成功', 'tag-ok') : tag('失败', 'tag-danger')) + '</td>' +
        '<td class="f11">' +
          (x.ok === true
            ? escapeHtml(shortId((x.review && x.review.id) || '') || '—')
            : escapeHtml(truncate(text(x.error) || '（未给诊断信息）', 90))) + '</td>' +
        '</tr>';
    }).join('');

    return card('批量审核结果', okCount + ' / ' + results.length + ' 成功',
      '<div class="table-wrap"><table class="tbl tbl-compact">' +
      '<thead><tr><th>内容</th><th style="width:72px">结果</th>' +
      '<th>审核 ID / 诊断</th></tr></thead><tbody>' + body + '</tbody></table></div>' +
      note('info', '失败行里的文字是诊断串，不是错误码。',
        'ReviewBatchItemResponseBody.error（feedback/schemas.py:345）是人类可读的诊断信息，' +
        '不要据此做分支判断。'));
  }

  function renderEditResult(version) {
    var v = version && typeof version === 'object' ? version : null;
    if (!v) return '';
    return note('info', '已保存为新版本 v' + text(v.versionNo) + '。',
      '编辑端点返回的是**新版本摘要**（id ' + shortId(v.id) + ' · payloadHash ' +
      truncate(text(v.payloadHash), 12) + ' · schemaVersion ' + text(v.schemaVersion) +
      '），**不返回更新后的内容** —— 要看新内容需要重新 GET preview，' +
      '页面已自动刷新。');
  }

  function renderRegenResult(accepted) {
    var a = accepted && typeof accepted === 'object' ? accepted : null;
    if (!a) return '';
    var job = a.job && typeof a.job === 'object' ? a.job : {};
    return note('warn', '已提交重做，进度不可查。',
      '范围 ' + text(a.scope) + ' · 任务类型 ' +
      (Array.isArray(a.taskKinds) ? a.taskKinds.join('/') : '—') +
      ' · jobId ' + shortId(job.jobId) +
      (job.created === false ? '（幂等命中，复用了排队中的任务）' : '') +
      '。jobId 拿到了，但 modules/jobs 没有读端点 ⇒ 查不到它的后续状态，' +
      '所以这里不画进度条。');
  }

  /** 素材上传（降级 D9）。 */
  function renderUploadUnavailable() {
    return card('上传替换素材', '本批不做写',
      note('info', '上传是 4 步分片流程，演示环境不提供。',
        'POST /projects/{id}/uploads → PUT /uploads/{id}/parts/{n} → ' +
        'POST /uploads/{id}/completions，加上幂等键与完成回调。' +
        '读端已接（见「媒体图位」卡），写端单独估 1 人日，建议另开一波做。'),
      noSourceBadge());
  }

  /** 方向通过情况 / 轮次对比（降级 D6 / D7）。 */
  function renderDirectionUnavailable() {
    return card('方向通过情况 / 轮次对比', '两块均无读源',
      note('warn', '没有 scene 字段，也没有 batch 资源。',
        '「方向」在内容侧完全没有字段（ContentSummaryBody 只有 contentId / projectId / ' +
        'title / state / currentVersionNo / createdAt / updatedAt），' +
        '「轮次 / 批次」在契约 86 paths 里 batch 零命中。' +
        '再加上审核结论不可读，「某方向 N 条通过」三个要素一个都不具备。' +
        '原型里 5 条方向进度与 BATCH-T01 / BATCH-T02 的全部数字已删除。'),
      noSourceBadge());
  }

  /** 教义文案（保留静态）。 */
  function renderReviewDoctrine() {
    return card('审核纪律', '',
      '<div class="col f12 t-mut" style="gap:8px">' +
      '<span>· 预览不替代真实设备验收。</span>' +
      '<span>· 关键事实问题未解决不能通过 —— 这一条后端有硬规则兜底（409 + blockingFeedbackIds）。</span>' +
      '<span>· 不允许仅点击「全部通过」绕过逐条处置。</span>' +
      '<span>· 差异类型须四选一 —— 但这四类在后端没有字段，见「媒体图位」卡的说明。</span>' +
      '</div>', doctrineBadge());
  }

  /* ---------------------------- 加载 / 空 / 错误 ---------------------------- */

  function renderLoading(label) {
    return '<section class="card"><div class="card-head"><span class="card-title">读取中</span></div>' +
      '<div class="card-body"><div class="row" style="gap:8px">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">' + escapeHtml(text(label) || '正在读取内容与意见…') + '</span>' +
      '</div></div></section>';
  }

  function renderEmpty(message) {
    return card('暂无数据', '', emptyBlock(text(message) || '没有可展示的内容。'));
  }

  /** 错误态卡 —— 分支文案由 describeError 给出，含 412/428/409/400 四条路径。 */
  function renderError(err) {
    var d = describeError(err);
    return '<section class="card"><div class="card-head">' +
      '<span class="card-title">操作未完成</span>' +
      '<span class="card-sub">' + escapeHtml(d.kind) + '</span></div>' +
      '<div class="card-body"><div class="row" style="align-items:flex-start;gap:10px">' +
      '<i class="fa-solid fa-triangle-exclamation t-dan"></i>' +
      '<div class="col" style="flex:1;gap:4px">' +
      '<span class="f12 fw6">' + escapeHtml(d.title) + '</span>' +
      '<span class="f10 t-mut">' + escapeHtml(d.hint) + '</span>' +
      '</div>' +
      '<button class="btn btn-sm btn-ghost" data-action="retry">' +
      '<i class="fa-solid fa-rotate"></i> 重试</button>' +
      '</div></div></section>';
  }

  return {
    /* 请求描述符（本页关键设计） */
    feedbackEtag: feedbackEtag,
    editRequest: editRequest,
    createFeedbackRequest: createFeedbackRequest,
    advanceRequest: advanceRequest,
    reviewRequest: reviewRequest,
    batchReviewRequest: batchReviewRequest,
    regenerateRequest: regenerateRequest,
    describeError: describeError,
    /* 纯计算 */
    indexVersions: indexVersions,
    versionLabel: versionLabel,
    unresolvedBlocking: unresolvedBlocking,
    tallyFeedback: tallyFeedback,
    /* 渲染 */
    renderPreviewPhone: renderPreviewPhone,
    renderParagraphAnchors: renderParagraphAnchors,
    renderMetaFields: renderMetaFields,
    renderSelfCheckGrid: renderSelfCheckGrid,
    renderFeedbackList: renderFeedbackList,
    renderFeedbackCounters: renderFeedbackCounters,
    renderBlockingGate: renderBlockingGate,
    renderGateChecks: renderGateChecks,
    renderContentsTable: renderContentsTable,
    renderMediaStrip: renderMediaStrip,
    renderRegenScopeOptions: renderRegenScopeOptions,
    renderEditForm: renderEditForm,
    renderFeedbackForm: renderFeedbackForm,
    renderAdvanceForm: renderAdvanceForm,
    renderReviewActions: renderReviewActions,
    renderBatchReviewResult: renderBatchReviewResult,
    renderEditResult: renderEditResult,
    renderRegenResult: renderRegenResult,
    renderUploadUnavailable: renderUploadUnavailable,
    renderDirectionUnavailable: renderDirectionUnavailable,
    renderReviewDoctrine: renderReviewDoctrine,
    renderLoading: renderLoading,
    renderEmpty: renderEmpty,
    renderError: renderError,
    escapeHtml: escapeHtml,
    CATEGORIES: CATEGORIES,
    CATEGORY_LABEL: CATEGORY_LABEL,
    ANCHOR_KINDS: ANCHOR_KINDS,
    SEVERITIES: SEVERITIES,
    SCOPES: SCOPES,
    STATES: STATES,
    DECISIONS: DECISIONS,
    REGEN_SCOPES: REGEN_SCOPES,
    MAX: MAX
  };
});
