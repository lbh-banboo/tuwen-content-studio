/* ============================================================================
 * render/projects.js —— 项目中心渲染层（方案 A：数据 → HTML 字符串）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 *
 * 双环境导出：
 *   Node/CommonJS  → require('./projects.js')
 *   浏览器          → RednoteRender.projects
 *
 * 数据形状（来自 GET /projects，api.js 解包后的数组）：
 *   { id, projectCode, name, phase, dueDate, targetDeliveryCount,
 *     ownerId, brandId, productId }
 *   phase ∈ PREPARATION | PRODUCTION | DATA_COLLECTION | CLOSED
 *
 * 已知口径：列表接口只返回 ownerId，不含成员姓名 / 品牌名 / 产品名；
 * 本模块不推测姓名，负责人列按 member ID 展示。
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.projects = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var COLUMNS = 6;

  var PHASE_META = {
    PREPARATION: { label: '准备期', cls: 'tag-purple' },
    PRODUCTION: { label: '投产期', cls: 'tag-info' },
    DATA_COLLECTION: { label: '数据回收期', cls: 'tag-ok' },
    CLOSED: { label: '已结束', cls: 'tag-muted' }
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

  function listOf(rows) {
    return Array.isArray(rows)
      ? rows.filter(function (row) { return row && typeof row === 'object'; })
      : [];
  }

  function phaseMeta(phase) {
    return PHASE_META[phase] || { label: text(phase) || '未提供', cls: '' };
  }

  /* ---------------------------- 汇总 ---------------------------- */

  function summarize(rows) {
    var list = listOf(rows);
    var out = { total: list.length, targetSum: 0, withTarget: 0, unknownPhase: 0, phases: {} };
    Object.keys(PHASE_META).forEach(function (key) { out.phases[key] = 0; });

    list.forEach(function (row) {
      var n = Number(row.targetDeliveryCount);
      if (isFinite(n)) {
        out.targetSum += n;
        out.withTarget += 1;
      }
      if (PHASE_META[row.phase]) out.phases[row.phase] += 1;
      else out.unknownPhase += 1;
    });
    return out;
  }

  function renderSummary(rows) {
    var s = summarize(rows);
    if (!s.total) return '暂无可见项目';
    var tail = s.withTarget === s.total
      ? '计划交付合计 ' + s.targetSum + ' 条'
      : '计划交付合计 ' + s.targetSum + ' 条（' + (s.total - s.withTarget) + ' 项缺该字段）';
    return '共 ' + s.total + ' 个项目 · ' + tail;
  }

  /* ---------------------------- 渲染 ---------------------------- */

  function renderProjectRow(project) {
    var p = project && typeof project === 'object' ? project : {};
    var phase = phaseMeta(p.phase);
    var name = text(p.name) ? escapeHtml(p.name) : '<span class="t-mut">名称未提供</span>';
    var code = text(p.projectCode)
      ? '<div class="f10 t-fai mono">' + escapeHtml(p.projectCode) + '</div>'
      : '<div class="f10 t-fai">编号未提供</div>';

    var target = p.targetDeliveryCount === null || p.targetDeliveryCount === undefined
      ? '<span class="t-mut">—</span>'
      : '<span class="mono">' + escapeHtml(p.targetDeliveryCount) + '</span> <span class="f10 t-fai">条</span>';

    var owner = text(p.ownerId)
      ? '<span class="f11 mono" title="' + escapeHtml(p.ownerId) + '">' + escapeHtml(shortId(p.ownerId)) + '</span>'
      : '<span class="t-mut">—</span>';

    var href = text(p.id)
      ? 'project-overview.v2.html?id=' + encodeURIComponent(text(p.id))
      : 'project-overview.v2.html';

    return '<tr>' +
      '<td class="strong">' + name + code + '</td>' +
      '<td><span class="tag ' + phase.cls + ' no-dot f10">' + escapeHtml(phase.label) + '</span></td>' +
      '<td class="num">' + target + '</td>' +
      '<td class="f11 mono">' + (text(p.dueDate) ? escapeHtml(p.dueDate) : '<span class="t-mut">未提供</span>') + '</td>' +
      '<td>' + owner + '</td>' +
      '<td class="num"><div class="row-act">' +
        '<a class="btn btn-sm" href="' + escapeHtml(href) + '">进入</a>' +
      '</div></td>' +
      '</tr>';
  }

  function renderProjectTable(rows) {
    var list = listOf(rows);
    var body = list.length
      ? list.map(renderProjectRow).join('')
      : renderEmpty();

    return '<div class="table-wrap"><table class="tbl">' +
      '<thead><tr>' +
        '<th>项目 / 编号</th>' +
        '<th style="width:120px">阶段</th>' +
        '<th class="num" style="width:96px">计划交付</th>' +
        '<th style="width:120px">截止日期</th>' +
        '<th style="width:132px">负责人（ID）</th>' +
        '<th class="num" style="width:82px">操作</th>' +
      '</tr></thead>' +
      '<tbody>' + body + '</tbody></table></div>';
  }

  function renderEmpty() {
    return '<tr><td colspan="' + COLUMNS + '">' +
      '<div class="col" style="align-items:center;gap:6px;padding:24px 0;">' +
      '<i class="fa-solid fa-folder-open t-fai"></i>' +
      '<span class="f12 t-mut">暂无可见项目</span>' +
      '<span class="f10 t-fai">创建项目后会自动出现在这里</span>' +
      '</div></td></tr>';
  }

  function renderLoading() {
    return '<div class="table-wrap"><table class="tbl"><thead><tr>' +
      '<th>项目 / 编号</th><th>阶段</th><th class="num">计划交付</th>' +
      '<th>截止日期</th><th>负责人（ID）</th><th class="num">操作</th>' +
      '</tr></thead><tbody><tr><td colspan="' + COLUMNS + '">' +
      '<div class="col" style="align-items:center;gap:6px;padding:24px 0;">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">正在读取项目列表…</span>' +
      '</div></td></tr></tbody></table></div>';
  }

  function renderError(err) {
    var e = err && typeof err === 'object' ? err : {};
    var code = text(e.code);
    var message = text(e.message) || text(err) || '读取项目列表失败。';
    var offline = !!e.offline || code === 'DEPENDENCY_UNAVAILABLE';
    var hint = offline
      ? '连不上后端。确认 127.0.0.1:8787 已启动后可重试。'
      : (e.retryable ? '该错误可重试，请稍后再试。' : '请按错误码排查后重试。');

    return '<div class="table-wrap"><table class="tbl"><tbody><tr class="tr-danger">' +
      '<td colspan="' + COLUMNS + '">' +
      '<div class="row" style="align-items:flex-start;gap:10px;padding:14px 0;">' +
      '<i class="fa-solid fa-triangle-exclamation t-dan"></i>' +
      '<div class="col" style="flex:1;gap:4px;">' +
      '<span class="f12 fw6">' + escapeHtml(message) + '</span>' +
      '<span class="f10 t-mut">' + escapeHtml((code ? code + ' · ' : '') + hint) + '</span>' +
      '</div>' +
      '<button class="btn btn-sm btn-ghost" data-action="retry">' +
      '<i class="fa-solid fa-rotate"></i> 重试</button>' +
      '</div></td></tr></tbody></table></div>';
  }

  return {
    summarize: summarize,
    renderSummary: renderSummary,
    renderProjectTable: renderProjectTable,
    renderProjectRow: renderProjectRow,
    renderEmpty: renderEmpty,
    renderLoading: renderLoading,
    renderError: renderError,
    escapeHtml: escapeHtml
  };
});
