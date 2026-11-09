/* ============================================================================
 * render/research.js —— 项目研究页渲染层（方案 A：数据 → HTML 字符串）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 * 双环境导出：Node/CommonJS → require('./research.js')；浏览器 → RednoteRender.research
 *
 * 数据形状（api.js 解包后）：
 *   GET /projects/{id}/research-prompts → { projectId, productName, keywords[],
 *     prompts[{ taskKind, title, text, formatHint{media,fields,description},
 *                relatedFieldPaths[], keywords[] }],
 *     gaps[{ claimId, fieldPath, fieldLabel, claimText, category, resolution,
 *            state, blocking, suggestedKeywords[], note }],
 *     summary{total,coveredCount,missingCount,notApplicableCount,blockingCount},
 *     derivation{rulesVersion,payloadVersion,briefVersionId,revisionNo,
 *                claimCount,truncated,notes[]} }
 *   GET /projects/{id}/research-files → items[] + meta.research{
 *     limitBytes,effectiveBytes,reservedBytes,remainingBytes,listedBytes,
 *     fileCount,usableEvidenceCount,reconciled }
 *
 * 上传（POST /projects/{id}/uploads）会预占配额；本页只读展示，不自动发起。
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.research = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var TASK_META = {
    FACT_VERIFICATION: { label: '事实核验', cls: 'tag-info' },
    EVIDENCE_COLLECTION: { label: '证据收集', cls: 'tag-purple' }
  };

  var MEDIA_LABELS = {
    TEXT: '文本',
    IMAGE: '图片 / 截图',
    DOCUMENT: '文档',
    URL: '链接',
    ANY: '不限'
  };

  var GAP_STATE_META = {
    COVERED: { label: '已覆盖', cls: 'tag-ok' },
    MISSING: { label: '缺失', cls: 'tag-danger' },
    NOT_APPLICABLE: { label: '不适用', cls: 'tag-muted' }
  };

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

  /* ---------------------------- 容量指标 ---------------------------- */

  function renderQuotaCards(meta) {
    var q = quotaOf(meta);
    if (!q) {
      return ['有效容量', '文件数', '剩余容量', '覆盖缺口'].map(function (label) {
        return '<div class="card metric"><div class="metric-label"><i class="fa-solid fa-database"></i>' +
          label + '</div><div class="metric-value na">—</div>' +
          '<div class="metric-foot">meta.research 未提供</div></div>';
      }).join('');
    }
    var limit = num(q.limitBytes);
    var effective = num(q.effectiveBytes);
    var pct = limit && effective !== null ? Math.min(100, effective / limit * 100) : 0;

    return [
      '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid fa-database"></i>有效研究容量</div>' +
        '<div class="metric-value">' + escapeHtml(fmtBytes(q.effectiveBytes)) + '</div>' +
        '<div class="mt10"><span class="bar"><span class="bar-fill" style="width:' + pct.toFixed(1) + '%"></span></span></div>' +
        '<div class="metric-foot">' + escapeHtml(fmtBytes(q.effectiveBytes)) + ' / ' +
          escapeHtml(fmtBytes(q.limitBytes)) + ' · ' + pct.toFixed(1) + '%</div></div>',
      '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid fa-file-lines"></i>文件数</div>' +
        '<div class="metric-value">' + escapeHtml(text(q.fileCount || 0)) + '</div>' +
        '<div class="metric-foot">可用证据 ' + escapeHtml(text(q.usableEvidenceCount || 0)) + ' 份' +
          (q.reconciled ? ' · 配额已对账' : ' · 配额未对账') + '</div></div>',
      '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid fa-hard-drive"></i>剩余容量</div>' +
        '<div class="metric-value t-ok">' + escapeHtml(fmtBytes(q.remainingBytes)) + '</div>' +
        '<div class="metric-foot">已预占 ' + escapeHtml(fmtBytes(q.reservedBytes)) + ' · 列表占用 ' +
          escapeHtml(fmtBytes(q.listedBytes)) + '</div></div>',
      '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid fa-list-check"></i>证据可用</div>' +
        '<div class="metric-value">' + escapeHtml(text(q.usableEvidenceCount || 0)) + ' / ' +
          escapeHtml(text(q.fileCount || 0)) + '</div>' +
        '<div class="metric-foot">evidenceUsable=true 的文件</div></div>'
    ].join('');
  }

  /* ---------------------------- 关键词 ---------------------------- */

  function renderKeywordsCard(prompts) {
    var keywords = listOf(prompts && prompts.keywords);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">推荐关键词</span>' +
        '<span class="card-sub">' + keywords.length + ' 个 · GET /research-prompts</span></div>' +
      '<div class="card-body">' +
        (keywords.length
          ? '<div class="row wrap" style="gap:6px">' + keywords.map(function (kw) {
              var title = text(kw.sourceFieldLabel) || text(kw.sourceFieldPath) || text(kw.sourceKind);
              return '<span class="tag no-dot" title="' + escapeHtml(title) + '">' +
                escapeHtml(kw.text) + '</span>';
            }).join('') + '</div>'
          : renderEmpty('尚未生成关键词', 'keywords 为空；需先有已固化 Brief 版本')) +
      '</div></section>';
  }

  /* ---------------------------- 提示词 ---------------------------- */

  function renderPromptCard(prompt) {
    var task = TASK_META[prompt.taskKind] || { label: text(prompt.taskKind) || '任务类别未提供', cls: 'tag-muted' };
    var hint = prompt.formatHint && typeof prompt.formatHint === 'object' ? prompt.formatHint : {};
    var fields = Array.isArray(hint.fields) ? hint.fields : [];
    var related = Array.isArray(prompt.relatedFieldPaths) ? prompt.relatedFieldPaths : [];
    var kws = Array.isArray(prompt.keywords) ? prompt.keywords : [];

    return '<div class="card card-pad mb10" style="background:rgba(255,255,255,0.02)">' +
      '<div class="row between mb6">' +
        '<div class="row" style="gap:8px"><span class="tag ' + task.cls + ' no-dot f10">' +
          escapeHtml(task.label) + '</span>' +
          '<span class="f13 fw6">' + (text(prompt.title) ? escapeHtml(prompt.title) : '标题未提供') + '</span></div>' +
      '</div>' +
      '<div class="prompt-box">' + (text(prompt.text) ? escapeHtml(prompt.text) : '提示词正文未提供') + '</div>' +
      '<div class="f11 t-mut mt10">' +
        '<b class="t-2">建议返回：</b>' + escapeHtml(MEDIA_LABELS[hint.media] || text(hint.media) || '未提供') +
        (fields.length ? ' · 字段 ' + fields.map(function (f) { return escapeHtml(f); }).join(' / ') : '') +
        (text(hint.description) ? '<br>' + escapeHtml(hint.description) : '') +
        (related.length ? '<br><b class="t-2">关联字段：</b>' + related.map(function (p) { return escapeHtml(p); }).join(' / ') : '') +
        (kws.length ? '<br><b class="t-2">关键词：</b>' + kws.map(function (k) { return escapeHtml(k); }).join(' / ') : '') +
      '</div></div>';
  }

  function renderPromptsCard(prompts) {
    var list = listOf(prompts && prompts.prompts);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">推荐研究任务</span>' +
        '<span class="card-sub">' + list.length + ' 条提示词 · 复制到外部 Agent 平台使用</span></div>' +
      '<div class="card-body">' +
        (list.length ? list.map(renderPromptCard).join('')
          : renderEmpty('暂无推荐提示词', 'prompts 为空')) +
        '<div class="f10 t-fai">上传入口：POST /projects/{id}/uploads（创建上传会话并预占配额）。' +
          '本页只读展示，不自动创建会话。</div>' +
      '</div></section>';
  }

  /* ---------------------------- 缺口 ---------------------------- */

  function renderGapRow(gap) {
    var state = GAP_STATE_META[gap.state] || { label: text(gap.state) || '状态未提供', cls: 'tag-muted' };
    return '<tr' + (gap.blocking ? ' class="tr-danger"' : '') + '>' +
      '<td class="strong">' + (text(gap.claimText) ? escapeHtml(gap.claimText) : '主张未提供') +
        '<div class="f10 t-fai mono">' + escapeHtml(text(gap.claimId) || 'claimId 未提供') + '</div></td>' +
      '<td class="f11">' + (text(gap.fieldLabel) ? escapeHtml(gap.fieldLabel) : escapeHtml(text(gap.fieldPath) || '字段未提供')) + '</td>' +
      '<td><span class="tag ' + state.cls + ' no-dot f10">' + escapeHtml(state.label) + '</span>' +
        (gap.blocking ? ' <span class="tag tag-danger no-dot f10">阻断</span>' : '') + '</td>' +
      '<td class="f11">' + (Array.isArray(gap.suggestedKeywords) && gap.suggestedKeywords.length
        ? gap.suggestedKeywords.map(function (k) { return '<span class="code f10">' + escapeHtml(k) + '</span>'; }).join(' ')
        : '<span class="t-mut">未提供</span>') + '</td>' +
      '<td class="f11">' + (text(gap.note) ? escapeHtml(gap.note) : '<span class="t-mut">未提供</span>') + '</td>' +
      '</tr>';
  }

  function renderGapsCard(prompts) {
    var gaps = listOf(prompts && prompts.gaps);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">覆盖缺口</span>' +
        '<span class="card-sub">' + gaps.length + ' 条 · gaps</span></div>' +
      (gaps.length
        ? '<div class="table-wrap"><table class="tbl tbl-compact">' +
            '<thead><tr><th>主张</th><th style="width:150px">字段</th><th style="width:96px">状态</th>' +
            '<th style="width:180px">建议关键词</th><th>说明</th></tr></thead>' +
            '<tbody>' + gaps.map(renderGapRow).join('') + '</tbody></table></div>'
        : renderEmpty('暂无覆盖缺口', 'gaps 为空（没有 Brief 版本时不会产生缺口）')) +
      '</section>';
  }

  /* ---------------------------- 派生信息 ---------------------------- */

  function renderSummaryCard(prompts) {
    var s = prompts && prompts.summary && typeof prompts.summary === 'object' ? prompts.summary : {};
    var d = prompts && prompts.derivation && typeof prompts.derivation === 'object' ? prompts.derivation : {};
    var notes = Array.isArray(d.notes) ? d.notes : [];
    var rows = [
      ['关键词 / 提示词', text((prompts && prompts.keywords ? prompts.keywords.length : 0)) + ' / ' +
        text((prompts && prompts.prompts ? prompts.prompts.length : 0))],
      ['覆盖统计', '总 ' + text(s.total || 0) + ' · 已覆盖 ' + text(s.coveredCount || 0) +
        ' · 缺失 ' + text(s.missingCount || 0) + ' · 不适用 ' + text(s.notApplicableCount || 0)],
      ['阻断缺口', text(s.blockingCount || 0)],
      ['Brief 版本', text(d.briefVersionId) ? escapeHtml(shortId(d.briefVersionId)) : 'null'],
      ['规则版本', text(d.rulesVersion) || '未提供'],
      ['payload 版本', text(d.payloadVersion) || '未提供'],
      ['是否截断', d.truncated === true ? '是' : d.truncated === false ? '否' : '未提供']
    ];
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">派生口径</span>' +
        '<span class="card-sub">derivation / summary</span></div>' +
      '<div class="card-body" style="padding-top:8px;padding-bottom:8px">' +
        '<table class="tbl tbl-compact" style="font-size:12px"><tbody>' +
        rows.map(function (row) {
          return '<tr><td class="t-mut" style="width:104px">' + escapeHtml(row[0]) + '</td>' +
            '<td class="strong">' + row[1] + '</td></tr>';
        }).join('') + '</tbody></table>' +
        (notes.length ? '<div class="note note-info mt14"><i class="fa-solid fa-circle-info"></i><div>' +
          notes.map(function (n) { return escapeHtml(n); }).join('<br>') + '</div></div>' : '') +
      '</div></section>';
  }

  /* ---------------------------- 文件摘要 ---------------------------- */

  function renderFilesCard(files, projectId) {
    var list = listOf(files);
    var href = 'research-files.v2.html' + (text(projectId) ? '?id=' + encodeURIComponent(text(projectId)) : '');
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">研究资料</span>' +
        '<span class="card-sub">' + list.length + ' 份 · 最近上传优先</span>' +
        '<div class="card-head-actions"><a class="btn btn-sm btn-ghost" href="' + escapeHtml(href) + '">全部资料</a></div></div>' +
      '<div>' + (list.length
        ? list.slice(0, 6).map(function (file) {
            var parse = file.parse && typeof file.parse === 'object' ? file.parse : {};
            var meta = parseMeta(parse.state);
            return '<div class="list-item">' +
              '<span class="li-icon ' + (file.evidenceUsable ? 'ok' : '') + '">' +
                '<i class="fa-regular fa-file"></i></span>' +
              '<div class="li-main"><div class="li-title">' +
                (text(file.originalName) ? escapeHtml(file.originalName) : '文件名未提供') + '</div>' +
                '<div class="li-sub">' + escapeHtml(fmtBytes(file.byteSize)) +
                  (parse.totalUnits != null
                    ? ' · ' + escapeHtml(text(parse.parsedUnits || 0)) + ' / ' + escapeHtml(text(parse.totalUnits)) + ' 单元'
                    : '') + '</div></div>' +
              '<div class="li-right"><span class="tag ' + meta.cls + ' no-dot f10">' +
                escapeHtml(meta.label) + '</span></div></div>';
          }).join('')
        : renderEmpty('暂无研究资料', 'research-files 返回空数组')) +
      '</div></section>';
  }

  /* ---------------------------- 页头 ---------------------------- */

  function renderHead(project, prompts) {
    var p = project && typeof project === 'object' ? project : {};
    var s = prompts && prompts.summary && typeof prompts.summary === 'object' ? prompts.summary : {};
    return '<div>' +
      '<div class="row mb6">' +
        '<span class="tag tag-purple no-dot"><i class="fa-solid fa-2"></i> 步骤 2 / 8</span>' +
        '<span class="code">' + (text(p.projectCode) ? escapeHtml(p.projectCode) : '编号未提供') + '</span>' +
        '<span class="tag ' + (Number(s.blockingCount) > 0 ? 'tag-danger' : 'tag-muted') + ' no-dot">' +
          '阻断缺口 ' + escapeHtml(text(s.blockingCount || 0)) + '</span>' +
      '</div>' +
      '<h1 class="page-title">领取推荐研究任务并上传资料</h1>' +
      '<p class="page-desc">' + (text(p.name) ? escapeHtml(p.name) : '项目名称未提供') +
        ' · 复制提示词到外部 Agent 平台获取资料，再上传本系统。' +
        '关键词与缺口由已固化 Brief 派生；无 Brief 版本时只返回引导提示。</p></div>';
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
    renderHead: renderHead,
    renderQuotaCards: renderQuotaCards,
    renderKeywordsCard: renderKeywordsCard,
    renderPromptsCard: renderPromptsCard,
    renderGapsCard: renderGapsCard,
    renderSummaryCard: renderSummaryCard,
    renderFilesCard: renderFilesCard,
    renderEmpty: renderEmpty,
    renderCardLoading: renderCardLoading,
    renderCardError: renderCardError,
    escapeHtml: escapeHtml
  };
});
