/* ============================================================================
 * render/research-files.js —— 资料文件核对页渲染层（方案 A：数据 → HTML 字符串）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 * 双环境导出：Node/CommonJS → require('./research-files.js')；浏览器 → RednoteRender.researchFiles
 *
 * 数据形状（api.js 解包后）：
 *   GET /projects/{id}/research-files → items[]:
 *     { fileId, originalName, mimeType, byteSize, classification, scanStatus,
 *       purpose, effective, uploadedBy, createdAt,
 *       parse{ state, complete, parseRunId, parserVersion, segmentCount,
 *              totalUnits, parsedUnits, errors[{unit,message}] },
 *       evidenceUsable }
 *     meta.research{ limitBytes, effectiveBytes, reservedBytes, remainingBytes,
 *                    listedBytes, fileCount, usableEvidenceCount, reconciled }
 *   GET /files/{id} → { fileId, projectId, fileName, byteSize, sha256, mimeType,
 *     classification, supported, scanStatus, storageState, uploadedBy,
 *     createdAt, latestParse }
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.researchFiles = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var PARSE_META = {
    NONE: { label: '未解析', cls: 'tag-muted' },
    QUEUED: { label: '排队中', cls: 'tag-info' },
    RUNNING: { label: '解析中', cls: 'tag-purple' },
    SUCCEEDED: { label: '成功', cls: 'tag-ok' },
    PARTIAL: { label: '部分成功', cls: 'tag-warn' },
    FAILED: { label: '失败', cls: 'tag-danger' },
    CANCELLED: { label: '已取消', cls: 'tag-muted' }
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

  function num(value) {
    var n = Number(value);
    return isFinite(n) ? n : null;
  }

  function fmtBytes(value) {
    var n = num(value);
    if (n === null) return '—';
    if (n >= 1e9) return (n / 1e9).toFixed(1) + ' GB';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + ' MB';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + ' KB';
    return n + ' B';
  }

  function quotaOf(meta) {
    return meta && meta.research && typeof meta.research === 'object' ? meta.research : null;
  }

  function parseMeta(state) {
    return PARSE_META[state] || { label: text(state) || '状态未提供', cls: 'tag-muted' };
  }

  function fileIcon(mimeType) {
    var m = text(mimeType);
    if (m.indexOf('pdf') !== -1) return 'fa-regular fa-file-pdf t-dan';
    if (m.indexOf('sheet') !== -1 || m.indexOf('excel') !== -1) return 'fa-regular fa-file-excel t-ok';
    if (m.indexOf('word') !== -1 || m.indexOf('document') !== -1) return 'fa-regular fa-file-word t-info';
    if (m.indexOf('image') !== -1) return 'fa-regular fa-image t-pur';
    if (m.indexOf('text') !== -1) return 'fa-regular fa-file-lines t-info';
    return 'fa-regular fa-file t-fai';
  }

  /* ---------------------------- 容量指标 ---------------------------- */

  function renderQuotaCards(meta) {
    var q = quotaOf(meta);
    if (!q) {
      return '<div class="card metric"><div class="metric-label"><i class="fa-solid fa-database"></i>研究配额</div>' +
        '<div class="metric-value na">—</div><div class="metric-foot">meta.research 未提供</div></div>';
    }
    var limit = num(q.limitBytes);
    var effective = num(q.effectiveBytes);
    var pct = limit && effective !== null ? Math.min(100, effective / limit * 100) : 0;
    return '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid fa-database"></i>有效研究容量</div>' +
        '<div class="metric-value">' + escapeHtml(fmtBytes(q.effectiveBytes)) + '</div>' +
        '<div class="mt10"><span class="bar"><span class="bar-fill" style="width:' + pct.toFixed(1) + '%"></span></span></div>' +
        '<div class="metric-foot">' + escapeHtml(fmtBytes(q.limitBytes)) + ' 上限 · ' + pct.toFixed(1) + '%</div></div>' +
      '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid fa-file-lines"></i>文件数</div>' +
        '<div class="metric-value">' + escapeHtml(text(q.fileCount || 0)) + '</div>' +
        '<div class="metric-foot">可用证据 ' + escapeHtml(text(q.usableEvidenceCount || 0)) + ' 份</div></div>' +
      '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid fa-hard-drive"></i>剩余容量</div>' +
        '<div class="metric-value t-ok">' + escapeHtml(fmtBytes(q.remainingBytes)) + '</div>' +
        '<div class="metric-foot">已预占 ' + escapeHtml(fmtBytes(q.reservedBytes)) + '</div></div>' +
      '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid fa-scale-balanced"></i>配额对账</div>' +
        '<div class="metric-value" style="font-size:20px">' + (q.reconciled ? '已对账' : '未对账') + '</div>' +
        '<div class="metric-foot">列表占用 ' + escapeHtml(fmtBytes(q.listedBytes)) + '</div></div>';
  }

  /* ---------------------------- 文件表 ---------------------------- */

  function renderFileRow(file) {
    var parse = file.parse && typeof file.parse === 'object' ? file.parse : {};
    var state = parseMeta(parse.state);
    var units = parse.totalUnits != null
      ? escapeHtml(text(parse.parsedUnits || 0)) + ' / ' + escapeHtml(text(parse.totalUnits))
      : '<span class="t-mut">—</span>';
    var errors = Array.isArray(parse.errors) ? parse.errors : [];
    return '<tr>' +
      '<td class="strong"><i class="' + fileIcon(file.mimeType) + '"></i> ' +
        (text(file.originalName) ? escapeHtml(file.originalName) : '文件名未提供') +
        '<div class="f10 t-fai">' + escapeHtml(fmtTime(file.createdAt) || '时间未提供') +
          ' · 上传者 <span class="mono">' + escapeHtml(shortId(file.uploadedBy) || '未提供') + '</span></div></td>' +
      '<td class="f11">' + (text(file.mimeType) ? escapeHtml(file.mimeType) : '未提供') + '</td>' +
      '<td class="num mono">' + escapeHtml(fmtBytes(file.byteSize)) + '</td>' +
      '<td><span class="tag ' + state.cls + ' no-dot f10">' + escapeHtml(state.label) + '</span></td>' +
      '<td class="f11">' + units +
        (errors.length ? '<div class="f10 t-dan">' + escapeHtml(
          errors.map(function (e) { return '单元 ' + text(e.unit) + '：' + text(e.message); }).join('；')
        ) + '</div>' : '') + '</td>' +
      '<td>' + (file.evidenceUsable
        ? '<span class="tag tag-ok no-dot f10">可用</span>'
        : '<span class="tag tag-muted no-dot f10">不可用</span>') + '</td>' +
      '<td class="num"><div class="row-act">' +
        (text(file.fileId)
          ? '<button class="btn btn-sm btn-ghost" data-file-id="' + escapeHtml(file.fileId) + '">详情</button>'
          : '<span class="t-mut">—</span>') +
      '</div></td></tr>';
  }

  function renderFilesTable(files) {
    var list = listOf(files);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">资料文件</span>' +
        '<span class="card-sub">' + list.length + ' 份 · 点击详情读取 GET /files/{id}</span></div>' +
      (list.length
        ? '<div class="table-wrap"><table class="tbl tbl-compact">' +
            '<thead><tr><th>文件名</th><th style="width:158px">类型</th>' +
            '<th class="num" style="width:82px">大小</th><th style="width:100px">解析状态</th>' +
            '<th style="width:158px">提取结果</th><th style="width:86px">证据</th>' +
            '<th class="num" style="width:72px">操作</th></tr></thead>' +
            '<tbody>' + list.map(renderFileRow).join('') + '</tbody></table></div>'
        : renderEmpty('暂无研究资料', 'research-files 返回空数组')) +
      '</section>';
  }

  /* ---------------------------- 文件详情 ---------------------------- */

  function renderDetailLoading() {
    return '<div class="row" style="gap:8px;padding:14px 0">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">正在读取文件详情…</span></div>';
  }

  function detailRows(file) {
    var rows = [
      ['文件 ID', text(file.fileId) ? '<span class="mono f11" title="' + escapeHtml(file.fileId) + '">' + escapeHtml(shortId(file.fileId)) + '</span>' : '<span class="t-mut">未提供</span>'],
      ['项目 ID', text(file.projectId) ? '<span class="mono f11" title="' + escapeHtml(file.projectId) + '">' + escapeHtml(shortId(file.projectId)) + '</span>' : '<span class="t-mut">未提供</span>'],
      ['大小', escapeHtml(fmtBytes(file.byteSize))],
      ['MIME', text(file.mimeType) ? escapeHtml(file.mimeType) : '<span class="t-mut">未提供</span>'],
      ['分类', text(file.classification) ? escapeHtml(file.classification) : '<span class="t-mut">未提供</span>'],
      ['支持', file.supported === true ? '是' : file.supported === false ? '否' : '<span class="t-mut">未提供</span>'],
      ['扫描', text(file.scanStatus) ? escapeHtml(file.scanStatus) : '<span class="t-mut">未提供</span>'],
      ['存储状态', text(file.storageState) ? escapeHtml(file.storageState) : '<span class="t-mut">未提供</span>'],
      ['上传者', text(file.uploadedBy) ? '<span class="mono f11" title="' + escapeHtml(file.uploadedBy) + '">' + escapeHtml(shortId(file.uploadedBy)) + '</span>' : '<span class="t-mut">未提供</span>'],
      ['上传时间', text(file.createdAt) ? escapeHtml(fmtTime(file.createdAt)) : '<span class="t-mut">未提供</span>'],
      ['SHA-256', text(file.sha256) ? '<span class="mono f10" style="word-break:break-all">' + escapeHtml(file.sha256) + '</span>' : '<span class="t-mut">未提供</span>'],
      ['最近解析', file.latestParse && typeof file.latestParse === 'object'
        ? '<span class="f11">' + escapeHtml(text(file.latestParse.state) || JSON.stringify(file.latestParse)) + '</span>'
        : '<span class="t-mut">null</span>']
    ];
    return rows.map(function (row) {
      return '<tr><td class="t-mut" style="width:88px">' + escapeHtml(row[0]) + '</td>' +
        '<td class="strong">' + row[1] + '</td></tr>';
    }).join('');
  }

  function renderDetail(file) {
    var f = file && typeof file === 'object' ? file : {};
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">文件详情</span>' +
        '<span class="card-sub">GET /files/{id}</span></div>' +
      '<div class="card-body" style="padding-top:8px;padding-bottom:8px">' +
        '<div class="f13 fw6 mb10">' + (text(f.fileName) ? escapeHtml(f.fileName) : '文件名未提供') + '</div>' +
        '<table class="tbl tbl-compact" style="font-size:12px"><tbody>' +
        detailRows(f) + '</tbody></table></div></section>';
  }

  function renderDetailPlaceholder() {
    return '<section class="card"><div class="card-head"><span class="card-title">文件详情</span></div>' +
      '<div class="card-body">' + renderEmpty('选择一份文件查看详情', '详情来自 GET /files/{id}') + '</div></section>';
  }

  function renderDetailError(err) {
    var e = err && typeof err === 'object' ? err : {};
    var message = text(e.message) || text(err) || '读取文件详情失败。';
    return '<section class="card"><div class="card-head"><span class="card-title">文件详情</span></div>' +
      '<div class="card-body"><div class="row" style="align-items:flex-start;gap:10px">' +
      '<i class="fa-solid fa-triangle-exclamation t-dan"></i>' +
      '<div class="col" style="flex:1;gap:4px"><span class="f12 fw6">' + escapeHtml(message) + '</span>' +
      '<span class="f10 t-mut">' + escapeHtml(text(e.code)) + '</span></div></div></div></section>';
  }

  /* ---------------------------- 页头 ---------------------------- */

  function renderHead(project, meta) {
    var p = project && typeof project === 'object' ? project : {};
    var q = quotaOf(meta);
    return '<div>' +
      '<div class="row mb6">' +
        '<span class="tag tag-purple no-dot"><i class="fa-solid fa-2"></i> 步骤 2 / 8</span>' +
        '<span class="code">' + (text(p.projectCode) ? escapeHtml(p.projectCode) : '编号未提供') + '</span>' +
        (q ? '<span class="tag tag-muted no-dot">' + escapeHtml(text(q.fileCount || 0)) + ' 份 · ' +
          escapeHtml(fmtBytes(q.effectiveBytes)) + '</span>' : '') +
      '</div>' +
      '<h1 class="page-title">核对资料文件与覆盖缺口</h1>' +
      '<p class="page-desc">' + (text(p.name) ? escapeHtml(p.name) : '项目名称未提供') +
        ' · 逐份核对解析状态、提取单元与证据可用性；缺口不阻断上传，但必须显式记录。</p></div>';
  }

  /* ---------------------------- 通用块 ---------------------------- */

  function renderEmpty(title, sub) {
    return '<div class="col" style="align-items:center;gap:6px;padding:22px 0">' +
      '<i class="fa-solid fa-inbox t-fai"></i>' +
      '<span class="f12 t-mut">' + escapeHtml(title || '暂无数据') + '</span>' +
      (sub ? '<span class="f10 t-fai">' + escapeHtml(sub) + '</span>' : '') + '</div>';
  }

  function renderBlockLoading(message) {
    return '<div class="row" style="justify-content:center;gap:8px;padding:22px 0">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">' + escapeHtml(message || '正在读取…') + '</span></div>';
  }

  function renderCardLoading(message) {
    return '<section class="card"><div class="card-body">' + renderBlockLoading(message) + '</div></section>';
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
    renderHead: renderHead,
    renderQuotaCards: renderQuotaCards,
    renderFilesTable: renderFilesTable,
    renderFileRow: renderFileRow,
    renderDetail: renderDetail,
    renderDetailPlaceholder: renderDetailPlaceholder,
    renderDetailLoading: renderDetailLoading,
    renderDetailError: renderDetailError,
    renderEmpty: renderEmpty,
    renderBlockLoading: renderBlockLoading,
    renderCardLoading: renderCardLoading,
    renderCardError: renderCardError,
    escapeHtml: escapeHtml
  };
});
