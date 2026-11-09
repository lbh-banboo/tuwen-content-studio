/* ============================================================================
 * render/baseline.js —— 生产基线页渲染层（方案 A：数据 → HTML 字符串）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 * 双环境导出：Node/CommonJS → require('./baseline.js')；浏览器 → RednoteRender.baseline
 *
 * 数据形状（api.js 解包后）：
 *   GET /projects/{id}/baselines → [{ baselineId, revisionNo, briefVersionId,
 *     rulesetVersion, supersedesId, itemCount, excludedCount, isActive, createdAt }]
 *   GET /projects/{id}/information-draft → 完成条件 readiness 与草稿规模
 *
 * POST /projects/{id}/baselines 需要 briefVersionId、selected[]、rulesetVersion、
 * changeReason；本页只读展示与前置条件，不自动创建基线。
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.baseline = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

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

  function summarize(draft, baselines) {
    var d = draftOf(draft) || {};
    var readiness = d.readiness && typeof d.readiness === 'object' ? d.readiness : {};
    var conditions = listOf(readiness.conditions);
    var list = listOf(baselines);
    return {
      baselineCount: list.length,
      active: list.filter(function (b) { return b.isActive === true; }).length,
      activeRevisionNo: d.activeBaselineRevisionNo,
      claims: listOf(d.claims).length,
      conflictsOpen: listOf(d.conflicts).filter(function (c) { return c.state !== 'SETTLED'; }).length,
      scenes: listOf(d.scenes).length,
      ready: readiness.ready === true,
      satisfied: conditions.filter(function (c) { return c.satisfied === true; }).length,
      conditions: conditions.length,
      briefVersionId: text(d.briefVersionId)
    };
  }

  /* ---------------------------- 页头 / 指标 ---------------------------- */

  function renderHead(project, draft, baselines) {
    var p = project && typeof project === 'object' ? project : {};
    var s = summarize(draft, baselines);
    return '<div>' +
      '<div class="row mb6">' +
        '<span class="tag tag-purple no-dot"><i class="fa-solid fa-3"></i> 步骤 3 / 8</span>' +
        '<span class="code">' + (text(p.projectCode) ? escapeHtml(p.projectCode) : '编号未提供') + '</span>' +
        '<span class="tag ' + (s.baselineCount ? 'tag-ok' : 'tag-warn') + ' no-dot">' +
          (s.baselineCount ? '基线 ' + s.baselineCount + ' 个' : '尚无基线') + '</span>' +
        (s.activeRevisionNo != null
          ? '<span class="tag tag-ok no-dot">生效修订 r' + escapeHtml(s.activeRevisionNo) + '</span>' : '') +
      '</div>' +
      '<h1 class="page-title">确认生产基线快照与组装顺序</h1>' +
      '<p class="page-desc">' + (text(p.name) ? escapeHtml(p.name) : '项目名称未提供') +
        ' · 基线创建后不可原地修改；后续生成绑定明确基线，不在任务执行中自动切换。</p></div>';
  }

  function renderMetrics(draft, baselines) {
    var s = summarize(draft, baselines);
    return [
      ['fa-layer-group', '基线数量', s.baselineCount, s.baselineCount ? 't-ok' : 't-warn', 'GET /baselines'],
      ['fa-code-branch', '生效修订', s.activeRevisionNo == null ? '—' : 'r' + s.activeRevisionNo, '', 'activeBaselineRevisionNo'],
      ['fa-shield-halved', '待处置冲突', s.conflictsOpen, s.conflictsOpen ? 't-dan' : 't-ok', 'conflicts.state != SETTLED'],
      ['fa-circle-check', '完成条件', s.satisfied + ' / ' + (s.conditions || 3), s.ready ? 't-ok' : 't-warn', 'readiness.ready = ' + (s.ready ? 'true' : 'false')]
    ].map(function (row) {
      return '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid ' + row[0] + '"></i>' + row[1] + '</div>' +
        '<div class="metric-value ' + row[3] + '">' + escapeHtml(row[2]) + '</div>' +
        '<div class="metric-foot">' + escapeHtml(row[4]) + '</div></div>';
    }).join('');
  }

  /* ---------------------------- 基线列表 ---------------------------- */

  function renderBaselineRow(baseline) {
    return '<tr' + (baseline.isActive === true ? ' class="tr-ok"' : '') + '>' +
      '<td class="strong mono f11">' + escapeHtml(text(baseline.baselineId) || 'baselineId 未提供') +
        '<div class="f10 t-fai">r' + escapeHtml(baseline.revisionNo == null ? '—' : baseline.revisionNo) + '</div></td>' +
      '<td class="f11">' + (text(baseline.briefVersionId) ? escapeHtml(shortId(baseline.briefVersionId)) : '<span class="t-mut">未绑定</span>') + '</td>' +
      '<td class="f11 mono">' + escapeHtml(text(baseline.rulesetVersion) || '未提供') + '</td>' +
      '<td class="num mono">' + escapeHtml(baseline.itemCount == null ? '—' : baseline.itemCount) + '</td>' +
      '<td class="num mono">' + escapeHtml(baseline.excludedCount == null ? '—' : baseline.excludedCount) + '</td>' +
      '<td>' + (baseline.isActive === true
        ? '<span class="tag tag-ok no-dot f10">生效中</span>'
        : '<span class="tag tag-muted no-dot f10">历史</span>') + '</td>' +
      '<td class="f11 mono">' + (text(baseline.createdAt) ? escapeHtml(fmtTime(baseline.createdAt)) : '未提供') + '</td>' +
      '</tr>';
  }

  function renderBaselinesCard(baselines) {
    var list = listOf(baselines);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">生产基线</span>' +
        '<span class="card-sub">' + list.length + ' 个 · 不可原地修改</span></div>' +
      (list.length
        ? '<div class="table-wrap"><table class="tbl tbl-compact">' +
            '<thead><tr><th>基线 / 修订</th><th style="width:120px">Brief 版本</th>' +
            '<th style="width:96px">规则集</th><th class="num" style="width:76px">纳入</th>' +
            '<th class="num" style="width:76px">排除</th><th style="width:86px">状态</th>' +
            '<th style="width:124px">创建时间</th></tr></thead>' +
            '<tbody>' + list.map(renderBaselineRow).join('') + '</tbody></table></div>'
        : renderEmpty('尚无生产基线', 'GET /baselines 返回空数组；满足前置条件后可 POST /baselines')) +
      '</section>';
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
      '<div class="card-body"><div class="col" style="gap:10px">' +
        conditions.map(function (cond) {
          var ok = cond.satisfied === true;
          return '<div class="row-t">' +
            '<span class="tag ' + (ok ? 'tag-ok' : 'tag-danger') + ' no-dot" style="margin-top:2px">' +
              '<i class="fa-solid ' + (ok ? 'fa-check' : 'fa-xmark') + '"></i></span>' +
            '<div><div class="f12 fw5">' + escapeHtml(CONDITION_LABELS[cond.key] || text(cond.key) || '条件未提供') + '</div>' +
            '<div class="f11 ' + (ok ? 't-ok' : 't-warn') + ' mt6">' + escapeHtml(text(cond.reason) || '原因未提供') + '</div></div>' +
            '</div>';
        }).join('') +
      '</div></div></section>';
  }

  /* ---------------------------- 草稿规模 ---------------------------- */

  function renderDraftSummaryCard(draft) {
    var d = draftOf(draft) || {};
    var s = summarize(draft, []);
    var rows = [
      ['Brief 版本', text(d.briefVersionId) ? escapeHtml(shortId(d.briefVersionId)) : '<span class="t-warn">null — 创建基线必填</span>'],
      ['事实（claims）', s.claims + ' 条'],
      ['冲突（conflicts）', s.conflictsOpen + ' 条待处置'],
      ['场景（scenes）', s.scenes + ' 个'],
      ['运营确认', d.operatorConfirmed === true ? '已确认' : d.operatorConfirmed === false ? '未确认' : '未提供'],
      ['草稿锁版本', text(d.lockVersion) || '未提供'],
      ['ETag', text(d.etag) ? escapeHtml(d.etag) : '未提供'],
      ['更新时间', text(d.updatedAt) ? escapeHtml(fmtTime(d.updatedAt)) : '未提供']
    ];
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">草稿与快照规模</span>' +
        '<span class="card-sub">information-draft</span></div>' +
      '<div class="card-body" style="padding-top:8px;padding-bottom:8px">' +
        '<table class="tbl tbl-compact" style="font-size:12px"><tbody>' +
        rows.map(function (row) {
          return '<tr><td class="t-mut" style="width:132px">' + escapeHtml(row[0]) + '</td>' +
            '<td class="strong f11">' + row[1] + '</td></tr>';
        }).join('') + '</tbody></table></div></section>';
  }

  /* ---------------------------- 创建前置条件 ---------------------------- */

  function renderCreatePrereqCard(draft) {
    var d = draftOf(draft) || {};
    var s = summarize(draft, []);
    var hasBrief = !!s.briefVersionId;
    var ready = s.ready;
    var can = hasBrief && ready;
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">创建基线前置条件</span>' +
        '<span class="tag ' + (can ? 'tag-ok' : 'tag-warn') + ' no-dot f10">' +
        (can ? '已满足' : '未满足') + '</span></div>' +
      '<div class="card-body">' +
        '<div class="col" style="gap:8px">' +
          '<div class="row between f12"><span class="t-mut">briefVersionId</span>' +
            '<span class="' + (hasBrief ? 't-ok' : 't-warn') + '">' + (hasBrief ? '已提供' : 'null') + '</span></div>' +
          '<div class="row between f12"><span class="t-mut">readiness.ready</span>' +
            '<span class="' + (ready ? 't-ok' : 't-warn') + '">' + (ready ? 'true' : 'false') + '</span></div>' +
          '<div class="row between f12"><span class="t-mut">selected[] / rulesetVersion / changeReason</span>' +
            '<span class="t-mut">需提交时提供</span></div>' +
        '</div>' +
        '<div class="note ' + (can ? 'note-info' : 'note-warn') + ' mt14">' +
          '<i class="fa-solid fa-circle-info"></i><div>' +
          (can
            ? '前置条件已满足。创建基线：POST /projects/{id}/baselines（selected 中未纳入项也须携带 exclusionReason）。本页只读，不自动创建。'
            : '当前不能创建基线：' + (!hasBrief ? 'briefVersionId 为空；' : '') +
              (!ready ? 'readiness.ready=false；' : '') +
              '创建端点 POST /projects/{id}/baselines，请求体要求 briefVersionId 与非空 selected[]。') +
        '</div></div>' +
        '<div class="f10 t-fai mt10">当前已选条目 / 规则集版本未由 information-draft 返回；' +
          'selected[] 需由客户端按基线条目快照提交。</div>' +
      '</div></section>';
  }

  /* ---------------------------- 组装顺序（静态） ---------------------------- */

  function renderAssemblyCard() {
    var items = [
      ['当前项目批准事实', '最高优先级，裁剪时优先保留'],
      ['当前产品有效资产', '按产品 / 型号关联'],
      ['当前场景与规则', '场景状态与规则集版本'],
      ['适用风格', '风格指纹与表达边界'],
      ['可迁移的跨项目方法', '标明来源品牌，不迁移跨品牌事实']
    ];
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">动态上下文组装顺序</span>' +
        '<span class="card-sub">规则说明（静态）</span></div>' +
      '<div class="card-body"><div class="timeline">' +
        items.map(function (item, index) {
          return '<div class="tl-item ' + (index < 4 ? 'purple' : '') + '">' +
            '<div class="tl-title">' + (index + 1) + '. ' + escapeHtml(item[0]) + '</div>' +
            '<div class="tl-desc">' + escapeHtml(item[1]) + '</div></div>';
        }).join('') +
      '</div>' +
      '<div class="note note-warn mt14"><i class="fa-solid fa-ban"></i><div>' +
        '<b>跨品牌事实不得迁移。</b>跨品牌表达方法可作参考但须标明来源；' +
        '保存实际选用条目、版本、片段与未纳入原因。</div></div>' +
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
    renderBaselinesCard: renderBaselinesCard,
    renderBaselineRow: renderBaselineRow,
    renderReadinessCard: renderReadinessCard,
    renderDraftSummaryCard: renderDraftSummaryCard,
    renderCreatePrereqCard: renderCreatePrereqCard,
    renderAssemblyCard: renderAssemblyCard,
    renderEmpty: renderEmpty,
    renderCardLoading: renderCardLoading,
    renderCardError: renderCardError,
    escapeHtml: escapeHtml
  };
});
