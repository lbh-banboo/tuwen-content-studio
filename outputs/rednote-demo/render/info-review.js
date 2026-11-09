/* ============================================================================
 * render/info-review.js —— 信息审核页渲染层（方案 A：数据 → HTML 字符串）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 * 双环境导出：Node/CommonJS → require('./info-review.js')；浏览器 → RednoteRender.infoReview
 *
 * 数据形状（GET /projects/{id}/information-draft，api.js 解包后）：
 *   { draftId, projectId, briefVersionId, claims[], conflicts[], scenes[],
 *     boundaries{}, researchSelection[], operatorConfirmed, lockVersion,
 *     updatedAt, etag, activeBaselineId, activeBaselineRevisionNo,
 *     readiness{ ready, conditions[{ key, satisfied, reason,
 *                                  blockingOpen[], producibleScenes[] }] } }
 *   claims[]    → { claimId, statement, source, state }   state ∈ CONFIRMED|CONFLICT|PENDING
 *   conflicts[] → { conflictId, kind, state, action?, blocking? }
 *   scenes[]    → { sceneId, state, factRefs[] }
 * PUT 需要 If-Match；本页只读展示，不自动写。
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.infoReview = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CLAIM_STATE = {
    CONFIRMED: { label: '已确认', cls: 'tag-ok' },
    CONFLICT: { label: '冲突', cls: 'tag-danger' },
    PENDING: { label: '待确认', cls: 'tag-warn' }
  };

  var CONFLICT_KIND = {
    SOURCE: '来源冲突',
    TIMELINESS: '时效冲突',
    CROSS_BRAND: '跨品牌冲突',
    FACT_VS_LIMIT: '事实与限制冲突'
  };

  var CONFLICT_STATE = {
    OPEN: { label: '待处置', cls: 'tag-warn' },
    SETTLED: { label: '已处置', cls: 'tag-ok' }
  };

  var CONFLICT_ACTION = {
    ADOPT_ONE: '采纳其一',
    RANGE_STATEMENT: '区间表述',
    ESCALATE_BLOCK: '升级阻断',
    DISABLE_SCENE: '停用场景'
  };

  var SCENE_STATE = {
    PRODUCIBLE: { label: '可生产', cls: 'tag-ok' },
    NEEDS_LIMIT: { label: '需标注限制', cls: 'tag-warn' },
    DISABLED: { label: '已停用', cls: 'tag-muted' }
  };

  var CONDITION_LABELS = {
    blockingResolved: '阻断问题已经处理',
    hasProducibleScene: '至少有一个可生产场景',
    operatorConfirmed: '运营明确点击确认'
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

  function draftOf(draft) {
    return draft && typeof draft === 'object' ? draft : null;
  }

  function summarize(draft) {
    var d = draftOf(draft) || {};
    var claims = listOf(d.claims);
    var conflicts = listOf(d.conflicts);
    var scenes = listOf(d.scenes);
    var readiness = d.readiness && typeof d.readiness === 'object' ? d.readiness : {};
    var conditions = listOf(readiness.conditions);
    return {
      claims: claims.length,
      confirmed: claims.filter(function (c) { return c.state === 'CONFIRMED'; }).length,
      conflictClaims: claims.filter(function (c) { return c.state === 'CONFLICT'; }).length,
      pending: claims.filter(function (c) { return c.state === 'PENDING'; }).length,
      conflicts: conflicts.length,
      conflictsOpen: conflicts.filter(function (c) { return c.state !== 'SETTLED'; }).length,
      blockingOpen: conflicts.filter(function (c) { return c.blocking === true && c.state !== 'SETTLED'; }).length,
      scenes: scenes.length,
      producible: scenes.filter(function (s) { return s.state === 'PRODUCIBLE'; }).length,
      ready: readiness.ready === true,
      conditions: conditions.length,
      satisfied: conditions.filter(function (c) { return c.satisfied === true; }).length
    };
  }

  function sourceText(source) {
    if (source === null || source === undefined || source === '') {
      return '<span class="t-mut">未提供</span>';
    }
    if (typeof source === 'string' || typeof source === 'number') {
      return escapeHtml(source);
    }
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
        '<span class="tag ' + (s.conflictsOpen ? 'tag-warn' : 'tag-ok') + ' no-dot">' +
          '待处置冲突 ' + s.conflictsOpen + '</span>' +
        '<span class="tag ' + (s.ready ? 'tag-ok' : 'tag-muted') + ' no-dot">' +
          (s.ready ? '完成条件已满足' : '完成条件未满足') + '</span>' +
      '</div>' +
      '<h1 class="page-title">审核产品事实、人群与表达边界</h1>' +
      '<p class="page-desc">' + (text(p.name) ? escapeHtml(p.name) : '项目名称未提供') +
        ' · 事实清单、冲突处置、场景与表达边界均来自 GET /information-draft。' +
        '阻断冲突未处置时不得创建基线。</p></div>';
  }

  function renderMetrics(draft) {
    var s = summarize(draft);
    return [
      ['fa-list-check', '审核项', s.claims, '', '已确认 ' + s.confirmed + ' · 冲突 ' + s.conflictClaims + ' · 待确认 ' + s.pending],
      ['fa-shield-halved', '阻断冲突', s.blockingOpen, s.blockingOpen ? 't-dan' : 't-ok', '未处置且 blocking=true'],
      ['fa-users', '可生产场景', s.producible, s.producible ? 't-ok' : 't-warn', '场景总数 ' + s.scenes],
      ['fa-circle-check', '完成条件', s.satisfied + ' / ' + (s.conditions || 3), s.ready ? 't-ok' : 't-warn', 'readiness.ready = ' + (s.ready ? 'true' : 'false')]
    ].map(function (row) {
      return '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid ' + row[0] + '"></i>' + row[1] + '</div>' +
        '<div class="metric-value ' + row[3] + '">' + escapeHtml(row[2]) + '</div>' +
        '<div class="metric-foot">' + escapeHtml(row[4]) + '</div></div>';
    }).join('');
  }

  /* ---------------------------- 完成条件 ---------------------------- */

  function renderReadinessCard(draft) {
    var d = draftOf(draft) || {};
    var readiness = d.readiness && typeof d.readiness === 'object' ? d.readiness : null;
    if (!readiness) {
      return '<section class="card"><div class="card-head"><span class="card-title">步骤 3 完成条件</span></div>' +
        '<div class="card-body">' + renderEmpty('readiness 未提供', 'GET /information-draft 未返回完成条件') + '</div></section>';
    }
    var conditions = listOf(readiness.conditions);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">步骤 3 完成条件</span>' +
        '<span class="tag ' + (readiness.ready ? 'tag-ok' : 'tag-warn') + ' no-dot f10">' +
        (readiness.ready ? 'ready' : 'not ready') + '</span></div>' +
      '<div class="card-body"><div class="grid g3">' +
        conditions.map(function (cond) {
          var ok = cond.satisfied === true;
          return '<div class="row-t">' +
            '<span class="tag ' + (ok ? 'tag-ok' : 'tag-danger') + ' no-dot" style="margin-top:2px">' +
              '<i class="fa-solid ' + (ok ? 'fa-check' : 'fa-xmark') + '"></i></span>' +
            '<div><div class="f12 fw5">' + escapeHtml(CONDITION_LABELS[cond.key] || text(cond.key) || '条件未提供') + '</div>' +
            '<div class="f11 ' + (ok ? 't-ok' : 't-warn') + ' mt6">' +
              escapeHtml(text(cond.reason) || '原因未提供') + '</div>' +
            (Array.isArray(cond.blockingOpen) && cond.blockingOpen.length
              ? '<div class="f10 t-dan mt6">阻断项：' + cond.blockingOpen.map(function (id) {
                  return '<span class="mono">' + escapeHtml(id) + '</span>';
                }).join('、') + '</div>' : '') +
            (Array.isArray(cond.producibleScenes) && cond.producibleScenes.length
              ? '<div class="f10 t-mut mt6">可生产场景：' + cond.producibleScenes.map(function (id) {
                  return '<span class="mono">' + escapeHtml(id) + '</span>';
                }).join('、') + '</div>' : '') +
            '</div></div>';
        }).join('') +
      '</div></div></section>';
  }

  /* ---------------------------- 事实清单 ---------------------------- */

  function renderClaimRow(claim) {
    var state = CLAIM_STATE[claim.state] || { label: text(claim.state) || '状态未提供', cls: 'tag-muted' };
    return '<tr' + (claim.state === 'CONFLICT' ? ' class="tr-danger"' : '') + '>' +
      '<td class="strong">' + (text(claim.statement) ? escapeHtml(claim.statement) : '陈述未提供') +
        '<div class="f10 t-fai mono">' + escapeHtml(text(claim.claimId) || 'claimId 未提供') + '</div></td>' +
      '<td class="f11">' + sourceText(claim.source) + '</td>' +
      '<td><span class="tag ' + state.cls + ' no-dot f10">' + escapeHtml(state.label) + '</span></td>' +
      '</tr>';
  }

  function renderClaimsCard(draft) {
    var d = draftOf(draft) || {};
    var claims = listOf(d.claims);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">产品事实与来源</span>' +
        '<span class="card-sub">claims · ' + claims.length + ' 条</span></div>' +
      (claims.length
        ? '<div class="table-wrap"><table class="tbl tbl-compact">' +
            '<thead><tr><th>事实陈述</th><th style="width:220px">来源</th>' +
            '<th style="width:92px">状态</th></tr></thead>' +
            '<tbody>' + claims.map(renderClaimRow).join('') + '</tbody></table></div>'
        : renderEmpty('暂无事实清单', 'claims 为空；需先完成 Brief 解构并整理事实')) +
      '</section>';
  }

  /* ---------------------------- 冲突 ---------------------------- */

  function renderConflictRow(conflict) {
    var state = CONFLICT_STATE[conflict.state] || { label: text(conflict.state) || '状态未提供', cls: 'tag-muted' };
    return '<tr' + (conflict.blocking === true && conflict.state !== 'SETTLED' ? ' class="tr-danger"' : '') + '>' +
      '<td class="strong mono f11">' + escapeHtml(text(conflict.conflictId) || 'conflictId 未提供') + '</td>' +
      '<td class="f11">' + escapeHtml(CONFLICT_KIND[conflict.kind] || text(conflict.kind) || '类型未提供') + '</td>' +
      '<td><span class="tag ' + state.cls + ' no-dot f10">' + escapeHtml(state.label) + '</span>' +
        (conflict.blocking === true ? ' <span class="tag tag-danger no-dot f10">阻断</span>' : '') + '</td>' +
      '<td class="f11">' + (text(conflict.action)
        ? escapeHtml(CONFLICT_ACTION[conflict.action] || conflict.action) : '<span class="t-mut">未提供</span>') + '</td>' +
      '</tr>';
  }

  function renderConflictsCard(draft) {
    var d = draftOf(draft) || {};
    var conflicts = listOf(d.conflicts);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">事实冲突与处置</span>' +
        '<span class="card-sub">conflicts · ' + conflicts.length + ' 条</span></div>' +
      (conflicts.length
        ? '<div class="table-wrap"><table class="tbl tbl-compact">' +
            '<thead><tr><th style="width:150px">冲突 ID</th><th style="width:140px">类型</th>' +
            '<th style="width:150px">状态</th><th>处置动作</th></tr></thead>' +
            '<tbody>' + conflicts.map(renderConflictRow).join('') + '</tbody></table></div>'
        : renderEmpty('暂无事实冲突', 'conflicts 为空')) +
      '</section>';
  }

  /* ---------------------------- 场景 ---------------------------- */

  function renderSceneRow(scene) {
    var state = SCENE_STATE[scene.state] || { label: text(scene.state) || '状态未提供', cls: 'tag-muted' };
    var refs = Array.isArray(scene.factRefs) ? scene.factRefs : [];
    return '<tr' + (scene.state === 'DISABLED' ? ' style="opacity:0.6"' : '') + '>' +
      '<td class="strong mono f11">' + escapeHtml(text(scene.sceneId) || 'sceneId 未提供') + '</td>' +
      '<td class="f11">' + (refs.length
        ? refs.map(function (ref) { return '<span class="code f10">' + escapeHtml(ref) + '</span>'; }).join(' ')
        : '<span class="t-mut">未提供</span>') + '</td>' +
      '<td><span class="tag ' + state.cls + ' no-dot f10">' + escapeHtml(state.label) + '</span></td>' +
      '</tr>';
  }

  function renderScenesCard(draft) {
    var d = draftOf(draft) || {};
    var scenes = listOf(d.scenes);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">人群与场景</span>' +
        '<span class="card-sub">scenes · ' + scenes.length + ' 个</span></div>' +
      (scenes.length
        ? '<div class="table-wrap"><table class="tbl tbl-compact">' +
            '<thead><tr><th style="width:170px">场景 ID</th><th>关联事实</th>' +
            '<th style="width:120px">状态</th></tr></thead>' +
            '<tbody>' + scenes.map(renderSceneRow).join('') + '</tbody></table></div>'
        : renderEmpty('暂无场景', 'scenes 为空；可生产场景是完成条件之一')) +
      '</section>';
  }

  /* ---------------------------- 表达边界 ---------------------------- */

  function renderAnyValue(value) {
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
        '<span class="card-sub">boundaries · ' + keys.length + ' 个键</span></div>' +
      '<div class="card-body" style="padding-top:8px;padding-bottom:8px">' +
        (keys.length
          ? '<table class="tbl tbl-compact" style="font-size:12px"><tbody>' +
              keys.map(function (key) {
                return '<tr><td class="t-mut" style="width:160px">' + escapeHtml(key) + '</td>' +
                  '<td class="strong f11">' + renderAnyValue(boundaries[key]) + '</td></tr>';
              }).join('') + '</tbody></table>'
          : renderEmpty('暂无表达边界', 'boundaries 为空对象')) +
      '</div></section>';
  }

  function renderResearchSelectionCard(draft) {
    var d = draftOf(draft) || {};
    var rows = listOf(d.researchSelection);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">研究资料选用</span>' +
        '<span class="card-sub">researchSelection · ' + rows.length + ' 项</span></div>' +
      '<div class="card-body" style="padding-top:8px;padding-bottom:8px">' +
        (rows.length
          ? '<table class="tbl tbl-compact" style="font-size:12px"><tbody>' +
              rows.map(function (row, index) {
                return '<tr><td class="t-mut" style="width:64px">#' + (index + 1) + '</td>' +
                  '<td class="f11 mono">' + escapeHtml(JSON.stringify(row)) + '</td></tr>';
              }).join('') + '</tbody></table>'
          : renderEmpty('暂无研究资料选用', 'researchSelection 为空数组')) +
      '</div></section>';
  }

  function renderMetaCard(draft) {
    var d = draftOf(draft) || {};
    var rows = [
      ['draftId', text(d.draftId) ? escapeHtml(shortId(d.draftId)) : '未提供'],
      ['briefVersionId', text(d.briefVersionId) ? escapeHtml(shortId(d.briefVersionId)) : 'null'],
      ['activeBaselineId', text(d.activeBaselineId) ? escapeHtml(shortId(d.activeBaselineId)) : 'null'],
      ['基线修订号', d.activeBaselineRevisionNo == null ? 'null' : escapeHtml(d.activeBaselineRevisionNo)],
      ['operatorConfirmed', d.operatorConfirmed === true ? 'true' : d.operatorConfirmed === false ? 'false' : '未提供'],
      ['锁版本 / ETag', escapeHtml(text(d.lockVersion) || '—') + ' · ' + (text(d.etag) ? escapeHtml(d.etag) : '未提供')],
      ['更新时间', text(d.updatedAt) ? escapeHtml(fmtTime(d.updatedAt)) : '未提供']
    ];
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">草稿元信息</span>' +
        '<span class="card-sub">PUT 需要 If-Match: etag</span></div>' +
      '<div class="card-body" style="padding-top:8px;padding-bottom:8px">' +
        '<table class="tbl tbl-compact" style="font-size:12px"><tbody>' +
        rows.map(function (row) {
          return '<tr><td class="t-mut" style="width:150px">' + escapeHtml(row[0]) + '</td>' +
            '<td class="strong mono f11">' + row[1] + '</td></tr>';
        }).join('') + '</tbody></table></div></section>';
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
    renderReadinessCard: renderReadinessCard,
    renderClaimsCard: renderClaimsCard,
    renderConflictsCard: renderConflictsCard,
    renderScenesCard: renderScenesCard,
    renderBoundariesCard: renderBoundariesCard,
    renderResearchSelectionCard: renderResearchSelectionCard,
    renderMetaCard: renderMetaCard,
    renderEmpty: renderEmpty,
    renderCardLoading: renderCardLoading,
    renderCardError: renderCardError,
    escapeHtml: escapeHtml
  };
});
