/* ============================================================================
 * render/brief-thicken.js —— Brief 加厚页渲染层（方案 A：数据 → HTML 字符串）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 * 双环境导出：Node/CommonJS → require('./brief-thicken.js')；浏览器 → RednoteRender.briefThicken
 *
 * 数据形状（来自 GET /projects/{id}/brief）：
 *   draft.suggestions: [{ fieldPath, text, category, rationale, evidence? }]
 *     category ∈ SOURCE_FACT | EXTERNAL_EVIDENCE | STRATEGY | UNCONFIRMED
 *   draft.issueResolutions: [{ id, fieldPath, severity, description, state, resolution?, resolvedBy? }]
 *   draft.suggestionsVersionId / activeVersionId 决定能否发起加厚。
 *
 * 加厚是 POST /projects/{id}/brief-enrichment-jobs（body: briefVersionId +
 * 非空 allowedSourceIds），本页只读展示既有建议，不自动发起写操作。
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.briefThicken = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CATEGORY_META = {
    SOURCE_FACT: { label: '原文事实', cls: 'tag-ok', key: 'sourceFact' },
    EXTERNAL_EVIDENCE: { label: '外部证据', cls: 'tag-info', key: 'externalEvidence' },
    STRATEGY: { label: '策略建议', cls: 'tag-purple', key: 'strategy' },
    UNCONFIRMED: { label: '待确认', cls: 'tag-warn', key: 'unconfirmed' }
  };

  var SEVERITY_META = {
    BLOCK: { label: '阻断', cls: 'tag-danger' },
    WARN: { label: '提醒', cls: 'tag-warn' }
  };

  var ISSUE_STATE_META = {
    OPEN: { label: '待处置', cls: 'tag-warn' },
    RESOLVED: { label: '已解决', cls: 'tag-ok' },
    DISMISSED: { label: '已忽略', cls: 'tag-muted' }
  };

  function text(value) {
    return value === null || value === undefined ? '' : String(value);
  }

  function escapeHtml(value) {
    return text(value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
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

  function listOf(rows) {
    return Array.isArray(rows)
      ? rows.filter(function (row) { return row && typeof row === 'object'; })
      : [];
  }

  function draftOf(brief) {
    return brief && typeof brief === 'object' && brief.draft && typeof brief.draft === 'object'
      ? brief.draft : null;
  }

  function suggestionsOf(brief) {
    var d = draftOf(brief);
    return d ? listOf(d.suggestions) : [];
  }

  function issuesOf(brief) {
    var d = draftOf(brief);
    return d ? listOf(d.issueResolutions) : [];
  }

  function summarize(brief) {
    var out = { sourceFact: 0, externalEvidence: 0, strategy: 0, unconfirmed: 0, unknown: 0, total: 0 };
    suggestionsOf(brief).forEach(function (s) {
      var meta = CATEGORY_META[s.category];
      out.total += 1;
      if (meta) out[meta.key] += 1;
      else out.unknown += 1;
    });
    out.issues = issuesOf(brief).length;
    out.blockingOpen = issuesOf(brief).filter(function (i) {
      return i.severity === 'BLOCK' && i.state === 'OPEN';
    }).length;
    return out;
  }

  /* ---------------------------- 加厚建议 ---------------------------- */

  function evidenceCell(suggestion) {
    var ev = suggestion && suggestion.evidence;
    if (!ev || typeof ev !== 'object') return '<span class="t-mut">未提供</span>';
    if (text(ev.segmentId)) {
      return '<span class="mono f10" title="' + escapeHtml(ev.segmentId) + '">片段 ' +
        escapeHtml(shortId(ev.segmentId)) + '</span>';
    }
    if (text(ev.url)) return '<span class="f10 mono">' + escapeHtml(ev.url) + '</span>';
    return '<span class="t-mut">未提供</span>';
  }

  function renderSuggestionRow(suggestion) {
    var meta = CATEGORY_META[suggestion.category] || { label: text(suggestion.category) || '分类未提供', cls: 'tag-muted' };
    return '<tr>' +
      '<td><span class="tag ' + meta.cls + ' no-dot f10">' + escapeHtml(meta.label) + '</span></td>' +
      '<td class="strong">' + (text(suggestion.text) ? escapeHtml(suggestion.text) : '<span class="t-mut">内容未提供</span>') +
        (text(suggestion.rationale)
          ? '<div class="f10 t-fai mt6">' + escapeHtml(suggestion.rationale) + '</div>' : '') + '</td>' +
      '<td class="f11 mono">' + (text(suggestion.fieldPath) ? escapeHtml(suggestion.fieldPath) : '<span class="t-mut">未提供</span>') + '</td>' +
      '<td>' + evidenceCell(suggestion) + '</td>' +
      '</tr>';
  }

  function renderSuggestionsCard(brief) {
    var list = suggestionsOf(brief);
    var body = list.length
      ? '<div class="table-wrap"><table class="tbl tbl-compact">' +
          '<thead><tr><th style="width:92px">分类</th><th>内容</th>' +
          '<th style="width:190px">字段</th><th style="width:140px">证据</th></tr></thead>' +
          '<tbody>' + list.map(renderSuggestionRow).join('') + '</tbody></table></div>'
      : renderEmpty('尚无加厚建议', 'draft.suggestions 为空；加厚任务完成后建议会出现在这里');

    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">加厚输出</span>' +
        '<span class="card-sub">四类严格区分 · 建议不自动生效 · 数据源 draft.suggestions</span></div>' +
      '<div>' + body + '</div></section>';
  }

  /* ---------------------------- 前置条件 ---------------------------- */

  function renderPrereqCard(brief) {
    var d = draftOf(brief);
    var activeVersionId = text(brief && brief.activeVersionId);
    var suggestionsVersionId = d ? text(d.suggestionsVersionId) : '';
    var ready = !!activeVersionId;

    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">加厚前置条件</span>' +
        '<span class="tag ' + (ready ? 'tag-ok' : 'tag-warn') + ' no-dot f10">' +
        (ready ? '已具备版本' : '未满足') + '</span></div>' +
      '<div class="card-body">' +
        '<table class="tbl tbl-compact" style="font-size:12px"><tbody>' +
          '<tr><td class="t-mut" style="width:148px">activeVersionId</td><td>' +
            (activeVersionId
              ? '<span class="mono f11" title="' + escapeHtml(activeVersionId) + '">' + escapeHtml(shortId(activeVersionId)) + '</span>'
              : '<span class="t-warn">null — 需先固化 Brief 版本</span>') + '</td></tr>' +
          '<tr><td class="t-mut">suggestionsVersionId</td><td>' +
            (suggestionsVersionId
              ? '<span class="mono f11" title="' + escapeHtml(suggestionsVersionId) + '">' + escapeHtml(shortId(suggestionsVersionId)) + '</span>'
              : '<span class="t-mut">null</span>') + '</td></tr>' +
          '<tr><td class="t-mut">草稿锁版本</td><td class="mono f11">' +
            (d && d.lockVersion != null ? escapeHtml(d.lockVersion) : '未提供') + '</td></tr>' +
        '</tbody></table>' +
        (ready
          ? '<div class="note note-info mt14"><i class="fa-solid fa-circle-info"></i><div>' +
            '可发起加厚：POST /projects/{id}/brief-enrichment-jobs（body: briefVersionId + 非空 allowedSourceIds）。' +
            '本页只读展示，不自动发起写操作。</div></div>'
          : '<div class="note note-warn mt14"><i class="fa-solid fa-triangle-exclamation"></i><div>' +
            '<b>当前不能发起加厚：</b>activeVersionId 为空，必须先完成解构并固化 Brief 版本。' +
            'POST /projects/{id}/brief-enrichment-jobs 要求 briefVersionId 与非空 allowedSourceIds，缺一即 422。</div></div>') +
      '</div></section>';
  }

  /* ---------------------------- 解构疑问 ---------------------------- */

  function renderIssueRow(issue) {
    var severity = SEVERITY_META[issue.severity] || { label: text(issue.severity) || '级别未提供', cls: 'tag-muted' };
    var state = ISSUE_STATE_META[issue.state] || { label: text(issue.state) || '状态未提供', cls: 'tag-muted' };
    return '<div class="list-item">' +
      '<span class="li-icon ' + (issue.severity === 'BLOCK' ? 'danger' : 'warn') + '">' +
        '<i class="fa-solid fa-circle-exclamation"></i></span>' +
      '<div class="li-main">' +
        '<div class="li-title">' + (text(issue.description) ? escapeHtml(issue.description) : '疑问描述未提供') + '</div>' +
        '<div class="li-sub">' + escapeHtml(text(issue.fieldPath) || '字段未提供') +
          (text(issue.resolution) ? ' · 处置：' + escapeHtml(issue.resolution) : '') + '</div>' +
      '</div>' +
      '<div class="li-right"><span class="tag ' + severity.cls + ' no-dot f10">' + escapeHtml(severity.label) + '</span>' +
        '<span class="tag ' + state.cls + ' no-dot f10">' + escapeHtml(state.label) + '</span></div>' +
      '</div>';
  }

  function renderIssuesCard(brief) {
    var issues = issuesOf(brief);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">解构疑问</span>' +
        '<span class="card-sub">draft.issueResolutions · ' + issues.length + ' 条</span></div>' +
      '<div>' + (issues.length
        ? issues.map(renderIssueRow).join('')
        : renderEmpty('无解构疑问', 'issueResolutions 为空')) + '</div></section>';
  }

  /* ---------------------------- 版本规则（静态） ---------------------------- */

  function renderRulesCard() {
    var rules = [
      ['建议不自动生效', '加厚结果写入 brief_drafts.suggestions；要成为事实必须在 PUT /brief-draft 中显式采纳。'],
      ['分类不可省略', 'SOURCE_FACT / EXTERNAL_EVIDENCE 必须带证据；STRATEGY 只能作为策略建议。'],
      ['版本不可原地修改', '固化后的 Brief 版本不可变；修改会形成新版本并保留历史。'],
      ['来源可追溯', '每条建议保留 fieldPath 与 evidence（片段 ID 或 URL），便于复核。']
    ];
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">加厚与版本规则</span>' +
        '<span class="card-sub">规则说明（静态）</span></div>' +
      '<div class="card-body"><div class="col" style="gap:10px">' +
        rules.map(function (rule) {
          return '<div class="row-t"><span class="li-icon purple"><i class="fa-solid fa-circle-info"></i></span>' +
            '<div><div class="f12 fw6">' + escapeHtml(rule[0]) + '</div>' +
            '<div class="f11 t-mut mt6">' + escapeHtml(rule[1]) + '</div></div></div>';
        }).join('') +
      '</div></div></section>';
  }

  /* ---------------------------- 页头 / 指标 ---------------------------- */

  function renderHead(project, brief) {
    var p = project && typeof project === 'object' ? project : {};
    var s = summarize(brief);
    var active = text(brief && brief.activeVersionId);
    return '<div>' +
      '<div class="row mb6">' +
        '<span class="tag tag-purple no-dot"><i class="fa-solid fa-1"></i> 步骤 1 / 8</span>' +
        '<span class="code">' + (text(p.projectCode) ? escapeHtml(p.projectCode) : '编号未提供') + '</span>' +
        (active
          ? '<span class="tag tag-ok no-dot">已固化 ' + escapeHtml(shortId(active)) + '</span>'
          : '<span class="tag tag-warn no-dot">未固化版本</span>') +
      '</div>' +
      '<h1 class="page-title">确认加厚输出与版本规则</h1>' +
      '<p class="page-desc">' + (text(p.name) ? escapeHtml(p.name) : '项目名称未提供') +
        ' · 建议 ' + s.total + ' 条 · 阻断疑问 ' + s.blockingOpen + ' 条 · 建议不自动生效</p></div>';
  }

  function renderMetrics(brief) {
    var s = summarize(brief);
    return [
      ['fa-file-lines', '原文事实', s.sourceFact, 't-ok', 'category=SOURCE_FACT'],
      ['fa-link', '外部证据', s.externalEvidence, 't-info', 'category=EXTERNAL_EVIDENCE'],
      ['fa-lightbulb', '策略建议', s.strategy, 't-pur', 'category=STRATEGY'],
      ['fa-circle-question', '待确认', s.unconfirmed + s.unknown, 't-warn', 'category=UNCONFIRMED / 未知']
    ].map(function (row) {
      return '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid ' + row[0] + '"></i>' + row[1] + '</div>' +
        '<div class="metric-value ' + row[3] + '">' + escapeHtml(row[2]) + '</div>' +
        '<div class="metric-foot">' + escapeHtml(row[4]) + '</div></div>';
    }).join('');
  }

  /* ---------------------------- 通用块 ---------------------------- */

  function renderEmpty(title, sub) {
    return '<div class="col" style="align-items:center;gap:6px;padding:22px 0">' +
      '<i class="fa-solid fa-inbox t-fai"></i>' +
      '<span class="f12 t-mut">' + escapeHtml(title || '暂无数据') + '</span>' +
      (sub ? '<span class="f10 t-fai">' + escapeHtml(sub) + '</span>' : '') + '</div>';
  }

  function renderCardLoading(message) {
    return '<section class="card"><div class="card-head"><span class="card-title">读取中</span></div>' +
      '<div class="card-body"><div class="row" style="gap:8px">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">' + escapeHtml(message || '正在读取…') + '</span></div></div></section>';
  }

  function renderCardError(err) {
    var e = err && typeof err === 'object' ? err : {};
    var code = text(e.code);
    var message = text(e.message) || text(err) || '读取失败。';
    var offline = !!e.offline || code === 'DEPENDENCY_UNAVAILABLE';
    var hint = offline
      ? '连不上后端。确认 127.0.0.1:8787 已启动后可重试。'
      : (e.retryable ? '该错误可重试，请稍后再试。' : '请按错误码排查后重试。');
    return '<section class="card"><div class="card-head"><span class="card-title">读取失败</span></div>' +
      '<div class="card-body"><div class="row" style="align-items:flex-start;gap:10px">' +
      '<i class="fa-solid fa-triangle-exclamation t-dan"></i>' +
      '<div class="col" style="flex:1;gap:4px">' +
      '<span class="f12 fw6">' + escapeHtml(message) + '</span>' +
      '<span class="f10 t-mut">' + escapeHtml((code ? code + ' · ' : '') + hint) + '</span></div>' +
      '<button class="btn btn-sm btn-ghost" data-action="retry">' +
      '<i class="fa-solid fa-rotate"></i> 重试</button></div></div></section>';
  }

  return {
    summarize: summarize,
    renderHead: renderHead,
    renderMetrics: renderMetrics,
    renderSuggestionsCard: renderSuggestionsCard,
    renderPrereqCard: renderPrereqCard,
    renderIssuesCard: renderIssuesCard,
    renderRulesCard: renderRulesCard,
    renderEmpty: renderEmpty,
    renderCardLoading: renderCardLoading,
    renderCardError: renderCardError,
    escapeHtml: escapeHtml
  };
});
