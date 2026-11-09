/* ============================================================================
 * render/plan-detail.js —— 逐行计划 / 实验分组渲染层（方案 A）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 * 双环境导出：Node/CommonJS → require('./plan-detail.js')；浏览器 → RednoteRender.planDetail
 *
 * 数据形状（实测自 127.0.0.1:8787）：
 *   GET /projects/{id}/plan-draft → 同 trial-config。
 *
 * 实验分组（M4 WP38）**没有端点**：GET /projects/{id}/experiment-groups /
 * experiments / plan-groups 均 404 RESOURCE_NOT_FOUND。本页如实降级为
 * 「无数据源」卡，不编造分组。
 *
 * 「逐行决策问题」同样没有独立数据源：本页展示的是 plan-draft 的行本身
 * （场景/形式条目、权重、停用原因、配对与分配读数），而非逐行问答。
 * ========================================================================== */

(function (root, factory) {
  var shared = (typeof module === 'object' && module && module.exports)
    ? require('./shared.js')
    : (root.RednoteRender && root.RednoteRender.shared);
  var api = factory(shared);
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.planDetail = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function (S) {
  'use strict';

  var text = S.text;
  var escapeHtml = S.escapeHtml;
  var fmtTime = S.fmtTime;

  var ITEM_STATE = {
    ENABLED: { label: '启用', cls: 'tag-ok' },
    DISABLED: { label: '停用', cls: 'tag-muted' }
  };

  function listOf(rows) {
    return Array.isArray(rows)
      ? rows.filter(function (row) { return row && typeof row === 'object'; })
      : [];
  }

  function draftOf(draft) {
    return draft && typeof draft === 'object' ? draft : null;
  }

  function permille(value) {
    var n = Number(value);
    return isFinite(n) ? n : 0;
  }

  function itemState(state) {
    return ITEM_STATE[state] || { label: text(state) || '状态未提供', cls: 'tag-muted' };
  }

  /* ---------------------------- 汇总 ---------------------------- */

  function summarize(draft) {
    var d = draftOf(draft) || {};
    return {
      scenes: listOf(d.sceneItems).length,
      forms: listOf(d.formItems).length,
      pairings: listOf(d.pairings).length,
      totalQuantity: d.totalQuantity,
      updatedRows: listOf(d.sceneItems).length + listOf(d.formItems).length
    };
  }

  /* ---------------------------- 页头 / 指标 ---------------------------- */

  function renderHead(project, draft) {
    var p = project && typeof project === 'object' ? project : {};
    var d = draftOf(draft) || {};
    var s = summarize(draft);
    return '<div>' +
      '<div class="row mb6">' +
        '<span class="tag tag-purple no-dot"><i class="fa-solid fa-4"></i> 步骤 4 / 8</span>' +
        '<span class="code">' + (text(p.projectCode) ? escapeHtml(p.projectCode) : '编号未提供') + '</span>' +
        '<span class="tag tag-muted no-dot">行 ' + s.updatedRows + ' · 配对 ' + s.pairings + '</span>' +
      '</div>' +
      '<h1 class="page-title">复核逐行决策问题与实验分组</h1>' +
      '<p class="page-desc">' + (text(p.name) ? escapeHtml(p.name) : '项目名称未提供') +
        ' · 逐行决策问题与实验分组没有独立数据源（M4 WP38 未实现）。' +
        '本页展示 plan-draft 的行本身：场景/形式条目、权重、停用原因与配对。</p></div>';
  }

  function renderMetrics(draft) {
    var s = summarize(draft);
    return [
      ['fa-table-list', '草稿行', s.updatedRows, '', 'sceneItems + formItems'],
      ['fa-diagram-next', '配对', s.pairings, s.pairings ? 't-ok' : 't-warn', 'pairings[]'],
      ['fa-list-ol', '总量', s.totalQuantity == null ? '—' : s.totalQuantity, s.totalQuantity == null ? 't-warn' : '', 'plan-draft.totalQuantity'],
      ['fa-flask', '实验分组', '无数据源', 't-warn', 'M4 WP38 · GET /experiment-groups 404']
    ].map(function (row) {
      return '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid ' + row[0] + '"></i>' + row[1] + '</div>' +
        '<div class="metric-value ' + row[3] + '">' + escapeHtml(row[2]) + '</div>' +
        '<div class="metric-foot">' + escapeHtml(row[4]) + '</div></div>';
    }).join('');
  }

  /* ---------------------------- 行与配对 ---------------------------- */

  function renderRowsCard(draft) {
    var d = draftOf(draft) || {};
    var scenes = listOf(d.sceneItems);
    var forms = listOf(d.formItems);
    var rows = [];
    scenes.forEach(function (i) { rows.push({ dim: '场景', item: i }); });
    forms.forEach(function (i) { rows.push({ dim: '形式', item: i }); });
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">草稿行（场景 / 形式）</span>' +
        '<span class="card-sub">' + rows.length + ' 行 · 数据源 plan-draft</span></div>' +
      (rows.length
        ? '<div class="table-wrap"><table class="tbl tbl-compact">' +
            '<thead><tr><th style="width:64px">维度</th><th>条目</th>' +
            '<th class="num" style="width:88px">权重</th><th style="width:70px">状态</th>' +
            '<th class="num" style="width:72px">容量</th><th>停用原因</th></tr></thead>' +
            '<tbody>' + rows.map(function (row) {
              var item = row.item;
              var state = itemState(item.state);
              return '<tr' + (item.state === 'DISABLED' ? ' style="opacity:0.6"' : '') + '>' +
                '<td><span class="tag no-dot f10">' + row.dim + '</span></td>' +
                '<td class="strong">' + (text(item.name) ? escapeHtml(item.name) : escapeHtml(text(item.key) || '键未提供')) +
                  '<div class="f10 t-fai mono">' + escapeHtml(text(item.key) || '键未提供') + '</div></td>' +
                '<td class="num mono">' + escapeHtml(text(item.weightPermille) || '0') + '‰</td>' +
                '<td><span class="tag ' + state.cls + ' no-dot f10">' + escapeHtml(state.label) + '</span></td>' +
                '<td class="num mono">' + (item.capacity == null ? '<span class="t-mut">—</span>' : escapeHtml(item.capacity)) + '</td>' +
                '<td class="f11">' + (text(item.disabledReason) ? escapeHtml(item.disabledReason) : '<span class="t-mut">—</span>') + '</td>' +
                '</tr>';
            }).join('') + '</tbody></table></div>'
        : renderEmpty('草稿行为空', 'sceneItems / formItems 均为空；需先在试产配置页写入')) +
      '<div class="f10 t-fai" style="padding:0 18px 12px">「逐行决策问题」没有独立数据源（接口未提供）；' +
        '本表只展示草稿行本身。</div>' +
      '</section>';
  }

  function renderPairingsCard(draft) {
    var d = draftOf(draft) || {};
    var pairings = listOf(d.pairings);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">场景 × 形式配对</span>' +
        '<span class="card-sub">' + pairings.length + ' 对 · pairings[]</span></div>' +
      (pairings.length
        ? '<div class="table-wrap"><table class="tbl tbl-compact">' +
            '<thead><tr><th>场景</th><th>形式</th></tr></thead>' +
            '<tbody>' + pairings.map(function (pair) {
              return '<tr>' +
                '<td class="strong mono f11">' + escapeHtml(text(pair.sceneKey) || '未提供') + '</td>' +
                '<td class="f11 mono">' + escapeHtml(text(pair.formKey) || '未提供') + '</td>' +
                '</tr>';
            }).join('') + '</tbody></table></div>'
        : renderEmpty('暂无配对', 'pairings 为空数组')) +
      '</section>';
  }

  /* ---------------------------- 分配读数 ---------------------------- */

  function renderAllocationTable(label, alloc) {
    var a = alloc && typeof alloc === 'object' ? alloc : null;
    if (!a) return '';
    var lines = listOf(a.lines);
    return '<div class="sec-title">' + escapeHtml(label) +
      '<span class="sec-sub">total ' + escapeHtml(text(a.total)) + ' · 已分配 ' +
      escapeHtml(text(a.allocatedTotal)) + ' · 未分配 ' + escapeHtml(text(a.unallocated)) + '</span></div>' +
      (lines.length
        ? '<div class="table-wrap"><table class="tbl tbl-compact">' +
            '<thead><tr><th>条目</th><th class="num">分配</th><th class="num">权重</th>' +
            '<th class="num">floor</th><th class="num">余数</th><th class="num">次序</th></tr></thead>' +
            '<tbody>' + lines.map(function (line) {
              return '<tr>' +
                '<td class="strong mono f11">' + escapeHtml(text(line.key) || '键未提供') + '</td>' +
                '<td class="num mono">' + escapeHtml(text(line.allocated)) + '</td>' +
                '<td class="num mono">' + escapeHtml(text(line.weightPermille)) + '‰</td>' +
                '<td class="num mono">' + escapeHtml(text(line.floorPart)) + '</td>' +
                '<td class="num mono">' + escapeHtml(text(line.remainder)) + '</td>' +
                '<td class="num mono">' + escapeHtml(text(line.tieRank)) + '</td>' +
                '</tr>';
            }).join('') + '</tbody></table></div>'
        : renderEmpty(label + ' 分配行为空', 'allocation.lines 为空')) +
      (Array.isArray(a.notes) && a.notes.length
        ? '<div class="f10 t-warn" style="margin-top:8px">' +
          a.notes.map(function (n) { return escapeHtml(n); }).join('<br>') + '</div>' : '');
  }

  function renderAllocationsCard(draft) {
    var d = draftOf(draft) || {};
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">两维分配读数</span>' +
        '<span class="card-sub">sceneAllocation / formAllocation</span></div>' +
      '<div class="card-body">' +
        renderAllocationTable('场景', d.sceneAllocation) +
        renderAllocationTable('形式', d.formAllocation) +
        (!d.sceneAllocation && !d.formAllocation ? renderEmpty('分配读数未提供', '两项 allocation 均为 null') : '') +
      '</div></section>';
  }

  /* ---------------------------- 实验分组（无数据源） ---------------------------- */

  function renderExperimentCard() {
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">实验分组</span>' +
        '<span class="tag tag-warn no-dot f10">无数据源</span></div>' +
      '<div class="card-body">' +
        '<div class="note note-warn"><i class="fa-solid fa-flask"></i><div>' +
          '<b>实验分组属 M4 WP38，当前没有可读端点。</b>' +
          '实测 GET /projects/{id}/experiment-groups、GET /projects/{id}/experiments、' +
          'GET /projects/{id}/plan-groups 均返回 404 RESOURCE_NOT_FOUND。' +
          '本区块不编造分组内容，待 M4 装配后再接。' +
        '</div></div>' +
        '<div class="f10 t-fai mt10">若需要对照实验，请在测试环境中由服务端装配后重新读取。</div>' +
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
    renderRowsCard: renderRowsCard,
    renderPairingsCard: renderPairingsCard,
    renderAllocationsCard: renderAllocationsCard,
    renderExperimentCard: renderExperimentCard,
    renderEmpty: renderEmpty,
    renderCardLoading: renderCardLoading,
    renderCardError: renderCardError,
    escapeHtml: escapeHtml
  };
});
