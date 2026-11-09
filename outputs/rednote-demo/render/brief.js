/* ============================================================================
 * render/brief.js —— Brief 解析页渲染层（方案 A：数据 → HTML 字符串）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 * 浏览器里只做一次 innerHTML；Node 单测见 render/brief.test.js。
 *
 * 双环境导出：Node/CommonJS → require('./brief.js')；浏览器 → RednoteRender.brief
 *
 * 数据形状（来自 GET /projects/{id}/brief，api.js 解包后）：
 *   { briefId, projectId, activeVersionId, version|null, versions[],
 *     blockingIssueCount,
 *     draft: { draftId, baseVersionId, lockVersion, etag,
 *              structuredFields: { "<path>": { value, status } },
 *              claims[], issueResolutions[], suggestions[],
 *              sourceFileId, sourceParseRunId, sourceState, sourceErrors[],
 *              schemaVersion, updatedAt } }
 *
 * 字段路径 / 中文标签 / 分组 / 必填标记来自后端 BriefSchema（20 个字段、10 组）；
 * GET /brief **不返回 Brief 原文正文**，本页如实说明。
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.brief = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var GROUP_LABELS = {
    1: '产品图片及使用权限',
    2: '产品名称 / 品牌 / 型号',
    3: '选购话术及购买场景',
    4: '产品解决的痛点',
    5: '核心卖点及优先级',
    6: '原理与功效及支持材料',
    7: '话题标签示例',
    8: '产品参数 / 单位 / 适用型号',
    9: '目标人群 / 场景 / 限制',
    10: '必须提及与禁止提及'
  };

  /* 与 modules/brief/schema.py BRIEF_FIELDS 一致（路径是稳定标识，标签/分组同源） */
  var FIELDS = [
    { path: 'product.images', label: '产品图片', group: 1, required: false, multi: true },
    { path: 'product.image_usage_rights', label: '图片使用权限说明', group: 1, required: false, multi: false },
    { path: 'product.name', label: '产品名称', group: 2, required: true, multi: false },
    { path: 'product.brand', label: '品牌', group: 2, required: true, multi: false },
    { path: 'product.model_code', label: '型号', group: 2, required: false, multi: false },
    { path: 'purchase.script', label: '选购话术', group: 3, required: false, multi: false },
    { path: 'purchase.scenarios', label: '适用购买场景', group: 3, required: false, multi: true },
    { path: 'product.pain_points', label: '产品解决的痛点', group: 4, required: false, multi: true },
    { path: 'product.selling_points', label: '核心卖点及优先级', group: 5, required: true, multi: true },
    { path: 'product.mechanism', label: '原理', group: 6, required: false, multi: false },
    { path: 'product.efficacy', label: '功效', group: 6, required: false, multi: false },
    { path: 'product.supporting_materials', label: '支持材料', group: 6, required: false, multi: true },
    { path: 'content.hashtag_examples', label: '话题标签示例', group: 7, required: false, multi: true },
    { path: 'product.parameters', label: '产品参数', group: 8, required: false, multi: true },
    { path: 'product.applicable_models', label: '适用型号', group: 8, required: false, multi: true },
    { path: 'audience.target', label: '目标人群', group: 9, required: true, multi: false },
    { path: 'audience.usage_scenarios', label: '使用场景', group: 9, required: false, multi: true },
    { path: 'audience.constraints', label: '限制条件', group: 9, required: false, multi: true },
    { path: 'content.must_mention', label: '必须提及内容', group: 10, required: true, multi: true },
    { path: 'content.must_not_mention', label: '禁止提及内容', group: 10, required: true, multi: true }
  ];

  var STATUS_META = {
    FILLED: { label: '已填写', cls: 'tag-ok' },
    MISSING: { label: '待补充', cls: 'tag-warn' },
    NOT_APPLICABLE: { label: '不适用', cls: 'tag-muted' }
  };

  var SOURCE_META = {
    NONE: { label: '未提供原文', cls: 'tag-muted' },
    SUCCEEDED: { label: '解构成功', cls: 'tag-ok' },
    PARTIAL: { label: '部分解构', cls: 'tag-warn' },
    FAILED: { label: '解构失败', cls: 'tag-danger' }
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

  function listOf(rows) {
    return Array.isArray(rows)
      ? rows.filter(function (row) { return row && typeof row === 'object'; })
      : [];
  }

  function draftOf(brief) {
    return brief && typeof brief === 'object' && brief.draft && typeof brief.draft === 'object'
      ? brief.draft : null;
  }

  /* ---------------------------- 结构化字段 ---------------------------- */

  function fieldEntry(fields, path) {
    var entry = fields && fields[path];
    return entry && typeof entry === 'object' ? entry : { value: null, status: 'MISSING' };
  }

  function statusMeta(status) {
    return STATUS_META[status] || { label: text(status) || '状态未提供', cls: 'tag-muted' };
  }

  function renderValue(value) {
    if (value === null || value === undefined || value === '') {
      return '<span class="t-mut">未提供</span>';
    }
    if (Array.isArray(value)) {
      if (!value.length) return '<span class="t-mut">未提供</span>';
      return value.map(function (item) {
        var s = item !== null && typeof item === 'object' ? JSON.stringify(item) : text(item);
        return '<span class="code f10">' + escapeHtml(s) + '</span>';
      }).join(' ');
    }
    if (typeof value === 'object') {
      return '<span class="f10 mono">' + escapeHtml(JSON.stringify(value)) + '</span>';
    }
    return '<span style="white-space:pre-wrap">' + escapeHtml(value) + '</span>';
  }

  function summarize(brief) {
    var draft = draftOf(brief);
    var fields = draft && draft.structuredFields ? draft.structuredFields : {};
    var out = { total: FIELDS.length, filled: 0, missing: 0, notApplicable: 0, requiredMissing: 0 };
    FIELDS.forEach(function (spec) {
      var entry = fieldEntry(fields, spec.path);
      if (entry.status === 'FILLED') out.filled += 1;
      else if (entry.status === 'NOT_APPLICABLE') out.notApplicable += 1;
      else out.missing += 1;
      if (spec.required && entry.status !== 'FILLED' && entry.status !== 'NOT_APPLICABLE') {
        out.requiredMissing += 1;
      }
    });
    out.versions = listOf(brief && brief.versions).length;
    out.activeVersionId = text(brief && brief.activeVersionId);
    return out;
  }

  function renderFieldRow(spec, entry) {
    var status = statusMeta(entry.status);
    return '<div class="fld">' +
      '<div class="fld-h">' +
        '<span class="fld-n">' + escapeHtml(spec.label) + '</span>' +
        (spec.required ? '<span class="tag tag-danger no-dot f10">必填</span>' : '') +
        '<span class="tag ' + status.cls + ' no-dot f10">' + escapeHtml(status.label) + '</span>' +
      '</div>' +
      '<div class="fld-v">' + renderValue(entry.value) + '</div>' +
      '<div class="fld-src"><i class="fa-solid fa-code"></i>' + escapeHtml(spec.path) + '</div>' +
      '</div>';
  }

  function renderStructuredCard(brief) {
    var draft = draftOf(brief);
    var fields = draft && draft.structuredFields ? draft.structuredFields : {};
    var known = {};
    FIELDS.forEach(function (spec) { known[spec.path] = true; });

    var groups = [];
    for (var g = 1; g <= 10; g += 1) {
      var specs = FIELDS.filter(function (spec) { return spec.group === g; });
      var rows = specs.map(function (spec) { return renderFieldRow(spec, fieldEntry(fields, spec.path)); }).join('');
      groups.push('<div class="sec-title" style="margin-top:14px">第 ' + g + ' 组 · ' +
        escapeHtml(GROUP_LABELS[g]) + '</div>' + rows);
    }

    var extras = Object.keys(fields).filter(function (path) { return !known[path]; });
    if (extras.length) {
      groups.push('<div class="sec-title" style="margin-top:14px">其他字段' +
        '<span class="sec-sub">响应中出现的未知路径，原样展示</span></div>' +
        extras.map(function (path) {
          var entry = fieldEntry(fields, path);
          return renderFieldRow({ label: path, path: path, required: false }, entry);
        }).join(''));
    }

    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">结构化拆解</span>' +
        '<span class="card-sub">10 组 · ' + FIELDS.length + ' 个字段（BriefSchema）</span></div>' +
      '<div class="card-body" style="max-height:720px;overflow-y:auto">' +
        (draft ? groups.join('') : renderBlockEmpty('尚无结构化草稿', 'Brief 草稿未创建或读取失败')) +
      '</div></section>';
  }

  /* ---------------------------- 原文来源 ---------------------------- */

  function renderSourceCard(brief) {
    var draft = draftOf(brief);
    if (!draft) {
      return '<section class="card"><div class="card-head"><span class="card-title">Brief 原文</span></div>' +
        '<div class="card-body">' + renderBlockEmpty('未获取到 Brief 数据', 'GET /brief 无草稿') + '</div></section>';
    }
    var source = SOURCE_META[draft.sourceState] || {
      label: text(draft.sourceState) || '状态未提供', cls: 'tag-muted'
    };
    var errors = listOf(draft.sourceErrors);

    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">Brief 原文</span>' +
        '<span class="tag ' + source.cls + ' no-dot f10">' + escapeHtml(source.label) + '</span></div>' +
      '<div class="card-body">' +
        '<div class="note note-warn mb14"><i class="fa-solid fa-circle-info"></i><div>' +
          '<b>GET /brief 不返回原文正文。</b>响应只包含结构化草稿与来源引用；' +
          '原文需通过 <code class="code f10">sourceFileId</code> 关联的文件读取。' +
        '</div></div>' +
        '<table class="tbl tbl-compact" style="font-size:12px"><tbody>' +
          '<tr><td class="t-mut" style="width:104px">原文文件</td><td>' +
            (text(draft.sourceFileId)
              ? '<span class="mono f11" title="' + escapeHtml(draft.sourceFileId) + '">' + escapeHtml(shortId(draft.sourceFileId)) + '</span>'
              : '<span class="t-mut">未提供</span>') + '</td></tr>' +
          '<tr><td class="t-mut">解析批次</td><td>' +
            (text(draft.sourceParseRunId)
              ? '<span class="mono f11" title="' + escapeHtml(draft.sourceParseRunId) + '">' + escapeHtml(shortId(draft.sourceParseRunId)) + '</span>'
              : '<span class="t-mut">未提供</span>') + '</td></tr>' +
          '<tr><td class="t-mut">草稿更新</td><td class="f11 mono">' +
            (text(draft.updatedAt) ? escapeHtml(fmtTime(draft.updatedAt)) : '未提供') + '</td></tr>' +
          '<tr><td class="t-mut">锁版本 / ETag</td><td class="f11 mono">' +
            escapeHtml(text(draft.lockVersion) || '—') + ' · ' +
            (text(draft.etag) ? escapeHtml(draft.etag) : '未提供') + '</td></tr>' +
        '</tbody></table>' +
        (errors.length
          ? '<div class="sec-title" style="margin-top:14px">来源错误</div>' + errors.map(function (err) {
              return '<div class="f10 t-dan">' + escapeHtml(err.message || JSON.stringify(err)) + '</div>';
            }).join('')
          : '') +
        '<div class="f10 t-fai mt14">发起解析需先有关联文件：POST /projects/{id}/brief-parse-jobs（body: fileId）。' +
          '当前 sourceFileId 为空，解析按钮不可用。</div>' +
      '</div></section>';
  }

  /* ---------------------------- 版本 ---------------------------- */

  function renderVersionRow(version) {
    return '<div class="list-item">' +
      '<span class="li-icon ok"><i class="fa-solid fa-code-branch"></i></span>' +
      '<div class="li-main">' +
        '<div class="li-title">第 ' + escapeHtml(version.revisionNo == null ? '—' : version.revisionNo) + ' 版 · ' +
          (text(version.productName) ? escapeHtml(version.productName) : '产品名未提供') + '</div>' +
        '<div class="li-sub">' + (text(version.summary) ? escapeHtml(version.summary) : '摘要未提供') + '</div>' +
      '</div>' +
      '<div class="li-right"><span class="f10 t-fai mono">' +
        (text(version.createdAt) ? escapeHtml(fmtTime(version.createdAt)) : '时间未提供') + '</span></div>' +
      '</div>';
  }

  function renderVersionsCard(brief) {
    var versions = listOf(brief && brief.versions);
    var active = brief && brief.activeVersionId;
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">Brief 版本</span>' +
        '<span class="card-sub">activeVersionId: ' +
          (text(active) ? escapeHtml(shortId(active)) : 'null') + '</span></div>' +
      '<div class="card-body" style="padding-top:8px;padding-bottom:8px">' +
        (versions.length
          ? versions.map(renderVersionRow).join('')
          : renderBlockEmpty('尚未固化 Brief 版本', 'versions 为空；需完成解构后 POST /brief-versions')) +
        '<div class="divider"></div>' +
        '<div class="row between f12"><span class="t-mut">阻断问题数</span>' +
          '<span class="mono ' + (Number(brief && brief.blockingIssueCount) > 0 ? 't-dan' : '') + '">' +
          escapeHtml(brief && brief.blockingIssueCount != null ? brief.blockingIssueCount : '—') + '</span></div>' +
      '</div></section>';
  }

  /* ---------------------------- 页头 / 指标 ---------------------------- */

  function renderHead(project, brief) {
    var p = project && typeof project === 'object' ? project : {};
    var s = summarize(brief);
    var versionTag = s.activeVersionId
      ? '<span class="tag tag-ok no-dot">已固化 ' + escapeHtml(shortId(s.activeVersionId)) + '</span>'
      : '<span class="tag tag-warn no-dot">未固化版本</span>';
    return '<div>' +
      '<div class="row mb6">' +
        '<span class="tag tag-purple no-dot"><i class="fa-solid fa-1"></i> 步骤 1 / 8</span>' +
        '<span class="code">' + (text(p.projectCode) ? escapeHtml(p.projectCode) : '编号未提供') + '</span>' +
        versionTag +
      '</div>' +
      '<h1 class="page-title">阅读 Brief 原文与结构化拆解</h1>' +
      '<p class="page-desc">' + (text(p.name) ? escapeHtml(p.name) : '项目名称未提供') +
        ' · 已填写 ' + s.filled + ' / ' + s.total + ' 字段 · 待补充 ' + s.missing +
        (s.requiredMissing ? ' · <b class="t-dan">必填缺失 ' + s.requiredMissing + '</b>' : '') +
      '</p></div>';
  }

  function renderMetrics(brief) {
    var s = summarize(brief);
    return [
      ['fa-list-check', '字段总数', s.total, '', 'BriefSchema 10 组 20 项'],
      ['fa-circle-check', '已填写', s.filled, s.filled ? 't-ok' : '', 'status=FILLED'],
      ['fa-circle-exclamation', '待补充', s.missing, s.missing ? 't-warn' : '', 'status=MISSING'],
      ['fa-ban', '必填缺失', s.requiredMissing, s.requiredMissing ? 't-dan' : '', '必填字段未填写且未标不适用']
    ].map(function (row) {
      return '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid ' + row[0] + '"></i>' + row[1] + '</div>' +
        '<div class="metric-value ' + row[3] + '">' + escapeHtml(row[2]) + '</div>' +
        '<div class="metric-foot">' + escapeHtml(row[4]) + '</div></div>';
    }).join('');
  }

  /* ---------------------------- 加载 / 错误 / 空态 ---------------------------- */

  function renderBlockEmpty(title, sub) {
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
    FIELDS: FIELDS,
    GROUP_LABELS: GROUP_LABELS,
    summarize: summarize,
    renderHead: renderHead,
    renderMetrics: renderMetrics,
    renderStructuredCard: renderStructuredCard,
    renderSourceCard: renderSourceCard,
    renderVersionsCard: renderVersionsCard,
    renderBlockEmpty: renderBlockEmpty,
    renderCardLoading: renderCardLoading,
    renderCardError: renderCardError,
    escapeHtml: escapeHtml
  };
});
