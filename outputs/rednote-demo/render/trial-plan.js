/* ============================================================================
 * render/trial-plan.js —— 生产清单 / 费用 / 提交校验渲染层（方案 A）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 * 双环境导出：Node/CommonJS → require('./trial-plan.js')；浏览器 → RednoteRender.trialPlan
 *
 * 数据形状（实测自 127.0.0.1:8787）：
 *   GET /projects/{id}/plan-draft → 同 trial-config（含两维 allocation）
 *   GET /projects/{id}/cost-estimates → { data:[{ id, projectId, batchKind,
 *     quantity, currency, state, totalAmount, actualAmount, varianceAmount,
 *     pricingVersionId, lines[{ unit, quantity, unitPrice, amount }] }] }
 *   GET /projects/{id}/budget → { projectId, currency, limit, committed,
 *     reserved, available, configured }
 *
 * 提交固化的真实前置条件（planning/service.py submit_draft）：
 *   state=EDITING、totalQuantity 已设、两维权重合计均 == 1000‰。
 * POST /cost-estimates 与 POST /plan-draft/submissions 都会写库，本页只读。
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
    root.RednoteRender.trialPlan = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function (S) {
  'use strict';

  var text = S.text;
  var escapeHtml = S.escapeHtml;
  var shortId = S.shortId;

  var ESTIMATE_STATE = {
    DRAFT: { label: '草稿', cls: 'tag-muted' },
    RESERVED: { label: '已预占', cls: 'tag-info' },
    SETTLED: { label: '已结算', cls: 'tag-ok' },
    CANCELLED: { label: '已取消', cls: 'tag-muted' }
  };

  var BATCH_KIND = { TRIAL: '试产', SCALE: '扩产', BULK: '批产' };

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

  function money(value, currency) {
    var raw = text(value);
    if (!raw) return '<span class="t-mut">未提供</span>';
    var n = Number(raw);
    var symbol = currency === 'CNY' || !currency ? '¥' : (text(currency) + ' ');
    if (!isFinite(n)) return escapeHtml(raw);
    return escapeHtml(symbol + n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  }

  function allocationOf(draft, key) {
    var d = draftOf(draft) || {};
    var alloc = d[key];
    return alloc && typeof alloc === 'object' ? alloc : null;
  }

  /* ---------------------------- 提交校验 ---------------------------- */

  function checks(draft) {
    var d = draftOf(draft) || {};
    var scenes = listOf(d.sceneItems);
    var forms = listOf(d.formItems);
    var sceneSum = scenes.reduce(function (sum, i) { return sum + permille(i.weightPermille); }, 0);
    var formSum = forms.reduce(function (sum, i) { return sum + permille(i.weightPermille); }, 0);
    var sa = allocationOf(draft, 'sceneAllocation');
    var fa = allocationOf(draft, 'formAllocation');
    return [
      {
        label: '草稿处于 EDITING',
        ok: d.state === 'EDITING',
        detail: 'state=' + (text(d.state) || '未提供')
      },
      {
        label: '已设置 totalQuantity',
        ok: d.totalQuantity != null && Number(d.totalQuantity) > 0,
        detail: d.totalQuantity == null ? 'totalQuantity=null' : 'totalQuantity=' + d.totalQuantity
      },
      {
        label: '场景权重合计 = 1000‰',
        ok: scenes.length > 0 && sceneSum === 1000,
        detail: scenes.length ? '合计 ' + sceneSum + '‰ · ' + scenes.length + ' 项' : 'sceneItems 为空'
      },
      {
        label: '形式权重合计 = 1000‰',
        ok: forms.length > 0 && formSum === 1000,
        detail: forms.length ? '合计 ' + formSum + '‰ · ' + forms.length + ' 项' : 'formItems 为空'
      }
    ].map(function (c) {
      return {
        label: c.label,
        ok: c.ok,
        detail: c.detail,
        sceneAllocated: sa ? text(sa.allocatedTotal) : '',
        formAllocated: fa ? text(fa.allocatedTotal) : ''
      };
    });
  }

  function canSubmit(draft) {
    return checks(draft).every(function (c) { return c.ok; });
  }

  /* ---------------------------- 页头 / 指标 ---------------------------- */

  function renderHead(project, draft) {
    var p = project && typeof project === 'object' ? project : {};
    var d = draftOf(draft) || {};
    var ready = canSubmit(draft);
    return '<div>' +
      '<div class="row mb6">' +
        '<span class="tag tag-purple no-dot"><i class="fa-solid fa-4"></i> 步骤 4 / 8</span>' +
        '<span class="code">' + (text(p.projectCode) ? escapeHtml(p.projectCode) : '编号未提供') + '</span>' +
        '<span class="tag ' + (ready ? 'tag-ok' : 'tag-warn') + ' no-dot">' +
          (ready ? '提交条件已满足' : '提交条件未满足') + '</span>' +
      '</div>' +
      '<h1 class="page-title">确认最终生产清单与费用</h1>' +
      '<p class="page-desc">' + (text(p.name) ? escapeHtml(p.name) : '项目名称未提供') +
        ' · 清单由 plan-draft 的两维分配派生；费用来自 cost-estimates；预算来自 budget。' +
        '提交（POST /plan-draft/submissions）与预估（POST /cost-estimates）都会写库，本页只读。</p></div>';
  }

  function renderMetrics(draft, estimates, budget) {
    var d = draftOf(draft) || {};
    var sa = allocationOf(draft, 'sceneAllocation');
    var fa = allocationOf(draft, 'formAllocation');
    var sceneAllocated = sa ? text(sa.allocatedTotal) : '—';
    var formAllocated = fa ? text(fa.allocatedTotal) : '—';
    var estimatesList = listOf(estimates);
    var b = budget && typeof budget === 'object' ? budget : null;
    return [
      ['fa-list-ol', '计划总量', d.totalQuantity == null ? '—' : d.totalQuantity, d.totalQuantity == null ? 't-warn' : '', 'plan-draft.totalQuantity'],
      ['fa-users', '场景分配', sceneAllocated, '', 'sceneAllocation.allocatedTotal'],
      ['fa-images', '形式分配', formAllocated, '', 'formAllocation.allocatedTotal'],
      ['fa-coins', '费用预估', estimatesList.length, estimatesList.length ? 't-ok' : 't-warn',
        b && b.configured === false ? '预算未配置' : 'cost-estimates 记录数']
    ].map(function (row) {
      return '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid ' + row[0] + '"></i>' + row[1] + '</div>' +
        '<div class="metric-value ' + row[3] + '">' + escapeHtml(row[2]) + '</div>' +
        '<div class="metric-foot">' + escapeHtml(row[4]) + '</div></div>';
    }).join('');
  }

  /* ---------------------------- 生产清单 ---------------------------- */

  function renderPlanLinesCard(draft) {
    var d = draftOf(draft) || {};
    var sa = allocationOf(draft, 'sceneAllocation');
    var fa = allocationOf(draft, 'formAllocation');
    var rows = [];
    [['场景', sa], ['形式', fa]].forEach(function (pair) {
      listOf(pair[1] && pair[1].lines).forEach(function (line) {
        rows.push({ dimension: pair[0], line: line });
      });
    });
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">生产清单</span>' +
        '<span class="card-sub">' + rows.length + ' 行 · 两维分配结果</span></div>' +
      (rows.length
        ? '<div class="table-wrap"><table class="tbl tbl-compact">' +
            '<thead><tr><th style="width:64px">维度</th><th>条目</th>' +
            '<th class="num" style="width:86px">分配条数</th><th class="num" style="width:86px">权重</th>' +
            '<th class="num" style="width:76px">容量</th><th class="num" style="width:76px">余数</th></tr></thead>' +
            '<tbody>' + rows.map(function (row) {
              var line = row.line;
              return '<tr>' +
                '<td><span class="tag no-dot f10">' + row.dimension + '</span></td>' +
                '<td class="strong mono f11">' + escapeHtml(text(line.key) || '键未提供') + '</td>' +
                '<td class="num mono">' + escapeHtml(text(line.allocated)) + '</td>' +
                '<td class="num mono">' + escapeHtml(text(line.weightPermille)) + '‰</td>' +
                '<td class="num mono">' + (line.capacity == null ? '<span class="t-mut">—</span>' : escapeHtml(line.capacity)) + '</td>' +
                '<td class="num mono">' + escapeHtml(text(line.remainder)) + '</td>' +
                '</tr>';
            }).join('') + '</tbody></table></div>'
        : renderEmpty('生产清单尚未生成', 'plan-draft 的两维分配为空；需先配置权重与总量')) +
      (sa || fa
        ? '<div class="card-foot"><span class="f12 t-mut">场景 total=' +
            escapeHtml(sa ? text(sa.total) : '—') + ' / 未分配 ' + escapeHtml(sa ? text(sa.unallocated) : '—') +
            ' · 形式 total=' + escapeHtml(fa ? text(fa.total) : '—') + ' / 未分配 ' +
            escapeHtml(fa ? text(fa.unallocated) : '—') + '</span></div>'
        : '') +
      '</section>';
  }

  function renderChecklistCard(draft) {
    var list = checks(draft);
    var ready = list.every(function (c) { return c.ok; });
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">提交校验</span>' +
        '<span class="tag ' + (ready ? 'tag-ok' : 'tag-warn') + ' no-dot f10">' +
        (ready ? '可提交' : '不可提交') + '</span></div>' +
      '<div class="card-body"><div class="col" style="gap:10px">' +
        list.map(function (c) {
          return '<div class="row-t">' +
            '<span class="tag ' + (c.ok ? 'tag-ok' : 'tag-danger') + ' no-dot" style="margin-top:2px">' +
              '<i class="fa-solid ' + (c.ok ? 'fa-check' : 'fa-xmark') + '"></i></span>' +
            '<div><div class="f12 fw6">' + escapeHtml(c.label) + '</div>' +
            '<div class="f11 ' + (c.ok ? 't-ok' : 't-warn') + ' mt6">' + escapeHtml(c.detail) + '</div></div>' +
            '</div>';
        }).join('') +
      '</div>' +
      '<div class="note note-info mt14"><i class="fa-solid fa-circle-info"></i><div>' +
        '提交固化：POST /projects/{id}/plan-draft/submissions，需 If-Match 与 reason。' +
        '校验口径来自后端 submit_draft：state=EDITING、totalQuantity 已设、两维权重各 1000‰。' +
      '</div></div></div></section>';
  }

  /* ---------------------------- 费用 / 预算 ---------------------------- */

  function renderEstimateRow(estimate) {
    var state = ESTIMATE_STATE[estimate.state] || { label: text(estimate.state) || '状态未提供', cls: 'tag-muted' };
    var lines = listOf(estimate.lines);
    return '<tr>' +
      '<td class="strong mono f11">' + escapeHtml(shortId(estimate.id) || 'ID 未提供') +
        '<div class="f10 t-fai">' + escapeHtml(BATCH_KIND[estimate.batchKind] || text(estimate.batchKind) || '批次未提供') +
        ' · ' + escapeHtml(text(estimate.quantity)) + ' 条</div></td>' +
      '<td><span class="tag ' + state.cls + ' no-dot f10">' + escapeHtml(state.label) + '</span></td>' +
      '<td class="num mono">' + money(estimate.totalAmount, estimate.currency) + '</td>' +
      '<td class="num mono">' + (estimate.actualAmount == null ? '<span class="t-mut">—</span>' : money(estimate.actualAmount, estimate.currency)) + '</td>' +
      '<td class="num mono">' + (estimate.varianceAmount == null ? '<span class="t-mut">—</span>' : money(estimate.varianceAmount, estimate.currency)) + '</td>' +
      '<td class="num mono">' + lines.length + '</td>' +
      '</tr>';
  }

  function renderEstimatesCard(estimates) {
    var list = listOf(estimates);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">费用预估</span>' +
        '<span class="card-sub">GET /cost-estimates · ' + list.length + ' 条</span></div>' +
      (list.length
        ? '<div class="table-wrap"><table class="tbl tbl-compact">' +
            '<thead><tr><th>预估 / 批次</th><th style="width:78px">状态</th>' +
            '<th class="num" style="width:104px">预估金额</th><th class="num" style="width:104px">实际</th>' +
            '<th class="num" style="width:96px">差异</th><th class="num" style="width:60px">行数</th></tr></thead>' +
            '<tbody>' + list.map(renderEstimateRow).join('') + '</tbody></table></div>'
        : renderEmpty('暂无费用预估', 'POST /cost-estimates 可创建（providerId 必填，会写库）；本页只读')) +
      '<div class="f10 t-fai" style="padding:0 18px 12px">' +
        '预估不预占预算；「打算花多少」与「先扣住多少」是两件事。</div>' +
      '</section>';
  }

  function renderBudgetCard(budget) {
    var b = budget && typeof budget === 'object' ? budget : null;
    if (!b) {
      return '<section class="card"><div class="card-head"><span class="card-title">项目预算</span></div>' +
        '<div class="card-body">' + renderEmpty('未获取到预算信息', 'GET /budget 无数据') + '</div></section>';
    }
    var configured = b.configured === false;
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">项目预算</span>' +
        '<span class="tag ' + (configured ? 'tag-warn' : 'tag-ok') + ' no-dot f10">' +
        (configured ? '未配置' : '已配置') + '</span></div>' +
      '<div class="card-body">' +
        '<div class="row between mb10"><span class="metric-value" style="font-size:20px">' +
          money(b.available, b.currency) + '</span><span class="f11 t-mut">可用 / 额度 ' +
          money(b.limit, b.currency) + '</span></div>' +
        '<div class="col" style="gap:6px">' +
          '<div class="row between f12"><span class="t-mut">已承诺</span><span class="mono">' + money(b.committed, b.currency) + '</span></div>' +
          '<div class="row between f12"><span class="t-mut">已冻结</span><span class="mono">' + money(b.reserved, b.currency) + '</span></div>' +
          '<div class="row between f12"><span class="t-mut">可用</span><span class="mono">' + money(b.available, b.currency) + '</span></div>' +
        '</div>' +
        (configured
          ? '<div class="note note-warn mt14"><i class="fa-solid fa-triangle-exclamation"></i><div>' +
            '<b>预算未配置</b>（configured=false）。金额为接口原值，0 不代表已批准额度。</div></div>'
          : '') +
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
    checks: checks,
    canSubmit: canSubmit,
    renderHead: renderHead,
    renderMetrics: renderMetrics,
    renderPlanLinesCard: renderPlanLinesCard,
    renderChecklistCard: renderChecklistCard,
    renderEstimatesCard: renderEstimatesCard,
    renderBudgetCard: renderBudgetCard,
    renderEmpty: renderEmpty,
    renderCardLoading: renderCardLoading,
    renderCardError: renderCardError,
    escapeHtml: escapeHtml
  };
});
