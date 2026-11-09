/* ============================================================================
 * render/cost.js —— cost 页渲染层（方案 A：数据 → HTML 字符串）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读 DOM、不读浏览器全局、不做 I/O、
 * 不取「现在几点」（`now` 一律由挂载层作入参传入）。因此可直接 Node 单测。
 *
 * 双环境导出：
 *   Node/CommonJS  → require('./cost.js')
 *   浏览器          → RednoteRender.cost
 *
 * ---------------------------------------------------------------------------
 * 数据来源（全部实际读过源码，行号为准；**不以 contracts/openapi.json 为准**，
 * 因为 tools/contracts/export_openapi.py:59 _strip_titles() 会删掉所有名为
 * title 的响应字段，快照与运行时不一致）：
 *
 *   GET /projects/{projectId}/budget            billing/api.py:388
 *     → BudgetBody                              billing/schema.py:295-313
 *       { projectId, currency, limit, committed, reserved, available, configured }
 *       金额全部是**字符串**（money.to_wire），configured=false 表示从未设过预算。
 *
 *   GET /projects/{projectId}/ledger-summary    billing/api.py:746
 *     → LedgerSummaryBody                       billing/schema.py:536-558
 *       { projectId, byKind:{KIND:金额串}, net, entryCount, truncated }
 *       truncated=true ⇒ net **不是**全量净额（schema.py:556 原文）。
 *
 *   GET /projects/{projectId}/ledger-entries    billing/api.py:711（?since=&limit≤100）
 *     → LedgerEntryResponseBody[]               billing/schema.py:347-359
 *       { id, projectId, entryKind, currency, amount, quantity, unit, description }
 *       ⚠️ **没有 createdAt / jobId / providerId / contentId / batchId**。
 *       响应是 DataEnvelope（无 page 块）⇒ 不可游标翻页，只能靠 since 增量。
 *
 *   GET /projects/{projectId}/cost-estimates    billing/api.py:852（?states=&limit≤100）
 *     → CostEstimateBody                        billing/schema.py:381-396
 *       { id, projectId, batchKind, quantity, currency, state, totalAmount,
 *         actualAmount, varianceAmount, pricingVersionId, lines[] }
 *       lines → EstimateLineBody{unit,quantity,unitPrice,amount}  schema.py:366-374
 *
 *   GET /projects/{projectId}/budget-reservations  billing/api.py:494（?states=&limit≤100）
 *     → ReservationBody                         billing/schema.py:327-340
 *       { id, projectId, state, currency, amount, settledAmount, ref, note, estimateId }
 *
 *   GET /pricing-versions                       billing/api.py:979
 *     → PricingVersionBody                      billing/schema.py:403-418
 *       { id, providerId, modelProfileId, state, currency, label, effectiveFrom, lines[] }
 *       ⚠️ 查询参数名是蛇形 `provider_id`（billing/api.py:989），与全站 camelCase 不一致。
 *
 *   POST /billing/reconciliations               billing/api.py:1028
 *     → ReconciliationReportBody                billing/schema.py:472-499
 *       { scanned, resolved, pending[], pendingCount, drifts[], projectId,
 *         truncated, projectsChecked, healthy }
 *       DriftBody                                billing/schema.py:450-460
 *       PendingUsageBody                         billing/schema.py:425-443
 *
 * 受控取值域（billing/types.py，下拉与分组一律用这几组，不自创）：
 *   LEDGER_KINDS      USAGE / BILL / REVERSAL / ADJUSTMENT   types.py:160-175
 *     正数种类 = USAGE + BILL（types.py:194），负数种类 = REVERSAL（types.py:187）
 *   PRICING_UNITS     TOKEN / IMAGE / SECOND / CALL          types.py:35-48
 *   CURRENCIES        CNY / USD                              types.py:61-64
 *   PRICING_STATES    DRAFT / PUBLISHED / RETIRED             types.py:74-78
 *   RESERVATION_STATES HELD / SETTLED / RELEASED / EXPIRED    types.py:100-117
 *   ESTIMATE_STATES   DRAFT / RESERVED / SETTLED / CANCELLED  types.py:211-226
 *
 * ---------------------------------------------------------------------------
 * 降级登记（每条都写明理由，防后人「顺手优化」时把它改回编造数据）：
 *
 *   D1 预算「硬限制模式 / 提醒模式」——  billing/types.py 里
 *      mode / HARD / SOFT / REMIND 全部零命中，**字段不存在**。原型的模式标签
 *      只能标为「模式字段未实现」，不得二选一地显示其中之一。
 *   D2 「待核对 ¥74.80」—— 无 GET 来源，只能由 POST /billing/reconciliations
 *      的响应得到。GET 页加载时不得发 POST，所以改为按钮触发后才渲染。
 *   D3 「单篇通过成本」—— 需要「通过条数」，而 content_reviews 没有任何 GET
 *      （feedback/api.py 全文只有 2 个 @router.get：:361 preview、:508 feedback 列表），
 *      审核结论只能写不能读 ⇒ 无读源，整卡降级。
 *   D4 归属维度（按批次 / 按发起人 / 按供应商）—— LedgerEntryResponseBody 里
 *      batch / actor / provider 一个都没有。唯一相关的是自由文本 description，
 *      **不得解析它来伪造归属**（那是最隐蔽的一种编造）⇒ 三块合并为一张降级卡。
 *   D5 「任务类型与用量分布」—— 无 taskKind 字段。改口径为
 *      「按分录类型 × 计量单位分布」并**改标题**，不得沿用「任务类型」这个名字。
 *   D6 AI 任务队列 —— jobs / job_attempts 表存在（modules/jobs/models.py:185、:283），
 *      但 modules/jobs 无 api.py、main.py 无 jobs_router、契约 86 paths 里
 *      /jobs /tasks /task-runs 零命中 ⇒ 渲染「无 HTTP 端点」降级卡，
 *      **不渲染空表格**（空表格会被误读为「当前无任务」）。
 *   D7 费用分组「研究 / 生产 / 报告」三段条 —— 分组维度本身无字段 ⇒
 *      替换为 byKind 的真实分布并改名。
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.cost = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  /* 受控取值域 —— 与 billing/types.py 同序（sorted 用于 SQL，这里用展示序） */
  var LEDGER_KINDS = ['USAGE', 'BILL', 'REVERSAL', 'ADJUSTMENT'];
  var POSITIVE_KINDS = ['USAGE', 'BILL'];
  var NEGATIVE_KINDS = ['REVERSAL'];
  var PRICING_UNITS = ['TOKEN', 'IMAGE', 'SECOND', 'CALL'];

  var KIND_META = {
    USAGE: { label: '用量（USAGE）', cls: 'tag-warn' },
    BILL: { label: '账单（BILL）', cls: 'tag-warn' },
    REVERSAL: { label: '冲正（REVERSAL）', cls: 'tag-ok' },
    ADJUSTMENT: { label: '调整（ADJUSTMENT）', cls: '' }
  };

  var ESTIMATE_STATE_META = {
    DRAFT: { label: '草稿', cls: '' },
    RESERVED: { label: '已预留', cls: 'tag-warn' },
    SETTLED: { label: '已结算', cls: 'tag-ok' },
    CANCELLED: { label: '已取消', cls: 'tag-danger' }
  };

  var RESERVATION_STATE_META = {
    HELD: { label: '占用中', cls: 'tag-warn' },
    SETTLED: { label: '已结算', cls: 'tag-ok' },
    RELEASED: { label: '已释放', cls: '' },
    EXPIRED: { label: '已过期', cls: 'tag-danger' }
  };

  var PRICING_STATE_META = {
    DRAFT: { label: '草稿', cls: '' },
    PUBLISHED: { label: '已发布', cls: 'tag-ok' },
    RETIRED: { label: '已退役', cls: 'tag-muted' }
  };

  var UNIT_LABEL = {
    TOKEN: 'TOKEN（词元）',
    IMAGE: 'IMAGE（张）',
    SECOND: 'SECOND（秒）',
    CALL: 'CALL（次）'
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

  function truncate(value, max) {
    var s = text(value);
    return s.length > max ? s.slice(0, max) + '…' : s;
  }

  function shortId(value) {
    var s = text(value);
    if (!s) return '';
    return s.length > 16 ? s.slice(0, 8) + '…' + s.slice(-4) : s;
  }

  function fmtTime(value) {
    var s = text(value);
    var m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(s);
    if (m) return m[1] + '-' + m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5];
    var d = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
    return d ? s : s;
  }

  function rows(value) {
    return Array.isArray(value)
      ? value.filter(function (row) { return row && typeof row === 'object'; })
      : [];
  }

  /** 后端金额是十进制字符串（money.to_wire，补零到 8 位小数）。
   *  这里只做展示格式化：原始串是权威值，调用方把它放进 title。
   *  **不用 Intl** —— 不同 Node 版本输出不同，会让单测不稳定。 */
  function money(amount, currency) {
    var raw = text(amount);
    if (!raw) return '—';
    var n = Number(raw);
    if (!isFinite(n)) return truncate(raw, 24);
    var sign = n < 0 ? '-' : '';
    var abs = Math.abs(n);
    var fixed = abs.toFixed(2);
    var parts = fixed.split('.');
    var int = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    var cur = text(currency);
    var symbol = cur === 'CNY' ? '¥' : (cur === 'USD' ? '$' : (cur ? cur + ' ' : ''));
    return sign + symbol + int + '.' + parts[1];
  }

  function numOf(value) {
    var n = Number(text(value));
    return isFinite(n) ? n : 0;
  }

  /* ---------------------------- 纯计算 ---------------------------- */

  /** 正数分录小计（USAGE + BILL，billing/types.py:194 POSITIVE_LEDGER_KINDS） */
  function sumPositive(byKind) {
    var map = byKind && typeof byKind === 'object' ? byKind : {};
    return POSITIVE_KINDS.reduce(function (acc, k) { return acc + numOf(map[k]); }, 0);
  }

  /** 负数分录小计（REVERSAL，billing/types.py:187 NEGATIVE_LEDGER_KINDS） */
  function sumReversal(byKind) {
    var map = byKind && typeof byKind === 'object' ? byKind : {};
    return NEGATIVE_KINDS.reduce(function (acc, k) { return acc + numOf(map[k]); }, 0);
  }

  /**
   * 预算使用率 = (committed + reserved) / limit。
   * configured === false ⇒ 返回 {pct:null}，页面显示「未配置预算」而不是 0%
   *   —— 演示库里 8 个项目 configured=false 是真实状态，0% 会被读成「还没花钱」。
   * limit <= 0 也返回 null（除零不能悄悄变 0）。
   */
  function usageRatio(budget) {
    var b = budget && typeof budget === 'object' ? budget : null;
    if (!b) return { pct: null, text: '无预算数据', used: 0, limit: 0 };
    if (b.configured === false) {
      return { pct: null, text: '未配置预算（configured=false）', used: 0, limit: 0 };
    }
    var limit = numOf(b.limit);
    var used = numOf(b.committed) + numOf(b.reserved);
    if (!(limit > 0)) {
      return { pct: null, text: '额度为 0 或缺失，使用率不可计算', used: used, limit: limit };
    }
    var pct = used / limit * 100;
    return { pct: pct, text: pct.toFixed(1) + '%', used: used, limit: limit };
  }

  /** 请求描述符：对账只能由写动作得到（降级 D2），绑在按钮上而非页面加载。 */
  function reconcileRequest(projectId) {
    return {
      method: 'POST',
      path: '/billing/reconciliations',
      query: { projectId: text(projectId), includeDrifts: true },
      body: {}
    };
  }

  /* ---------------------------- 公共片段 ---------------------------- */

  function card(title, sub, body, extraHead) {
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">' + escapeHtml(title) + '</span>' +
      (sub ? '<span class="card-sub">' + escapeHtml(sub) + '</span>' : '') +
      (extraHead || '') +
      '</div><div class="card-body">' + body + '</div></section>';
  }

  function doctrineBadge() {
    return '<span class="tag tag-muted no-dot f10">标准文本 · 非系统数据</span>';
  }

  function noSourceBadge() {
    return '<span class="tag tag-muted no-dot f10">无数据源</span>';
  }

  function note(cls, strong, body) {
    return '<div class="note note-' + cls + '">' +
      '<i class="fa-solid fa-' + (cls === 'warn' ? 'triangle-exclamation' : 'circle-info') + '"></i>' +
      '<div>' + (strong ? '<b>' + escapeHtml(strong) + '</b> ' : '') + escapeHtml(body) + '</div></div>';
  }

  function kv(label, value, mono) {
    return '<div class="row between f12"><span class="t-mut">' + escapeHtml(label) + '</span>' +
      '<span class="' + (mono ? 'mono' : '') + '">' + value + '</span></div>';
  }

  function barRow(label, pct, valueHtml, cls) {
    var w = (pct === null || pct === undefined || !isFinite(pct)) ? 0 : Math.max(0, Math.min(100, pct));
    return '<div class="bar-row">' +
      '<span class="f11 t-mut" style="width:148px">' + escapeHtml(label) + '</span>' +
      '<span class="bar"><span class="bar-fill' + (cls ? ' ' + cls : '') +
        '" style="width:' + w.toFixed(1) + '%"></span></span>' +
      '<span class="bar-num f11">' + valueHtml + '</span></div>';
  }

  function emptyBlock(message, hint) {
    return '<div class="col" style="align-items:center;gap:6px;padding:20px 0;">' +
      '<i class="fa-solid fa-inbox t-fai"></i>' +
      '<span class="f12 t-mut">' + escapeHtml(message) + '</span>' +
      (hint ? '<span class="f10 t-fai">' + escapeHtml(hint) + '</span>' : '') +
      '</div>';
  }

  /* ---------------------------- 预算 ---------------------------- */

  function renderBudgetCard(budget) {
    var b = budget && typeof budget === 'object' ? budget : null;
    if (!b) {
      return card('预算控制', 'GET /projects/{id}/budget',
        emptyBlock('未获取到预算数据。', '接口未返回 BudgetBody'));
    }

    var ratio = usageRatio(b);
    var cur = b.currency;
    var body = [];

    if (ratio.pct === null) {
      body.push('<div class="row between mb10">' +
        '<span class="metric-value" style="font-size:22px">—</span>' +
        '<span class="f11 t-mut">' + escapeHtml(ratio.text) + '</span></div>');
      body.push(note('warn', '使用率不可计算。',
        b.configured === false
          ? '该项目从未设过预算（configured=false），四个金额均为账面原值；0 不代表已批准额度，更不代表「已用 0%」。'
          : '额度缺失或为 0，使用率留空而不显示 0%。'));
    } else {
      var cls = ratio.pct >= 95 ? 'danger' : (ratio.pct >= 80 ? 'warn' : 'ok');
      body.push('<div class="row between mb10">' +
        '<span class="metric-value" style="font-size:22px">' + escapeHtml(ratio.text) + '</span>' +
        '<span class="f11 t-mut">已用（已计费 + 进行中占用） / 额度</span></div>');
      body.push('<span class="bar bar-lg mb14"><span class="bar-fill ' + cls +
        '" style="width:' + Math.min(100, ratio.pct).toFixed(1) + '%"></span></span>');
    }

    body.push('<div class="col" style="gap:6px">');
    body.push(kv('额度 limit', '<span title="' + escapeHtml(text(b.limit)) + '">' +
      escapeHtml(money(b.limit, cur)) + '</span>', true));
    body.push(kv('实际已计费 committed', '<span title="' + escapeHtml(text(b.committed)) + '">' +
      escapeHtml(money(b.committed, cur)) + '</span>', true));
    body.push(kv('进行中占用 reserved', '<span title="' + escapeHtml(text(b.reserved)) + '">' +
      escapeHtml(money(b.reserved, cur)) + '</span>', true));
    body.push(kv('可用余额 available', '<span title="' + escapeHtml(text(b.available)) + '">' +
      escapeHtml(money(b.available, cur)) + '</span>', true));
    body.push(kv('币种 currency', escapeHtml(text(b.currency) || '—'), true));
    body.push('</div>');

    return card('预算控制', 'GET /projects/{id}/budget · 四个金额一起返回', body.join(''));
  }

  /** 降级 D1：预算模式字段不存在 —— 不得二选一地显示「硬限制」或「提醒」。 */
  function renderBudgetModeNotice() {
    return card('预算模式', '', note('warn', '「硬限制模式 / 提醒模式」无字段。',
      'billing/types.py 里 mode / HARD / SOFT / REMIND 全部零命中，BudgetBody 只返回 ' +
      'limit / committed / reserved / available / configured 五项。原型里的模式标签' +
      '在后端补上该字段之前不显示，以免二选一地编造其中之一。'),
      noSourceBadge());
  }

  /* ---------------------------- 汇总卡 ---------------------------- */

  /**
   * 五卡：3 真 2 降级。
   *   实际费用   ← byKind.USAGE + byKind.BILL
   *   退款/抵扣  ← byKind.REVERSAL（+ ADJUSTMENT 单列）
   *   进行中占用 ← budget.reserved
   *   待核对     ← 降级 D2（只能由 POST 对账得到）
   *   单篇通过成本 ← 降级 D3（审核不可读）
   */
  function renderSummaryCards(summary, budget) {
    var s = summary && typeof summary === 'object' ? summary : null;
    var b = budget && typeof budget === 'object' ? budget : null;
    var cur = (s && s.currency) || (b && b.currency) || 'CNY';

    function metric(label, valueHtml, footHtml, badge) {
      return '<div class="card metric">' +
        '<div class="metric-label">' + escapeHtml(label) +
          (badge ? ' ' + badge : '') + '</div>' +
        '<div class="metric-value">' + valueHtml + '</div>' +
        '<div class="metric-foot f10 t-mut">' + footHtml + '</div></div>';
    }

    var cards = [];
    if (s) {
      var pos = sumPositive(s.byKind);
      var rev = sumReversal(s.byKind);
      var adj = numOf((s.byKind || {}).ADJUSTMENT);
      cards.push(metric('实际费用', escapeHtml(money(String(pos), cur)),
        'byKind.USAGE + byKind.BILL · ' + escapeHtml(text(s.entryCount)) + ' 条分录'));
      cards.push(metric('冲正（退款 / 抵扣）', escapeHtml(money(String(rev), cur)),
        'byKind.REVERSAL（负数分录）'));
      cards.push(metric('调整 ADJUSTMENT', escapeHtml(money(String(adj), cur)),
        'byKind.ADJUSTMENT · 可正可负'));
      cards.push(metric('净额 net', '<span title="' + escapeHtml(text(s.net)) + '">' +
        escapeHtml(money(s.net, cur)) + '</span>',
        s.truncated === true ? '⚠️ truncated=true，net 非全量净额' : '全部分录金额之和（含负数冲正）'));
    } else {
      cards.push(metric('实际费用', '—', '未获取到 ledger-summary', noSourceBadge()));
    }

    if (b) {
      cards.push(metric('进行中占用', '<span title="' + escapeHtml(text(b.reserved)) + '">' +
        escapeHtml(money(b.reserved, b.currency)) + '</span>', 'budget.reserved'));
    }

    /* 降级 D2 / D3：两张卡显示「无数据源」而不是 0 —— 0 会被读成「真的是 0 条」 */
    cards.push(metric('待核对', '—',
      '需 POST /billing/reconciliations（写动作）。点页头「处理待核对」后才有值。',
      noSourceBadge()));
    cards.push(metric('单篇通过成本', '—',
      'content_reviews 无任何 GET（feedback/api.py 只有 2 个 @router.get），通过条数不可读。',
      noSourceBadge()));

    return '<div class="grid g4">' + cards.join('') + '</div>';
  }

  /** truncated=true 必须显式渲染：这是「缺失不归零」纪律的落点。 */
  function renderTruncationWarning(summary) {
    var s = summary && typeof summary === 'object' ? summary : null;
    if (!s || s.truncated !== true) return '';
    return note('warn', '账本汇总已被 limit 截断。',
      '参与统计的分录 ' + text(s.entryCount) + ' 条，truncated=true ⇒ net 不是全量净额' +
      '（billing/schema.py:556 原文）。请用 ledger-entries 的 since 参数增量读取后再判断总额。');
  }

  /** 降级 D7：替代原型的「研究 / 生产 / 报告」三段条 —— 那三个分组维度无字段。 */
  function renderKindDistribution(summary) {
    var s = summary && typeof summary === 'object' ? summary : null;
    var map = (s && s.byKind && typeof s.byKind === 'object') ? s.byKind : null;
    if (!map || !Object.keys(map).length) {
      return card('按分录类型分布', 'ledger-summary.byKind',
        emptyBlock('账本里暂无分录。', 'byKind 为空对象；这是「真的没有分录」，不是缺字段'));
    }

    var cur = (s && s.currency) || 'CNY';
    var items = LEDGER_KINDS.filter(function (k) {
      return Object.prototype.hasOwnProperty.call(map, k);
    }).map(function (k) {
      return { kind: k, amount: numOf(map[k]), raw: text(map[k]) };
    });
    /* 未在受控取值域里的键原样列出，不丢、也不归到已有类里 */
    Object.keys(map).forEach(function (k) {
      if (LEDGER_KINDS.indexOf(k) === -1) {
        items.push({ kind: k, amount: numOf(map[k]), raw: text(map[k]), unknown: true });
      }
    });

    var max = items.reduce(function (acc, it) {
      return Math.max(acc, Math.abs(it.amount));
    }, 0);

    var body = items.map(function (it) {
      var meta = KIND_META[it.kind];
      var label = meta ? meta.label : it.kind + '（未知类型）';
      var pct = max > 0 ? Math.abs(it.amount) / max * 100 : 0;
      var cls = it.amount < 0 ? 'ok' : (meta ? '' : '');
      return barRow(truncate(label, 24), pct,
        '<span title="' + escapeHtml(it.raw) + '">' + escapeHtml(money(it.raw, cur)) + '</span>', cls);
    }).join('');

    return card('按分录类型分布', '替代原型的「研究 / 生产 / 报告」分组 —— 那三个维度在 ledger 行里无字段',
      body + (s.truncated === true ? renderTruncationWarning(s) : ''));
  }

  /**
   * 降级 D5：原型「任务类型与用量分布」→ 改口径为「分录类型 × 计量单位」。
   * 标题必须改名：ledger 行里没有 taskKind，叫「任务类型」就是说谎。
   */
  function renderUnitDistribution(entries) {
    var list = rows(entries);
    if (!list.length) {
      return card('分录类型 × 计量单位分布', '替代原型的「任务类型与用量分布」（无 taskKind 字段）',
        emptyBlock('暂无费用分录。', 'GET /projects/{id}/ledger-entries 返回空数组'));
    }

    var tally = new Map();
    list.forEach(function (row) {
      var kind = text(row.entryKind) || '（未标注类型）';
      var unit = text(row.unit) || '（未标注单位）';
      var key = kind + ' · ' + unit;
      var cell = tally.get(key);
      if (!cell) {
        cell = { key: key, kind: kind, unit: unit, count: 0, amount: 0, quantity: 0, hasQuantity: false };
        tally.set(key, cell);
      }
      cell.count += 1;
      cell.amount += numOf(row.amount);
      if (row.quantity !== null && row.quantity !== undefined && text(row.quantity) !== '') {
        cell.hasQuantity = true;
        cell.quantity += numOf(row.quantity);
      }
    });

    var cells = Array.from(tally.values());
    var maxCount = cells.reduce(function (a, c) { return Math.max(a, c.count); }, 0);
    var cur = text(list[0].currency) || 'CNY';

    var unknownUnits = cells.filter(function (c) {
      return c.unit !== '（未标注单位）' && PRICING_UNITS.indexOf(c.unit) === -1;
    });

    var body = cells.map(function (c) {
      var label = (KIND_META[c.kind] ? KIND_META[c.kind].label : c.kind) + ' · ' +
        (UNIT_LABEL[c.unit] || c.unit);
      return barRow(truncate(label, 30), maxCount > 0 ? c.count / maxCount * 100 : 0,
        c.count + ' 条 / ' + escapeHtml(money(String(c.amount), cur)) +
        (c.hasQuantity ? ' / 量 ' + c.quantity : ''));
    }).join('');

    return card('分录类型 × 计量单位分布',
      '计量单位受控取值域 TOKEN / IMAGE / SECOND / CALL（billing/types.py:35-48）',
      body + (unknownUnits.length
        ? note('warn', '出现受控取值域外的计量单位：',
            unknownUnits.map(function (c) { return c.unit; }).join('、') +
            '。原样显示，不归并到已知单位。')
        : ''));
  }

  /* ---------------------------- 费用明细 ---------------------------- */

  function renderLedgerTable(entries) {
    var list = rows(entries);
    var head = '<thead><tr>' +
      '<th style="width:112px">分录 ID</th>' +
      '<th style="width:124px">类型</th>' +
      '<th class="num" style="width:104px">金额</th>' +
      '<th class="num" style="width:76px">数量</th>' +
      '<th style="width:88px">单位</th>' +
      '<th>说明 description</th>' +
      '</tr></thead>';

    var body = list.length
      ? list.map(function (row) {
          var meta = KIND_META[text(row.entryKind)];
          var tagCls = 'tag' + (meta && meta.cls ? ' ' + meta.cls : '') + ' no-dot f10';
          var kindLabel = meta ? meta.label : (text(row.entryKind) || '未标注');
          return '<tr' + (numOf(row.amount) < 0 ? ' class="tr-warn"' : '') + '>' +
            '<td class="f11 mono" title="' + escapeHtml(text(row.id)) + '">' +
              escapeHtml(shortId(row.id) || '—') + '</td>' +
            '<td><span class="' + tagCls + '">' + escapeHtml(truncate(kindLabel, 20)) + '</span></td>' +
            '<td class="num mono f11" title="' + escapeHtml(text(row.amount)) + '">' +
              escapeHtml(money(row.amount, row.currency)) + '</td>' +
            '<td class="num mono f11">' +
              (text(row.quantity) ? escapeHtml(truncate(text(row.quantity), 16)) : '<span class="t-mut">—</span>') + '</td>' +
            '<td class="f11 mono">' +
              (text(row.unit) ? escapeHtml(truncate(text(row.unit), 12)) : '<span class="t-mut">—</span>') + '</td>' +
            '<td class="f11">' +
              (text(row.description) ? escapeHtml(truncate(text(row.description), 90)) : '<span class="t-mut">—</span>') + '</td>' +
            '</tr>';
        }).join('')
      : '<tr><td colspan="6">' + emptyBlock('暂无费用分录。',
          '项目产生用量后会自动出现在这里') + '</td></tr>';

    return card('费用明细',
      'GET /projects/{id}/ledger-entries · limit≤100',
      note('info', '本表只有 6 个字段。',
        'LedgerEntryResponseBody（billing/schema.py:347-359）**没有 createdAt**，' +
        '所以表里没有时间列；也没有 jobId / providerId / contentId / batchId，' +
        '所以无法按任务、供应商或内容归因（见「归属维度」降级卡）。' +
        '响应是 DataEnvelope（无 page 块）⇒ 不可游标翻页，只能用 since 增量拉取。') +
      '<div class="table-wrap"><table class="tbl tbl-compact">' + head +
      '<tbody>' + body + '</tbody></table></div>' +
      (list.length === 100
        ? note('warn', '已取满 limit=100。', '可能还有更多分录未显示，请用 since 参数继续增量读取。')
        : ''));
  }

  /* ---------------------------- 预估 vs 实际 ---------------------------- */

  function renderEstimates(estimates) {
    var list = rows(estimates);
    var head = '<thead><tr>' +
      '<th style="width:104px">预估 ID</th>' +
      '<th style="width:120px">批次类型</th>' +
      '<th class="num" style="width:62px">数量</th>' +
      '<th style="width:86px">状态</th>' +
      '<th class="num" style="width:98px">预估</th>' +
      '<th class="num" style="width:98px">实际</th>' +
      '<th class="num" style="width:98px">差异</th>' +
      '</tr></thead>';

    var body = list.length
      ? list.map(function (row) {
          var meta = ESTIMATE_STATE_META[text(row.state)];
          var tagCls = 'tag' + (meta && meta.cls ? ' ' + meta.cls : '') + ' no-dot f10';
          var stateLabel = meta ? meta.label : (text(row.state) || '未标注');
          var variance = row.varianceAmount;
          var vNum = numOf(variance);
          var hasVariance = text(variance) !== '';
          return '<tr>' +
            '<td class="f11 mono" title="' + escapeHtml(text(row.id)) + '">' +
              escapeHtml(shortId(row.id) || '—') + '</td>' +
            '<td class="f11">' + escapeHtml(truncate(text(row.batchKind) || '—', 20)) + '</td>' +
            '<td class="num mono f11">' + escapeHtml(text(row.quantity) || '—') + '</td>' +
            '<td><span class="' + tagCls + '">' + escapeHtml(stateLabel) + '</span></td>' +
            '<td class="num mono f11" title="' + escapeHtml(text(row.totalAmount)) + '">' +
              escapeHtml(money(row.totalAmount, row.currency)) + '</td>' +
            '<td class="num mono f11">' +
              (text(row.actualAmount)
                ? escapeHtml(money(row.actualAmount, row.currency))
                : '<span class="t-mut">—</span>') + '</td>' +
            '<td class="num mono f11' + (hasVariance && vNum !== 0 ? (vNum > 0 ? ' t-dan' : ' t-ok') : '') + '">' +
              (hasVariance
                ? escapeHtml(money(variance, row.currency))
                : '<span class="t-mut">未结算</span>') + '</td>' +
            '</tr>';
        }).join('')
      : '<tr><td colspan="7">' + emptyBlock('暂无成本预估单。',
          '演示库里 cost-estimates 可能为空，需先补种子数据') + '</td></tr>';

    return card('预估 vs 实际差异',
      'GET /projects/{id}/cost-estimates · 状态域 DRAFT / RESERVED / SETTLED / CANCELLED',
      '<div class="table-wrap"><table class="tbl tbl-compact">' + head +
      '<tbody>' + body + '</tbody></table></div>' +
      note('info', '差异列是后端算好的 varianceAmount。',
        '未结算的预估单 actualAmount / varianceAmount 为 null ⇒ 显示「未结算」而不是 0，' +
        '0 会被读成「预估与实际完全一致」。'));
  }

  function renderReservations(reservations) {
    var list = rows(reservations);
    var head = '<thead><tr>' +
      '<th style="width:104px">预留 ID</th>' +
      '<th style="width:86px">状态</th>' +
      '<th class="num" style="width:98px">占用额</th>' +
      '<th class="num" style="width:98px">已结算</th>' +
      '<th style="width:104px">关联预估</th>' +
      '<th>ref / note</th>' +
      '</tr></thead>';

    var body = list.length
      ? list.map(function (row) {
          var meta = RESERVATION_STATE_META[text(row.state)];
          var tagCls = 'tag' + (meta && meta.cls ? ' ' + meta.cls : '') + ' no-dot f10';
          var stateLabel = meta ? meta.label : (text(row.state) || '未标注');
          var tail = [text(row.ref), text(row.note)].filter(Boolean).join(' · ');
          return '<tr' + (text(row.state) === 'EXPIRED' ? ' class="tr-danger"' : '') + '>' +
            '<td class="f11 mono" title="' + escapeHtml(text(row.id)) + '">' +
              escapeHtml(shortId(row.id) || '—') + '</td>' +
            '<td><span class="' + tagCls + '">' + escapeHtml(stateLabel) + '</span></td>' +
            '<td class="num mono f11" title="' + escapeHtml(text(row.amount)) + '">' +
              escapeHtml(money(row.amount, row.currency)) + '</td>' +
            '<td class="num mono f11">' +
              (text(row.settledAmount)
                ? escapeHtml(money(row.settledAmount, row.currency))
                : '<span class="t-mut">—</span>') + '</td>' +
            '<td class="f11 mono" title="' + escapeHtml(text(row.estimateId)) + '">' +
              escapeHtml(shortId(row.estimateId) || '—') + '</td>' +
            '<td class="f11">' +
              (tail ? escapeHtml(truncate(tail, 70)) : '<span class="t-mut">—</span>') + '</td>' +
            '</tr>';
        }).join('')
      : '<tr><td colspan="6">' + emptyBlock('暂无预算预留。',
          '「进行中占用」为 0 时这里就是空的') + '</td></tr>';

    return card('预留 / 占用明细',
      'GET /projects/{id}/budget-reservations · 状态域 HELD / SETTLED / RELEASED / EXPIRED',
      '<div class="table-wrap"><table class="tbl tbl-compact">' + head +
      '<tbody>' + body + '</tbody></table></div>');
  }

  function renderPricingVersions(versions) {
    var list = rows(versions);
    var head = '<thead><tr>' +
      '<th>计价版本 label</th>' +
      '<th style="width:104px">供应商 ID</th>' +
      '<th style="width:86px">状态</th>' +
      '<th style="width:62px">币种</th>' +
      '<th style="width:128px">生效自</th>' +
      '<th style="width:140px">计费单位</th>' +
      '</tr></thead>';

    var body = list.length
      ? list.map(function (row) {
          var meta = PRICING_STATE_META[text(row.state)];
          var tagCls = 'tag' + (meta && meta.cls ? ' ' + meta.cls : '') + ' no-dot f10';
          var stateLabel = meta ? meta.label : (text(row.state) || '未标注');
          var lines = Array.isArray(row.lines) ? row.lines : [];
          var units = [];
          lines.forEach(function (line) {
            var u = line && typeof line === 'object' ? text(line.unit) : '';
            if (u && units.indexOf(u) === -1) units.push(u);
          });
          return '<tr>' +
            '<td class="f11" title="' + escapeHtml(text(row.label)) + '">' +
              escapeHtml(truncate(text(row.label) || '—', 40)) + '</td>' +
            '<td class="f11 mono" title="' + escapeHtml(text(row.providerId)) + '">' +
              escapeHtml(shortId(row.providerId) || '—') + '</td>' +
            '<td><span class="' + tagCls + '">' + escapeHtml(stateLabel) + '</span></td>' +
            '<td class="f11 mono">' + escapeHtml(text(row.currency) || '—') + '</td>' +
            '<td class="f11 mono">' + escapeHtml(fmtTime(row.effectiveFrom) || '—') + '</td>' +
            '<td class="f11 mono">' +
              (units.length ? escapeHtml(units.join(' / ')) : '<span class="t-mut">—</span>') + '</td>' +
            '</tr>';
        }).join('')
      : '<tr><td colspan="6">' + emptyBlock('暂无计价版本。',
          'GET /pricing-versions 返回空数组') + '</td></tr>';

    return card('计价版本',
      'GET /pricing-versions · 筛选参数名是蛇形 provider_id（billing/api.py:989）',
      '<div class="table-wrap"><table class="tbl tbl-compact">' + head +
      '<tbody>' + body + '</tbody></table></div>' +
      note('info', '这里只有供应商 ID，没有供应商名称。',
        'providers 表在 modules/gateway/models.py:110，但 modules/gateway 无 api.py、' +
        'main.py 无 gateway_router ⇒ 供应商名称、密钥掩码均无 HTTP 来源。'));
  }

  /* ---------------------------- 降级卡 ---------------------------- */

  /** 降级 D4：归属维度三块合并一张卡。不渲染空表格、不解析 description。 */
  function renderAttributionUnavailable() {
    return card('归属维度（按批次 / 按发起人 / 按供应商）',
      '三块均无读源',
      note('warn', '账本分录里没有任何归属列。',
        'LedgerEntryResponseBody（billing/schema.py:347-359）的 6 个字段是 ' +
        'id / projectId / entryKind / currency / amount / quantity / unit / description —— ' +
        'batchId、actorId（发起人）、providerId（供应商）一个都没有。' +
        '唯一相关的是自由文本 description，但解析它来反推归属属于编造，本页不做。' +
        '完整接线的前置条件是后端给 ledger 行补 jobId / providerId / batchId 关联，' +
        '或提供一个带归属维度的汇总端点。') +
      '<div class="col f11 t-mut" style="gap:4px;margin-top:10px">' +
      '<span>· 按批次 —— 缺 batchId，且契约里 batch 零命中（无 batch 资源）</span>' +
      '<span>· 按发起人 —— 缺 actorId；页头「发起人为归因字段，不意味其承担个人费用」是教义，保留</span>' +
      '<span>· 按供应商 —— 缺 providerId；只能从 /pricing-versions 侧面看到供应商 ID（见下表）</span>' +
      '</div>',
      noSourceBadge());
  }

  /** 降级 D6：AI 任务队列。与 tasks 页同一缺口、同一文案。 */
  function renderQueueUnavailable() {
    return card('AI 任务队列', '无 HTTP 端点',
      note('warn', 'jobs 表存在，但没有读端点。',
        'jobs（modules/jobs/models.py:185，含 kind / queue / state / priority / attempt / ' +
        'error_code / result_ref）与 job_attempts（:283）两张表都在库里，但 modules/jobs ' +
        '目录下没有 api.py，main.py 的 include_router 列表里也没有 jobs_router，' +
        '契约 86 paths 里 /jobs、/tasks、/task-runs 三个前缀全部零命中。' +
        '这里刻意不渲染空表格 —— 空表格会被误读为「当前没有任务」，而真相是「查不到」。') +
      '<div class="col f11 t-mut" style="gap:4px;margin-top:10px">' +
      '<span>· 任务编号 / 类型 / 对象·输入版本 / 供应商·模型 / 状态 / 重试 / 耗时 / 费用 / 错误分类 —— 10 列全部无源</span>' +
      '<span>· 完整接线的前置：后端新增 GET /jobs + GET /jobs/{id}/attempts 并注册 router</span>' +
      '</div>',
      noSourceBadge());
  }

  /* ---------------------------- 对账（按钮触发） ---------------------------- */

  function renderReconciliation(report) {
    var r = report && typeof report === 'object' ? report : null;
    if (!r) {
      return note('info', '尚未对账。',
        '点击页头「处理待核对」会发 POST /billing/reconciliations，' +
        '之后这里显示 pendingCount / drifts / healthy。GET 页加载时不发写请求。');
    }

    var body = [];
    body.push('<div class="row" style="gap:8px;flex-wrap:wrap;margin-bottom:10px">' +
      '<span class="tag ' + (r.healthy === true ? 'tag-ok' : 'tag-danger') + ' no-dot f10">' +
        (r.healthy === true ? '账面健康' : '账面存在分叉') + '</span>' +
      (r.truncated === true
        ? '<span class="tag tag-warn no-dot f10">truncated=true · 未扫全</span>'
        : '') +
      '</div>');
    body.push('<div class="col" style="gap:6px">');
    body.push(kv('扫描条数 scanned', escapeHtml(text(r.scanned) || '0'), true));
    body.push(kv('已结清 resolved', escapeHtml(text(r.resolved) || '0'), true));
    body.push(kv('待核对 pendingCount', escapeHtml(text(r.pendingCount) || '0'), true));
    body.push(kv('检查项目数 projectsChecked', escapeHtml(text(r.projectsChecked) || '0'), true));
    body.push(kv('账面分叉 drifts', escapeHtml(String(rows(r.drifts).length)), true));
    body.push('</div>');

    var drifts = rows(r.drifts);
    if (drifts.length) {
      body.push('<div class="table-wrap" style="margin-top:12px"><table class="tbl tbl-compact">' +
        '<thead><tr><th>表</th><th class="num">账面值</th><th class="num">重算值</th>' +
        '<th class="num">差额</th></tr></thead><tbody>' +
        drifts.map(function (d) {
          return '<tr' + (d.healthy === false ? ' class="tr-danger"' : '') + '>' +
            '<td class="f11 mono">' + escapeHtml(truncate(text(d.table), 28)) + '</td>' +
            '<td class="num mono f11">' + escapeHtml(text(d.accountValue) || '—') + '</td>' +
            '<td class="num mono f11">' + escapeHtml(text(d.recomputedValue) || '—') + '</td>' +
            '<td class="num mono f11">' + escapeHtml(text(d.drift) || '—') + '</td></tr>';
        }).join('') + '</tbody></table></div>');
    }

    var pending = rows(r.pending);
    if (pending.length) {
      body.push('<div class="table-wrap" style="margin-top:12px"><table class="tbl tbl-compact">' +
        '<thead><tr><th>任务 ID</th><th class="num">尝试号</th><th>状态</th>' +
        '<th class="num">预估额</th><th>说明</th></tr></thead><tbody>' +
        pending.map(function (p) {
          return '<tr class="tr-warn">' +
            '<td class="f11 mono" title="' + escapeHtml(text(p.jobId)) + '">' +
              escapeHtml(shortId(p.jobId) || '—') + '</td>' +
            '<td class="num mono f11">' + escapeHtml(text(p.attemptNo) || '—') + '</td>' +
            '<td class="f11">' + escapeHtml(truncate(text(p.state) || '—', 18)) + '</td>' +
            '<td class="num mono f11">' +
              (text(p.estimatedAmount) ? escapeHtml(text(p.estimatedAmount)) : '<span class="t-mut">—</span>') +
            '</td>' +
            '<td class="f11">' + escapeHtml(truncate(text(p.note) || '—', 60)) + '</td></tr>';
        }).join('') + '</tbody></table></div>');
    }

    if (r.truncated === true) {
      body.push(note('warn', '对账未扫全。',
        'truncated=true ⇒ scanned 不是全量，pendingCount 是下界而不是确数。'));
    }

    return body.join('');
  }

  /** 「失败重试费用计入」等教义文案 —— 保留静态并标注。 */
  function renderCostDoctrine() {
    return card('费用口径纪律', '',
      '<div class="col f12 t-mut" style="gap:8px">' +
      '<span>· 失败重试的费用计入账本，不能只统计成功结果 —— LEDGER_KIND_USAGE 的存在印证它：用量按调用记，不按成功记。</span>' +
      '<span>· 未知结果不能直接记作无费用，须标记待核对（对应 POST /billing/reconciliations 的 pending）。</span>' +
      '<span>· 发起人是归因字段，不意味其承担个人费用。</span>' +
      '<span>· 冲正是负数分录，净额就该变小 —— append-only 账本不改历史行。</span>' +
      '</div>', doctrineBadge());
  }

  /* ---------------------------- 加载 / 空 / 错误 ---------------------------- */

  function renderCardLoading(label) {
    return '<section class="card"><div class="card-head"><span class="card-title">读取中</span></div>' +
      '<div class="card-body"><div class="row" style="gap:8px">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">' + escapeHtml(text(label) || '正在读取费用数据…') + '</span>' +
      '</div></div></section>';
  }

  function renderEmpty(message) {
    return card('暂无数据', '', emptyBlock(text(message) || '当前项目没有费用数据。'));
  }

  /**
   * @param {object|string} err ApiError 实例或 {code,message,retryable,offline,details}
   *   只读普通字段，不依赖 ApiError 类（保持本模块零依赖）。
   */
  function renderCardError(err) {
    var e = err && typeof err === 'object' ? err : {};
    var code = text(e.code);
    var message = text(e.message) || text(err) || '读取费用数据失败。';
    var offline = !!e.offline || code === 'DEPENDENCY_UNAVAILABLE';
    var hint = offline
      ? '连不上后端（页面结构已保留）。确认后端已启动后可重试。'
      : (e.retryable ? '该错误可重试，请稍后再试。' : '请按错误码排查后重试。');

    return '<section class="card"><div class="card-head">' +
      '<span class="card-title">读取失败</span></div>' +
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
    /* 纯计算 */
    money: money,
    usageRatio: usageRatio,
    sumPositive: sumPositive,
    sumReversal: sumReversal,
    reconcileRequest: reconcileRequest,
    /* 渲染 */
    renderBudgetCard: renderBudgetCard,
    renderBudgetModeNotice: renderBudgetModeNotice,
    renderSummaryCards: renderSummaryCards,
    renderTruncationWarning: renderTruncationWarning,
    renderKindDistribution: renderKindDistribution,
    renderUnitDistribution: renderUnitDistribution,
    renderLedgerTable: renderLedgerTable,
    renderEstimates: renderEstimates,
    renderReservations: renderReservations,
    renderPricingVersions: renderPricingVersions,
    renderAttributionUnavailable: renderAttributionUnavailable,
    renderQueueUnavailable: renderQueueUnavailable,
    renderReconciliation: renderReconciliation,
    renderCostDoctrine: renderCostDoctrine,
    renderCardLoading: renderCardLoading,
    renderEmpty: renderEmpty,
    renderCardError: renderCardError,
    escapeHtml: escapeHtml,
    LEDGER_KINDS: LEDGER_KINDS,
    PRICING_UNITS: PRICING_UNITS
  };
});
