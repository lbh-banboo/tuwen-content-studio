/* ============================================================================
 * render/project-overview.js —— 项目总览（八步关口中枢）渲染层（方案 A）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 *
 * 双环境导出：
 *   Node/CommonJS  → require('./project-overview.js')
 *   浏览器          → RednoteRender.projectOverview
 *
 * 数据形状（api.js 解包后）：
 *   GET /projects/{id}                → { id, projectCode, name, phase, dueDate,
 *       timezone, targetDeliveryCount, targetPublicationCount, externalPolicy,
 *       ownerId, brandId, productId, description, lockVersion, permissions }
 *   GET /projects/{id}/step-approvals → [{ approvalId, projectId, stepNo,
 *       subjectKind, subjectVersionId, decision, decidedAt }]
 *       decision ∈ APPROVED | REJECTED
 *   GET /projects/{id}/budget         → { projectId, currency, limit, committed,
 *       reserved, available, configured }
 *
 * 口径：某关口的"最新批准"= 该 stepNo 下 decidedAt 最大的一行；
 * 没有任何批准行 = 未开始。不根据 phase 推断关口状态。
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.projectOverview = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var STEP_GATES = [
    { step: 1, label: 'Brief 解析与加厚', href: 'brief.html' },
    { step: 2, label: '项目研究与上传', href: 'research.html' },
    { step: 3, label: '信息审核与基线', href: 'info-review.html' },
    { step: 4, label: '素材试产配置', href: 'trial-config.html' },
    { step: 5, label: '试产反馈', href: 'content-feedback.html' },
    { step: 6, label: '扩产 / 批产', href: 'scale-batch.html' },
    { step: 7, label: '批量产出与交付', href: 'delivery.v2.html' },
    { step: 8, label: '发布登记与数据', href: 'data-feedback.v2.html' }
  ];

  var PHASE_META = {
    PREPARATION: { label: '准备期', cls: 'tag-purple' },
    PRODUCTION: { label: '投产期', cls: 'tag-info' },
    DATA_COLLECTION: { label: '数据回收期', cls: 'tag-ok' },
    CLOSED: { label: '已结束', cls: 'tag-muted' }
  };

  var GATE_STATE = {
    done: { label: '已批准', tag: 'tag-ok', icon: 'fa-circle-check', iconCls: 'ok' },
    rejected: { label: '已驳回', tag: 'tag-danger', icon: 'fa-circle-xmark', iconCls: 'danger' },
    pending: { label: '未开始', tag: 'tag-muted', icon: 'fa-lock', iconCls: '' },
    unknown: { label: '状态未识别', tag: 'tag-warn', icon: 'fa-circle-question', iconCls: 'warn' }
  };

  /* ---------------------------- 基础工具 ---------------------------- */

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

  function provided(value, fallback) {
    var s = text(value);
    return s ? escapeHtml(s) : '<span class="t-mut">' + escapeHtml(fallback || '未提供') + '</span>';
  }

  function phaseMeta(phase) {
    return PHASE_META[phase] || { label: text(phase) || '未提供', cls: '' };
  }

  function fmtMoney(value, currency) {
    var raw = text(value);
    if (!raw) return '<span class="t-mut">未提供</span>';
    var n = Number(raw);
    var symbol = currency === 'CNY' || !currency ? '¥' : (text(currency) + ' ');
    if (!isFinite(n)) return escapeHtml(raw);
    return escapeHtml(symbol + n.toLocaleString('zh-CN', {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    }));
  }

  /* ---------------------------- 关口状态 ---------------------------- */

  function gateStatuses(rows) {
    var list = Array.isArray(rows) ? rows : [];
    var latest = {};

    list.forEach(function (row) {
      if (!row || typeof row !== 'object') return;
      var step = Number(row.stepNo);
      if (!isFinite(step)) return;
      var at = text(row.decidedAt);
      if (!latest[step] || at >= text(latest[step].decidedAt)) latest[step] = row;
    });

    return STEP_GATES.map(function (gate) {
      var row = latest[gate.step];
      var decision = row ? text(row.decision) : '';
      var state = !row ? 'pending'
        : decision === 'APPROVED' ? 'done'
        : decision === 'REJECTED' ? 'rejected'
        : 'unknown';
      return {
        step: gate.step,
        label: gate.label,
        href: gate.href,
        state: state,
        decision: decision,
        decidedAt: row ? text(row.decidedAt) : '',
        approvalId: row ? text(row.approvalId) : ''
      };
    });
  }

  function renderGateRow(gate) {
    var meta = GATE_STATE[gate.state] || GATE_STATE.pending;
    var statusLabel = gate.state === 'unknown' && gate.decision
      ? meta.label + '（' + gate.decision + '）'
      : meta.label;
    var sub = gate.decidedAt
      ? '决定时间 ' + fmtTime(gate.decidedAt) + ' · 同一关口取最新一行'
      : '尚无批准记录';

    return '<div class="list-item">' +
      '<span class="li-icon' + (meta.iconCls ? ' ' + meta.iconCls : '') + '">' +
        '<i class="fa-solid ' + meta.icon + '"></i></span>' +
      '<div class="li-main">' +
        '<div class="li-title">步骤 ' + gate.step + ' · ' + escapeHtml(gate.label) + '</div>' +
        '<div class="li-sub">' + escapeHtml(sub) + '</div>' +
      '</div>' +
      '<div class="li-right">' +
        '<span class="tag ' + meta.tag + ' no-dot f10">' + escapeHtml(statusLabel) + '</span>' +
        '<a class="btn btn-sm btn-ghost" href="' + escapeHtml(gate.href) + '">查看</a>' +
      '</div>' +
      '</div>';
  }

  function renderGateHub(rows) {
    var statuses = gateStatuses(rows);
    var approved = statuses.filter(function (g) { return g.state === 'done'; }).length;
    var rejected = statuses.filter(function (g) { return g.state === 'rejected'; }).length;
    var note = statuses.every(function (g) { return g.state === 'pending'; })
      ? '<div class="note note-info" style="margin:12px 18px 16px">' +
          '<i class="fa-solid fa-circle-info"></i><div>' +
          'GET /step-approvals 当前返回空数组：8 个关口全部显示「未开始」。' +
          '本卡不根据项目阶段推断批准状态。' +
          '</div></div>'
      : '';

    return '<section class="card">' +
      '<div class="card-head">' +
        '<div class="row"><i class="fa-solid fa-diagram-project t-pur"></i>' +
          '<h3 class="card-title">八步关口</h3></div>' +
        '<span class="card-sub">已批准 ' + approved + ' / ' + statuses.length +
          (rejected ? ' · 已驳回 ' + rejected : '') + ' · 数据源 GET /step-approvals</span>' +
      '</div>' +
      '<div>' + statuses.map(renderGateRow).join('') + '</div>' +
      note +
      '</section>';
  }

  /* ---------------------------- 页头 / 项目信息 ---------------------------- */

  function renderProjectHead(project) {
    var p = project && typeof project === 'object' ? project : null;
    if (!p) {
      return '<div><h1 class="page-title">项目未选择</h1>' +
        '<p class="page-desc">GET /projects 没有返回可访问项目，或 URL 中的 id 无效。' +
        '请从项目中心重新进入。</p></div>';
    }
    var phase = phaseMeta(p.phase);
    var code = text(p.projectCode) ? escapeHtml(p.projectCode) : '编号未提供';
    return '<div>' +
      '<div class="row mb6">' +
        '<span class="code">' + code + '</span>' +
        '<span class="tag ' + phase.cls + ' no-dot">' + escapeHtml(phase.label) + '</span>' +
        '<span class="tag tag-muted no-dot"><i class="fa-regular fa-clock"></i> 截止 ' +
          (text(p.dueDate) ? escapeHtml(p.dueDate) : '未提供') + '</span>' +
      '</div>' +
      '<h1 class="page-title">' + (text(p.name) ? escapeHtml(p.name) : '名称未提供') + '</h1>' +
      '<p class="page-desc">目标交付 ' +
        (p.targetDeliveryCount == null ? '未提供' : escapeHtml(p.targetDeliveryCount) + ' 条') +
        ' · 目标发布 ' +
        (p.targetPublicationCount == null ? '未提供' : escapeHtml(p.targetPublicationCount) + ' 次') +
        ' · 外部策略 ' + provided(p.externalPolicy, '未提供') +
      '</p>' +
      '</div>';
  }

  function infoRow(label, value) {
    return '<tr><td class="t-mut" style="width:96px">' + escapeHtml(label) + '</td>' +
      '<td class="strong">' + value + '</td></tr>';
  }

  function renderProjectInfoCard(project) {
    var p = project && typeof project === 'object' ? project : null;
    if (!p) {
      return '<section class="card"><div class="card-head"><span class="card-title">项目信息</span></div>' +
        '<div class="card-body"><span class="f12 t-mut">未获取到项目详情。</span></div></section>';
    }
    var rows = [
      infoRow('项目编号', provided(p.projectCode)),
      infoRow('阶段', provided(phaseMeta(p.phase).label)),
      infoRow('截止日期', provided(p.dueDate) + (text(p.timezone)
        ? ' <span class="f10 t-fai">' + escapeHtml(p.timezone) + '</span>' : '')),
      infoRow('目标交付', p.targetDeliveryCount == null ? provided(null) : escapeHtml(p.targetDeliveryCount) + ' 条'),
      infoRow('目标发布', p.targetPublicationCount == null ? provided(null) : escapeHtml(p.targetPublicationCount) + ' 次'),
      infoRow('外部策略', provided(p.externalPolicy)),
      infoRow('负责人 ID', text(p.ownerId)
        ? '<span class="mono f11" title="' + escapeHtml(p.ownerId) + '">' + escapeHtml(shortId(p.ownerId)) + '</span>'
        : provided(null)),
      infoRow('品牌 ID', text(p.brandId)
        ? '<span class="mono f11" title="' + escapeHtml(p.brandId) + '">' + escapeHtml(shortId(p.brandId)) + '</span>'
        : provided(null)),
      infoRow('产品 ID', text(p.productId)
        ? '<span class="mono f11" title="' + escapeHtml(p.productId) + '">' + escapeHtml(shortId(p.productId)) + '</span>'
        : provided(null)),
      infoRow('版本号', p.lockVersion == null ? provided(null) : escapeHtml(p.lockVersion)),
      infoRow('项目说明', text(p.description)
        ? '<span class="f11" style="white-space:pre-wrap">' + escapeHtml(p.description) + '</span>'
        : provided(null))
    ];

    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">项目信息</span>' +
        '<span class="card-sub">字段原样展示，缺字段标「未提供」</span></div>' +
      '<div class="card-body" style="padding-top:8px;padding-bottom:8px">' +
        '<table class="tbl tbl-compact" style="font-size:12px"><tbody>' +
        rows.join('') + '</tbody></table>' +
      '</div></section>';
  }

  /* ---------------------------- 预算 ---------------------------- */

  function renderBudgetCard(budget) {
    var b = budget && typeof budget === 'object' ? budget : null;
    if (!b) {
      return '<section class="card"><div class="card-head"><span class="card-title">项目预算</span></div>' +
        '<div class="card-body"><span class="f12 t-mut">未获取到预算信息（GET /budget 无数据）。</span></div></section>';
    }

    var limit = Number(b.limit);
    var committed = Number(b.committed);
    var reserved = Number(b.reserved);
    var used = (isFinite(committed) ? committed : 0) + (isFinite(reserved) ? reserved : 0);
    var pct = isFinite(limit) && limit > 0 ? Math.min(100, used / limit * 100) : 0;

    var body = [
      '<div class="row between mb10">' +
        '<span class="metric-value" style="font-size:22px">' + fmtMoney(b.available, b.currency) + '</span>' +
        '<span class="f11 t-mut">可用 / 额度 ' + fmtMoney(b.limit, b.currency) + '</span>' +
      '</div>'
    ];
    if (b.configured === false) {
      body.push('<div class="note note-warn mb14"><i class="fa-solid fa-triangle-exclamation"></i>' +
        '<div><b>预算未配置</b>（configured=false）。以上金额为接口原值，0 不代表已批准额度。</div></div>');
    } else if (b.configured === true) {
      body.push('<div class="note note-info mb14"><i class="fa-solid fa-circle-info"></i>' +
        '<div>预算已配置（configured=true）。</div></div>');
    } else {
      body.push('<div class="note note-info mb14"><i class="fa-solid fa-circle-info"></i>' +
        '<div>接口未返回 configured 字段，配置状态未知。</div></div>');
    }
    body.push('<span class="bar mb14"><span class="bar-fill" style="width:' + pct.toFixed(1) + '%"></span></span>');

    [
      ['额度', b.limit],
      ['已承诺', b.committed],
      ['已冻结', b.reserved],
      ['可用', b.available]
    ].forEach(function (row) {
      body.push('<div class="row between f12"><span class="t-mut">' + row[0] + '</span>' +
        '<span class="mono">' + fmtMoney(row[1], b.currency) + '</span></div>');
    });

    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">项目预算</span>' +
        '<div class="card-head-actions"><a class="btn btn-sm btn-ghost" href="cost.html">明细</a></div></div>' +
      '<div class="card-body">' + body.join('') + '</div>' +
      '</section>';
  }

  /* ---------------------------- 加载 / 错误 / 空态 ---------------------------- */

  function renderHeadLoading() {
    return '<div><h1 class="page-title">正在读取项目…</h1>' +
      '<p class="page-desc"><i class="fa-solid fa-circle-notch fa-spin"></i> GET /projects/{id}</p></div>';
  }

  function renderHeadError(err) {
    var e = err && typeof err === 'object' ? err : {};
    return '<div><h1 class="page-title">项目读取失败</h1>' +
      '<p class="page-desc">' + escapeHtml(text(e.message) || text(err) || '未知错误') +
      (text(e.code) ? ' · ' + escapeHtml(e.code) : '') + '</p></div>';
  }

  function renderCardLoading(message) {
    return '<section class="card"><div class="card-head"><span class="card-title">读取中</span></div>' +
      '<div class="card-body"><div class="row" style="gap:8px">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">' + escapeHtml(message || '正在读取…') + '</span>' +
      '</div></div></section>';
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
      '<span class="f10 t-mut">' + escapeHtml((code ? code + ' · ' : '') + hint) + '</span>' +
      '</div>' +
      '<button class="btn btn-sm btn-ghost" data-action="retry">' +
      '<i class="fa-solid fa-rotate"></i> 重试</button>' +
      '</div></div></section>';
  }

  return {
    STEP_GATES: STEP_GATES,
    gateStatuses: gateStatuses,
    renderGateHub: renderGateHub,
    renderGateRow: renderGateRow,
    renderProjectHead: renderProjectHead,
    renderProjectInfoCard: renderProjectInfoCard,
    renderBudgetCard: renderBudgetCard,
    renderHeadLoading: renderHeadLoading,
    renderHeadError: renderHeadError,
    renderCardLoading: renderCardLoading,
    renderCardError: renderCardError,
    escapeHtml: escapeHtml
  };
});
