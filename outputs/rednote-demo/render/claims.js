/* ============================================================================
 * render/claims.js —— 可用主张页渲染层（方案 A：数据 → HTML 字符串）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 * 双环境导出：Node/CommonJS → require('./claims.js')；浏览器 → RednoteRender.claims
 *
 * 复用 GET /projects/{id}/information-draft。claims[] 的实际形状只有
 * { claimId, statement, source, state }；证据强度、体验缺口、拟用策略不在
 * 该响应里，本页不推测，只渲染真实字段并在页面注明。
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.claims = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CLAIM_STATE = {
    CONFIRMED: { label: '可用', cls: 'tag-ok' },
    CONFLICT: { label: '冲突', cls: 'tag-danger' },
    PENDING: { label: '待确认', cls: 'tag-warn' }
  };

  var CONFLICT_STATE = {
    OPEN: { label: '待处置', cls: 'tag-warn' },
    SETTLED: { label: '已处置', cls: 'tag-ok' }
  };

  var CONFLICT_KIND = {
    SOURCE: '来源冲突',
    TIMELINESS: '时效冲突',
    CROSS_BRAND: '跨品牌冲突',
    FACT_VS_LIMIT: '事实与限制冲突'
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

  function listOf(rows) {
    return Array.isArray(rows)
      ? rows.filter(function (row) { return row && typeof row === 'object'; })
      : [];
  }

  function draftOf(draft) {
    return draft && typeof draft === 'object' ? draft : null;
  }

  function summarize(draft) {
    var d = draftOf(draft) || {};
    var claims = listOf(d.claims);
    var conflicts = listOf(d.conflicts);
    return {
      claims: claims.length,
      confirmed: claims.filter(function (c) { return c.state === 'CONFIRMED'; }).length,
      conflictClaims: claims.filter(function (c) { return c.state === 'CONFLICT'; }).length,
      pending: claims.filter(function (c) { return c.state === 'PENDING'; }).length,
      conflictsOpen: conflicts.filter(function (c) { return c.state !== 'SETTLED'; }).length,
      researchSelection: listOf(d.researchSelection).length,
      boundaries: d.boundaries && typeof d.boundaries === 'object' ? Object.keys(d.boundaries).length : 0
    };
  }

  function sourceHtml(source) {
    if (source === null || source === undefined || source === '') {
      return '<span class="t-mut">未提供</span>';
    }
    if (typeof source === 'string' || typeof source === 'number') return escapeHtml(source);
    if (typeof source === 'object') {
      if (text(source.label)) return escapeHtml(source.label);
      if (text(source.url)) return '<span class="mono f10">' + escapeHtml(source.url) + '</span>';
      if (text(source.segmentId)) return '<span class="mono f10">片段 ' + escapeHtml(shortId(source.segmentId)) + '</span>';
      return '<span class="mono f10">' + escapeHtml(JSON.stringify(source)) + '</span>';
    }
    return '<span class="t-mut">未提供</span>';
  }

  /* ---------------------------- 页头 / 指标 ---------------------------- */

  function renderHead(project, draft) {
    var p = project && typeof project === 'object' ? project : {};
    var s = summarize(draft);
    return '<div>' +
      '<div class="row mb6">' +
        '<span class="tag tag-purple no-dot"><i class="fa-solid fa-3"></i> 步骤 3 / 8</span>' +
        '<span class="code">' + (text(p.projectCode) ? escapeHtml(p.projectCode) : '编号未提供') + '</span>' +
        '<span class="tag ' + (s.confirmed ? 'tag-ok' : 'tag-warn') + ' no-dot">可用主张 ' + s.confirmed + '</span>' +
      '</div>' +
      '<h1 class="page-title">确认可用主张与拟用策略</h1>' +
      '<p class="page-desc">' + (text(p.name) ? escapeHtml(p.name) : '项目名称未提供') +
        ' · 每条主张必须回答「哪份资料支持它」。当前接口只返回 claimId / statement / source / state；' +
        '证据强度、体验缺口与拟用策略字段未提供，本页不以推测补齐。</p></div>';
  }

  function renderMetrics(draft) {
    var s = summarize(draft);
    return [
      ['fa-quote-left', '主张总数', s.claims, '', 'claims[]'],
      ['fa-circle-check', '可用', s.confirmed, 't-ok', 'state=CONFIRMED'],
      ['fa-triangle-exclamation', '冲突', s.conflictClaims, s.conflictClaims ? 't-dan' : '', 'state=CONFLICT'],
      ['fa-circle-question', '待确认', s.pending, s.pending ? 't-warn' : '', 'state=PENDING']
    ].map(function (row) {
      return '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid ' + row[0] + '"></i>' + row[1] + '</div>' +
        '<div class="metric-value ' + row[3] + '">' + escapeHtml(row[2]) + '</div>' +
        '<div class="metric-foot">' + escapeHtml(row[4]) + '</div></div>';
    }).join('');
  }

  /* ---------------------------- 主张表 ---------------------------- */

  function renderClaimRow(claim) {
    var state = CLAIM_STATE[claim.state] || { label: text(claim.state) || '状态未提供', cls: 'tag-muted' };
    return '<tr' + (claim.state === 'CONFLICT' ? ' class="tr-danger"' : '') + '>' +
      '<td class="strong">' + (text(claim.statement) ? escapeHtml(claim.statement) : '陈述未提供') +
        '<div class="f10 t-fai mono">' + escapeHtml(text(claim.claimId) || 'claimId 未提供') + '</div></td>' +
      '<td class="f11">' + sourceHtml(claim.source) + '</td>' +
      '<td><span class="tag ' + state.cls + ' no-dot f10">' + escapeHtml(state.label) + '</span></td>' +
      '<td class="f11 t-mut">接口未提供</td>' +
      '<td class="f11 t-mut">接口未提供</td>' +
      '</tr>';
  }

  function renderClaimsCard(draft) {
    var d = draftOf(draft) || {};
    var claims = listOf(d.claims);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">可用主张与拟用策略</span>' +
        '<span class="card-sub">' + claims.length + ' 条 · information-draft.claims</span></div>' +
      (claims.length
        ? '<div class="table-wrap"><table class="tbl tbl-compact">' +
            '<thead><tr><th>主张</th><th style="width:220px">证据来源</th>' +
            '<th style="width:86px">状态</th><th style="width:110px">证据强度</th>' +
            '<th style="width:130px">拟用策略</th></tr></thead>' +
            '<tbody>' + claims.map(renderClaimRow).join('') + '</tbody></table></div>' +
            '<div class="f10 t-fai" style="padding:8px 18px 12px">' +
              '「证据强度」「拟用策略」当前响应中没有对应字段，统一标为「接口未提供」，不编造。</div>'
        : renderEmpty('暂无可用主张', 'claims 为空；需先在信息审核中整理事实清单')) +
      '</section>';
  }

  /* ---------------------------- 冲突上下文 ---------------------------- */

  function renderConflictRow(conflict) {
    var state = CONFLICT_STATE[conflict.state] || { label: text(conflict.state) || '状态未提供', cls: 'tag-muted' };
    return '<tr' + (conflict.blocking === true && conflict.state !== 'SETTLED' ? ' class="tr-danger"' : '') + '>' +
      '<td class="strong mono f11">' + escapeHtml(text(conflict.conflictId) || 'conflictId 未提供') + '</td>' +
      '<td class="f11">' + escapeHtml(CONFLICT_KIND[conflict.kind] || text(conflict.kind) || '类型未提供') + '</td>' +
      '<td><span class="tag ' + state.cls + ' no-dot f10">' + escapeHtml(state.label) + '</span>' +
        (conflict.blocking === true ? ' <span class="tag tag-danger no-dot f10">阻断</span>' : '') + '</td>' +
      '<td class="f11">' + (text(conflict.action) ? escapeHtml(conflict.action) : '<span class="t-mut">未提供</span>') + '</td>' +
      '</tr>';
  }

  function renderConflictsCard(draft) {
    var d = draftOf(draft) || {};
    var conflicts = listOf(d.conflicts);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">冲突上下文</span>' +
        '<span class="card-sub">' + conflicts.length + ' 条 · 待处置 ' +
          conflicts.filter(function (c) { return c.state !== 'SETTLED'; }).length + '</span></div>' +
      (conflicts.length
        ? '<div class="table-wrap"><table class="tbl tbl-compact">' +
            '<thead><tr><th style="width:150px">冲突 ID</th><th style="width:140px">类型</th>' +
            '<th style="width:150px">状态</th><th>处置动作</th></tr></thead>' +
            '<tbody>' + conflicts.map(renderConflictRow).join('') + '</tbody></table></div>'
        : renderEmpty('暂无冲突', 'conflicts 为空')) +
      '</section>';
  }

  /* ---------------------------- 边界 / 资料选用 ---------------------------- */

  function renderAny(value) {
    if (Array.isArray(value)) {
      if (!value.length) return '<span class="t-mut">未提供</span>';
      return value.map(function (item) {
        var s = item !== null && typeof item === 'object' ? JSON.stringify(item) : text(item);
        return '<span class="code f10">' + escapeHtml(s) + '</span>';
      }).join(' ');
    }
    if (value !== null && typeof value === 'object') {
      return '<span class="f10 mono">' + escapeHtml(JSON.stringify(value)) + '</span>';
    }
    if (value === null || value === undefined || value === '') {
      return '<span class="t-mut">未提供</span>';
    }
    return escapeHtml(value);
  }

  function renderBoundariesCard(draft) {
    var d = draftOf(draft) || {};
    var boundaries = d.boundaries && typeof d.boundaries === 'object' ? d.boundaries : {};
    var keys = Object.keys(boundaries);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">表达边界</span>' +
        '<span class="card-sub">' + keys.length + ' 个键</span></div>' +
      '<div class="card-body" style="padding-top:8px;padding-bottom:8px">' +
        (keys.length
          ? '<table class="tbl tbl-compact" style="font-size:12px"><tbody>' +
              keys.map(function (key) {
                return '<tr><td class="t-mut" style="width:150px">' + escapeHtml(key) + '</td>' +
                  '<td class="strong f11">' + renderAny(boundaries[key]) + '</td></tr>';
              }).join('') + '</tbody></table>'
          : renderEmpty('暂无表达边界', 'boundaries 为空对象')) +
      '</div></section>';
  }

  function renderSelectionCard(draft) {
    var d = draftOf(draft) || {};
    var rows = listOf(d.researchSelection);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">研究资料选用</span>' +
        '<span class="card-sub">' + rows.length + ' 项</span></div>' +
      '<div class="card-body" style="padding-top:8px;padding-bottom:8px">' +
        (rows.length
          ? '<table class="tbl tbl-compact" style="font-size:12px"><tbody>' +
              rows.map(function (row, index) {
                return '<tr><td class="t-mut" style="width:56px">#' + (index + 1) + '</td>' +
                  '<td class="f11 mono">' + escapeHtml(JSON.stringify(row)) + '</td></tr>';
              }).join('') + '</tbody></table>'
          : renderEmpty('暂无研究资料选用', 'researchSelection 为空数组')) +
      '</div></section>';
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
    renderClaimsCard: renderClaimsCard,
    renderConflictsCard: renderConflictsCard,
    renderBoundariesCard: renderBoundariesCard,
    renderSelectionCard: renderSelectionCard,
    renderEmpty: renderEmpty,
    renderCardLoading: renderCardLoading,
    renderCardError: renderCardError,
    escapeHtml: escapeHtml
  };
});
