/* ============================================================================
 * render/feedback-fix.js —— feedback-fix 页渲染层（方案 A：数据 → HTML 字符串）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读 DOM、不读浏览器全局、不做 I/O、
 * 不取「现在几点」。因此可直接 Node 单测。
 *
 * 双环境导出：
 *   Node/CommonJS  → require('./feedback-fix.js')
 *   浏览器          → RednoteRender.feedbackFix
 *
 * ---------------------------------------------------------------------------
 * 数据来源（WP29，读的是后端源码；**不以 contracts/openapi.json 为准** ——
 * tools/contracts/export_openapi.py:59 _strip_titles() 会删掉所有名为 title 的
 * 响应字段，ContentVersionBody.title 在快照里缺失但运行时真有）：
 *
 *   GET /contents/{contentId}/feedback        feedback/api.py:507
 *     → PaginatedEnvelope[FeedbackItemResponseBody]  feedback/schemas.py:279-312
 *       { id, contentItemId, projectId, observedVersionId, anchorKind, anchorState,
 *         paragraphRef, selectedText, mediaItemId, category, suggestedCategory,
 *         severity, scope, state, authorKind, body, rejectReason,
 *         resolvedVersionId, createdBy, createdAt, updatedAt, lockVersion }
 *       真游标分页（服务层 keyset，created_at DESC, id DESC）。
 *       筛选参数只有 state / category / anchorKind（feedback/api.py:520-522）——
 *       **没有 severity 筛选**，所以阻断过滤必须在本模块里按字段做。
 *       R5：anchorState==='STALE' 照常返回、不过滤（feedback/service.py:433-436）
 *       ⇒ 页面必须展示 STALE 标记，不得隐藏。
 *
 *   GET /contents/{contentId}/versions        content/api.py:1127
 *     → PaginatedEnvelope[ContentVersionBody]        content/api.py:682-710
 *       { versionId, versionNo, title, blueprintId, adoptedTitleCandidateId,
 *         generationContextId, payloadHash, schemaVersion, createdAt, createdBy }
 *       **不含正文**（content/api.py:692 原文：不给 body / bindings / payload）。
 *
 * 受控取值域（feedback/types.py，页面标签一律用这几组，不自创）：
 *   anchorKind  WHOLE / PARAGRAPH / MEDIA            types.py:35-37
 *   category    FACT TONE STRUCTURE SCENE DUPLICATE
 *               MEDIA_CONSISTENCY TECH_FORMAT OTHER  types.py:49-56
 *   severity    BLOCKING / WARNING / INFO            types.py:77-79
 *   scope       CURRENT_ONLY / FUTURE_GENERATIONS    types.py:95-96
 *   state       OPEN / IN_PROGRESS / RESOLVED / REJECTED  types.py:108-111
 *   anchorState FRESH / STALE（读时判定、不落库）    service.py:112-113
 *
 * ---------------------------------------------------------------------------
 * 降级登记（每条写明理由）：
 *
 *   D1 「原因确认」4 候选根因表 + issue_code（FAC-01）+ root_cause
 *      （生成未遵守已批准基线）—— 契约里 root_cause / issue_code **零命中**，
 *      零字段。最接近但**不等价**的真字段是 category（8 类，是「哪类问题」
 *      不是「根因」）、suggestedCategory、rejectReason。
 *      ⇒ 整表保持静态教义 + 卡头标「标准文本 · 无数据源」，
 *        4 条「已排除 / 已确认」结论一律删掉（那是对数据的声称）。
 *   D2 「同类根因近 30 天频次」5 条柱 —— 可做同口径替代（按 category 聚合
 *      本条内容的反馈条数），但**必须改名**：它是分类分布、不是根因频次；
 *      且只能按单条内容（没有跨内容的 feedback 查询端点），
 *      也**没有时间窗筛选参数**（无 since）。条宽由聚合结果算，
 *      原型里写死的 data-w="70%" 一律不要。
 *   D3 「第 1 轮 / 最多 2 轮」—— 无轮次/重试上限字段。jobs.attempt 有这个
 *      语义但 modules/jobs 无 api.py、无 jobs_router ⇒ 静态 + 标注。
 *   D4 「需一并记录的事件」4 类 —— 只有「不采纳修改建议」有源
 *      （state==='REJECTED' + rejectReason）。「被淘汰」对应
 *      decision==='ELIMINATED'，但 content_reviews 没有任何 GET
 *      （feedback/api.py 全文只有 2 个 @router.get）⇒ 审核只能写不能读。
 *      「无修改通过」「优秀初稿」无字段。
 *   D5 「BL-v3」生产基线编号 —— preview 不返回 baselineId，无字段。
 *      两张作用范围卡的正文（「作为附加约束写入 PLAN 版本」「不修改生产基线」）
 *      是教义，保留静态。
 *   D6 「批次 BATCH-T01」—— GET /projects/{id}/contents 只有 limit + cursor，
 *      契约里 batch 零命中 ⇒ 无 batch 资源，不显示批次号。
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.feedbackFix = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  /* 受控取值域（展示序；feedback/types.py 的 frozenset 无序，SQL 用 sorted） */
  var CATEGORIES = ['FACT', 'TONE', 'STRUCTURE', 'SCENE', 'DUPLICATE',
    'MEDIA_CONSISTENCY', 'TECH_FORMAT', 'OTHER'];

  var CATEGORY_LABEL = {
    FACT: '事实', TONE: '语气', STRUCTURE: '结构', SCENE: '场景',
    DUPLICATE: '重复', MEDIA_CONSISTENCY: '文图一致', TECH_FORMAT: '技术格式', OTHER: '其他'
  };

  var STATE_META = {
    OPEN: { label: '待处理', cls: 'tag-warn', order: 0 },
    IN_PROGRESS: { label: '处理中', cls: 'tag-purple', order: 1 },
    RESOLVED: { label: '已解决', cls: 'tag-ok', order: 2 },
    REJECTED: { label: '不采纳', cls: 'tag-danger', order: 3 }
  };

  var SEVERITY_META = {
    BLOCKING: { label: '阻断', cls: 'tag-danger' },
    WARNING: { label: '警告', cls: 'tag-warn' },
    INFO: { label: '提示', cls: '' }
  };

  var SCOPE_META = {
    CURRENT_ONLY: { label: '仅本条', desc: '只修这一条内容的当前稿，不影响后续生成。' },
    FUTURE_GENERATIONS: { label: '应用后续生成', desc: '作为附加约束写入 PLAN 版本，影响本批次后续生成；不修改生产基线。' }
  };

  var ANCHOR_KIND_LABEL = { WHOLE: '整篇', PARAGRAPH: '段落', MEDIA: '媒体' };

  /** 未解决 = 不在 RESOLVED / REJECTED 两个终态里。与后端 service.py:874-878
   *  判「未解决的 BLOCKING 反馈」用的 SQL 判据同口径。 */
  var SETTLED_STATES = ['RESOLVED', 'REJECTED'];

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

  /* ---------------------------- 纯计算 ---------------------------- */

  /** versions → {versionId: versionNo}。用于把 resolvedVersionId 翻成「v3」。 */
  function indexVersions(versions) {
    var index = {};
    rows(versions).forEach(function (v) {
      var id = text(v.versionId);
      if (id && (v.versionNo !== null && v.versionNo !== undefined)) index[id] = v.versionNo;
    });
    return index;
  }

  /**
   * 「解决于 v{n}」标签。
   * resolvedVersionId 为空 → '—'（未解决，或解决时没登记版本）；
   * 有 id 但 versions 里查不到 → 回退成短 id，**不猜版本号**。
   */
  function resolveVersionLabel(item, index) {
    var it = item && typeof item === 'object' ? item : {};
    var id = text(it.resolvedVersionId);
    if (!id) return '—';
    var map = index && typeof index === 'object' ? index : {};
    if (Object.prototype.hasOwnProperty.call(map, id)) return 'v' + map[id];
    return shortId(id);
  }

  /** 「观测于 v{n}」—— observedVersionId 是必填字段，一定有值。 */
  function observedVersionLabel(item, index) {
    var it = item && typeof item === 'object' ? item : {};
    var id = text(it.observedVersionId);
    if (!id) return '—';
    var map = index && typeof index === 'object' ? index : {};
    if (Object.prototype.hasOwnProperty.call(map, id)) return 'v' + map[id];
    return shortId(id);
  }

  /**
   * 降级 D2：按 category 聚合。pct 由**最大值**归一（不是占比），
   * 与原型那组柱的视觉一致但数值来自数据。空集返回 []（调用方渲染空态，
   * 不画 0% 柱 —— 0% 柱会被读成「这一类真的是 0 条」）。
   */
  function tallyByCategory(items) {
    var list = rows(items);
    var counts = {};
    list.forEach(function (it) {
      var c = text(it.category) || '(未标注)';
      counts[c] = (counts[c] || 0) + 1;
    });

    var keys = CATEGORIES.filter(function (c) { return counts[c]; });
    Object.keys(counts).forEach(function (c) {
      if (keys.indexOf(c) === -1) keys.push(c);
    });

    var max = keys.reduce(function (a, c) { return Math.max(a, counts[c]); }, 0);
    return keys.map(function (c) {
      return {
        category: c,
        label: CATEGORY_LABEL[c] || c,
        known: CATEGORIES.indexOf(c) !== -1,
        count: counts[c],
        pct: max > 0 ? counts[c] / max * 100 : 0
      };
    });
  }

  /** 未解决的阻断反馈 —— severity 不是筛选参数，只能在这里按字段过滤。 */
  function unresolvedBlocking(items) {
    return rows(items).filter(function (it) {
      return text(it.severity) === 'BLOCKING' && SETTLED_STATES.indexOf(text(it.state)) === -1;
    });
  }

  function tallyByScope(items) {
    var list = rows(items);
    var out = { CURRENT_ONLY: 0, FUTURE_GENERATIONS: 0, unknown: [] };
    list.forEach(function (it) {
      var s = text(it.scope);
      if (s === 'CURRENT_ONLY' || s === 'FUTURE_GENERATIONS') out[s] += 1;
      else out.unknown.push(s || '(未标注)');
    });
    return out;
  }

  /* ---------------------------- 局部修复清单（本页最真的一块） ---------------------------- */

  function renderFixList(items, versionIndex) {
    var list = rows(items);
    if (!list.length) {
      return card('局部修复清单', 'GET /contents/{id}/feedback · 一条反馈 = 一行修复项',
        emptyBlock('这条内容还没有任何反馈。',
          '提了意见之后，每条意见都会作为一行修复项出现在这里'));
    }

    var index = versionIndex || {};
    var body = list.map(function (it) {
      var stateMeta = STATE_META[text(it.state)];
      var sevMeta = SEVERITY_META[text(it.severity)];
      var tags = [];

      tags.push(tag(CATEGORY_LABEL[text(it.category)] || (text(it.category) || '未标注分类') ,
        CATEGORY_LABEL[text(it.category)] ? '' : 'tag-muted'));
      if (sevMeta) tags.push(tag(sevMeta.label, sevMeta.cls));
      else if (text(it.severity)) tags.push(tag(truncate(text(it.severity), 16), ''));
      tags.push(tag(ANCHOR_KIND_LABEL[text(it.anchorKind)] || (text(it.anchorKind) || '未标注粒度'),
        ANCHOR_KIND_LABEL[text(it.anchorKind)] ? '' : 'tag-muted'));
      if (SCOPE_META[text(it.scope)]) tags.push(tag(SCOPE_META[text(it.scope)].label, ''));
      /* R5：STALE 必须显式展示，不得隐藏（service.py:433-436） */
      if (text(it.anchorState) === 'STALE') {
        tags.push(tag('锚点已失效 STALE', 'tag-warn'));
      }
      if (text(it.authorKind) === 'AI') tags.push(tag('AI 提出', 'tag-purple'));

      var stateTag = stateMeta
        ? tag(stateMeta.label, stateMeta.cls)
        : tag(text(it.state) || '状态未标注', 'tag-muted');

      var lines = [];
      /* 「第 N 张图」：anchorKind==='MEDIA' 时 mediaItemId 必有值 */
      if (text(it.anchorKind) === 'MEDIA' && text(it.mediaItemId)) {
        lines.push('<span class="f10 t-mut mono" title="' + escapeHtml(text(it.mediaItemId)) +
          '">媒体 ' + escapeHtml(shortId(it.mediaItemId)) + '</span>');
      }
      if (text(it.paragraphRef)) {
        lines.push('<span class="f10 t-mut mono">段落 ' +
          escapeHtml(truncate(text(it.paragraphRef), 24)) + '</span>');
      }
      if (text(it.selectedText)) {
        lines.push('<span class="f10 t-mut">选中「' +
          escapeHtml(truncate(text(it.selectedText), 40)) + '」</span>');
      }
      lines.push('<span class="f10 t-fai mono">观测于 ' +
        escapeHtml(observedVersionLabel(it, index)) + ' · 解决于 ' +
        escapeHtml(resolveVersionLabel(it, index)) + '</span>');
      lines.push('<span class="f10 t-fai mono">' + escapeHtml(fmtTime(it.createdAt)) + '</span>');

      var suggested = text(it.suggestedCategory);
      var suggestedRow = suggested
        ? '<div class="f10 t-mut" style="margin-top:4px">AI 建议分类 ' +
          escapeHtml(CATEGORY_LABEL[suggested] || suggested) +
          ' · 运营已确认为 ' +
          escapeHtml(CATEGORY_LABEL[text(it.category)] || text(it.category) || '—') + '</div>'
        : '';

      var rejected = text(it.state) === 'REJECTED'
        ? '<div class="note note-warn" style="margin-top:6px"><i class="fa-solid fa-ban"></i>' +
          '<div><b>不采纳说明：</b>' + escapeHtml(truncate(text(it.rejectReason) || '（后端保证 REJECTED 必有理由，此处为空属异常）', 300)) +
          '</div></div>'
        : '';

      return '<div class="fix-row">' +
        '<div class="row between" style="align-items:flex-start;gap:10px">' +
          '<div class="col" style="flex:1;gap:5px">' +
            '<span class="f12">' + escapeHtml(truncate(text(it.body) || '（反馈正文为空）', 220)) + '</span>' +
            '<div class="row" style="gap:6px;flex-wrap:wrap">' + tags.join('') + '</div>' +
            '<div class="row" style="gap:10px;flex-wrap:wrap">' + lines.join('') + '</div>' +
            suggestedRow +
          '</div>' +
          '<div class="col" style="align-items:flex-end;gap:4px">' + stateTag +
            '<span class="f10 t-fai mono" title="' + escapeHtml(text(it.id)) + '">' +
            escapeHtml(shortId(it.id)) + '</span>' +
          '</div>' +
        '</div>' + rejected + '</div>';
    }).join('');

    var blocking = unresolvedBlocking(list);
    return card('局部修复清单',
      'GET /contents/{id}/feedback · ' + list.length + ' 条',
      (blocking.length
        ? note('warn', '有 ' + blocking.length + ' 条未解决的阻断意见。',
            '与后端 service.py:874-878 同口径（severity==="BLOCKING" 且 state 不在 ' +
            'RESOLVED / REJECTED 里）。标记「已通过」会被 409 INVALID_TRANSITION 挡住，' +
            'details.reason = UNRESOLVED_BLOCKING_FEEDBACK。')
        : '') +
      body +
      (list.length === 100
        ? note('warn', '已取满 limit=100。', '本端点是真游标分页，还有更多反馈未显示。')
        : ''));
  }

  /* ---------------------------- 作用范围 ---------------------------- */

  function renderScopeCards(items) {
    var tallies = tallyByScope(items);
    var cards = ['CURRENT_ONLY', 'FUTURE_GENERATIONS'].map(function (key) {
      var meta = SCOPE_META[key];
      return '<div class="card metric">' +
        '<div class="metric-label">' + escapeHtml(meta.label) +
          ' <span class="mono f10 t-fai">' + key + '</span></div>' +
        '<div class="metric-value">' + tallies[key] + '<span class="unit"> 条</span></div>' +
        '<div class="metric-foot f10 t-mut">' + escapeHtml(meta.desc) + '</div></div>';
    }).join('');

    return card('作用范围',
      'feedback.scope 受控取值域 CURRENT_ONLY / FUTURE_GENERATIONS',
      '<div class="grid g2">' + cards + '</div>' +
      (tallies.unknown.length
        ? note('warn', '出现受控取值域外的 scope：',
            Array.from(new Set(tallies.unknown)).join('、') + '，未计入上面两格。')
        : '') +
      note('info', '两张卡的说明文字是教义，不是接口字段。',
        '「作为附加约束写入 PLAN 版本」「不修改生产基线」是流程纪律；' +
        '生产基线编号（原型里的 BL-v3）在 preview 与 feedback 两个响应里都没有字段。'));
  }

  /* ---------------------------- 分类分布（降级 D2） ---------------------------- */

  function renderCategoryTally(items) {
    var tally = tallyByCategory(items);
    if (!tally.length) {
      return card('本条内容的反馈分类分布',
        '替代原型的「同类根因近 30 天频次」',
        emptyBlock('这条内容还没有反馈，无分布可算。',
          '空集不画 0% 柱 —— 0% 会被读成「这一类真的是 0 条」'),
        noSourceBadge());
    }

    var body = tally.map(function (t) {
      return '<div class="bar-row">' +
        '<span class="f11 t-mut" style="width:112px">' + escapeHtml(t.label) +
          (t.known ? '' : '<span class="t-fai">（取值域外）</span>') + '</span>' +
        '<span class="bar"><span class="bar-fill" style="width:' + t.pct.toFixed(1) + '%"></span></span>' +
        '<span class="bar-num f11">' + t.count + ' 条</span></div>';
    }).join('');

    return card('本条内容的反馈分类分布',
      '替代原型的「同类根因近 30 天频次」—— 改名是必须的，见下',
      body +
      note('warn', '口径三点差异，必须说清。',
        '① 这是**分类分布**（feedback.category 的 8 类受控取值域），不是「根因频次」——' +
        'root_cause / issue_code 两个字段在契约里零命中；' +
        '② 范围只到**本条内容**：没有跨内容 / 跨项目的 feedback 查询端点；' +
        '③ **没有 30 天窗口**：GET /contents/{id}/feedback 的筛选参数只有 ' +
        'state / category / anchorKind，没有 since 或任何时间窗参数。' +
        '条宽由上面的计数按最大值归一算出，不是写死的百分比。'));
  }

  /* ---------------------------- 不采纳清单（降级 D4 里唯一有源的一类） ---------------------------- */

  function renderRejectedList(items) {
    var list = rows(items).filter(function (it) { return text(it.state) === 'REJECTED'; });
    if (!list.length) {
      return card('不采纳的修改建议', 'state === REJECTED · rejectReason 必有值',
        emptyBlock('当前没有被标为「不采纳」的意见。',
          '这是真的 0 条 —— state 是真字段，过滤出来确实为空'));
    }

    var body = list.map(function (it) {
      return '<div class="row" style="align-items:flex-start;gap:8px;padding:9px 0;border-bottom:1px solid rgba(255,255,255,0.05)">' +
        '<i class="fa-solid fa-ban t-dan f11" style="margin-top:3px"></i>' +
        '<div class="col" style="flex:1;gap:3px">' +
          '<span class="f12">' + escapeHtml(truncate(text(it.body), 160)) + '</span>' +
          '<span class="f10 t-mut">不采纳理由：' +
            escapeHtml(truncate(text(it.rejectReason) || '（空 —— 后端保证非空，此处为异常）', 240)) + '</span>' +
        '</div></div>';
    }).join('');

    return card('不采纳的修改建议',
      'state === REJECTED · ' + list.length + ' 条',
      body +
      note('info', '「推进到非 REJECTED 状态」会把 rejectReason 归一为 NULL。',
        '服务端行为（feedback/service.py:700 advance_feedback）：to_state === REJECTED 时 ' +
        'rejectReason 必填（≤500 字），其余目标态下 reject_reason 被置 NULL —— ' +
        '即「改判为已解决」会清空之前的不采纳理由。'));
  }

  /* ---------------------------- 静态教义区 ---------------------------- */

  /** 降级 D1：4 候选根因表零字段 —— 保留表格骨架作为方法论，删掉所有结论。 */
  function renderRootCauseDoctrine() {
    var candidates = [
      ['生成未遵守已批准基线', '生产基线 → 生成请求', '对照基线条款与生成约束'],
      ['基线本身写错', '生产基线条款', '回到信息审核与可用主张'],
      ['研究资料有误', '研究结论 / 来源', '回到资料与缺口'],
      ['Brief 口径本身有歧义', 'Brief 结构化字段', '回到 Brief 解析与加厚']
    ];
    var body = '<div class="table-wrap"><table class="tbl tbl-compact">' +
      '<thead><tr><th>候选根因</th><th>修复位置</th><th>对照方式</th><th style="width:96px">结论</th></tr></thead><tbody>' +
      candidates.map(function (c) {
        return '<tr><td class="f11">' + escapeHtml(c[0]) + '</td>' +
          '<td class="f11 t-mut">' + escapeHtml(c[1]) + '</td>' +
          '<td class="f11 t-mut">' + escapeHtml(c[2]) + '</td>' +
          '<td><span class="tag tag-muted no-dot f10">无数据源</span></td></tr>';
      }).join('') +
      '</tbody></table></div>';

    return card('原因确认（四候选根因）', '方法论文本',
      body +
      note('warn', '「结论」一列无法由接口填。',
        'root_cause / issue_code 在契约 86 paths 的全部 schema 里零命中，' +
        '后端没有「根因」这个概念的字段。最接近但**语义不等价**的真字段是 ' +
        'feedback.category（8 类受控取值域，答的是「哪一类问题」而不是「为什么会这样」）、' +
        'suggestedCategory（AI 建议分类）与 rejectReason。' +
        '原型里那 4 条写死的排查结论、以及写死的问题编码与根因文本，已全部删除 —— ' +
        '那是对数据的声称，而数据不存在。'),
      doctrineBadge());
  }

  /** 降级 D3：轮次上限无字段。 */
  function renderRoundNotice() {
    return card('修复轮次', '无数据源',
      note('warn', '「第 N 轮 / 最多 2 轮」无字段。',
        '轮次与重试上限的语义在 jobs 表里有（modules/jobs/models.py:185 的 attempt 列、' +
        ':283 job_attempts 的 attempt_no），但 modules/jobs 下没有 api.py、' +
        'main.py 里没有 jobs_router，契约里 /jobs 前缀零命中 ⇒ 读不到。' +
        '「最多 2 轮」本身是流程纪律，保留为说明文字，但不显示「当前第几轮」。'),
      noSourceBadge());
  }

  /** 降级 D4：需一并记录的 4 类事件，1 有源 3 无源。 */
  function renderEventRecordDoctrine(items) {
    var rejected = rows(items).filter(function (it) { return text(it.state) === 'REJECTED'; }).length;
    var lines = [
      ['不采纳修改建议', 'state === REJECTED + rejectReason', String(rejected) + ' 条', true],
      ['无修改即通过', '无字段', '无数据源', false],
      ['优秀初稿', '无字段', '无数据源', false],
      ['内容被淘汰', 'decision === ELIMINATED（只能写不能读）', '无数据源', false]
    ];

    var body = '<div class="table-wrap"><table class="tbl tbl-compact">' +
      '<thead><tr><th>需记录的事件</th><th>对应字段</th><th style="width:104px">当前</th></tr></thead><tbody>' +
      lines.map(function (l) {
        return '<tr><td class="f11">' + escapeHtml(l[0]) + '</td>' +
          '<td class="f11 t-mut mono">' + escapeHtml(l[1]) + '</td>' +
          '<td>' + (l[3]
            ? '<span class="tag tag-ok no-dot f10">' + escapeHtml(l[2]) + '</span>'
            : '<span class="tag tag-muted no-dot f10">' + escapeHtml(l[2]) + '</span>') +
          '</td></tr>';
      }).join('') + '</tbody></table></div>';

    return card('需一并记录的事件', '1 / 4 有读源',
      body +
      note('warn', '「内容被淘汰」查不到，不是没发生。',
        'content_reviews 有表（feedback/models.py:336）、有两个写端点' +
        '（POST /contents/{id}/reviews、POST /projects/{id}/content-reviews），' +
        '但**没有任何 GET**（feedback/api.py 全文只有 2 个 @router.get）。' +
        '另外 content_items.state 的取值域是 DRAFT / STRUCTURED / GENERATED / FAILED' +
        '（content/types.py:147-150），里面**没有「已通过 / 已淘汰」**—— ' +
        '不得把 GENERATED 当「已通过」显示。'));
  }

  /** 版本时间线：versions 是本页唯一能证明「改过几版」的真数据。 */
  function renderVersionTimeline(versions) {
    var list = rows(versions).slice().sort(function (a, b) {
      return Number(b.versionNo) - Number(a.versionNo);
    });
    if (!list.length) {
      return card('版本时间线', 'GET /contents/{id}/versions',
        emptyBlock('未获取到版本历史。', '接口返回空数组'));
    }

    var body = list.map(function (v, i) {
      return '<div class="row between" style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.05)">' +
        '<div class="col" style="gap:3px;flex:1">' +
          '<div class="row" style="gap:6px;flex-wrap:wrap">' +
            '<span class="code f11">v' + escapeHtml(text(v.versionNo)) + '</span>' +
            (i === 0 ? tag('当前稿', 'tag-ok') : '') +
            (text(v.adoptedTitleCandidateId) ? tag('已采纳标题候选', 'tag-purple') : '') +
          '</div>' +
          '<span class="f11">' + escapeHtml(truncate(text(v.title) || '（无标题）', 60)) + '</span>' +
        '</div>' +
        '<div class="col" style="align-items:flex-end;gap:3px">' +
          '<span class="f10 t-fai mono">' + escapeHtml(fmtTime(v.createdAt)) + '</span>' +
          '<span class="f10 t-fai mono" title="' + escapeHtml(text(v.payloadHash)) + '">' +
            escapeHtml(truncate(text(v.payloadHash), 12)) + '</span>' +
        '</div></div>';
    }).join('');

    return card('版本时间线', list.length + ' 个版本 · 最新为当前稿',
      body +
      note('info', '这里没有正文对照（diff）。',
        'ContentVersionBody 不含 body / bindings / payload（content/api.py:692 原文），' +
        '而 GET /contents/{contentId} 没有 versionId 查询参数、只返回当前版 —— ' +
        '**历史版本正文没有读取端点**，所以做不了正文 diff，只能对 payloadHash。' +
        '对比：GET /knowledge/assets/{assetId}?versionId= 是有这个能力的，内容侧没有。'));
  }

  /* ---------------------------- 加载 / 空 / 错误 ---------------------------- */

  function renderLoading(label) {
    return '<section class="card"><div class="card-head"><span class="card-title">读取中</span></div>' +
      '<div class="card-body"><div class="row" style="gap:8px">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">' + escapeHtml(text(label) || '正在读取反馈与版本…') + '</span>' +
      '</div></div></section>';
  }

  function renderEmpty(message) {
    return card('暂无数据', '', emptyBlock(text(message) || '这条内容还没有反馈。'));
  }

  function renderError(err) {
    var e = err && typeof err === 'object' ? err : {};
    var code = text(e.code);
    var message = text(e.message) || text(err) || '读取反馈数据失败。';
    var offline = !!e.offline || code === 'DEPENDENCY_UNAVAILABLE';
    var details = e.details && typeof e.details === 'object' ? e.details : null;
    var hint = offline
      ? '连不上后端（页面结构已保留）。确认后端已启动后可重试。'
      : (e.retryable ? '该错误可重试，请稍后再试。' : '请按错误码排查后重试。');
    /* 游标绑定筛选：换了筛选条件还用旧游标 → 400 cursor_binding_mismatch */
    if (details && text(details.reason) === 'cursor_binding_mismatch') {
      hint = '筛选条件变了但沿用了旧游标（cursor_binding_mismatch）。请从第一页重新读取。';
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
    /* 纯计算 */
    indexVersions: indexVersions,
    resolveVersionLabel: resolveVersionLabel,
    observedVersionLabel: observedVersionLabel,
    tallyByCategory: tallyByCategory,
    tallyByScope: tallyByScope,
    unresolvedBlocking: unresolvedBlocking,
    /* 渲染 */
    renderFixList: renderFixList,
    renderScopeCards: renderScopeCards,
    renderCategoryTally: renderCategoryTally,
    renderRejectedList: renderRejectedList,
    renderRootCauseDoctrine: renderRootCauseDoctrine,
    renderRoundNotice: renderRoundNotice,
    renderEventRecordDoctrine: renderEventRecordDoctrine,
    renderVersionTimeline: renderVersionTimeline,
    renderLoading: renderLoading,
    renderEmpty: renderEmpty,
    renderError: renderError,
    escapeHtml: escapeHtml,
    CATEGORIES: CATEGORIES,
    CATEGORY_LABEL: CATEGORY_LABEL
  };
});
