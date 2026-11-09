const fs = require('fs');

const groups = [
  {
    n: 'GROUP 01', t: '入口与全局导航', d: 'PRD §3.1 主导航 · §2.4 登录与成员生命周期',
    items: [
      { f:'workbench.html', i:'fa-gauge-high', name:'工作台', step:'导航 1', desc:'我的项目、待审核、失败任务、临近截止、数据待补五类待办聚合，一键进入处理。', tags:['待办聚合','失败任务','临近截止'] },
      { f:'projects.html', i:'fa-folder-open', name:'项目列表', step:'导航 2', desc:'品牌、产品、负责人、阶段、交付进度、截止日期。支持搜索、筛选、排序、分页。', tags:['分页','筛选','逾期标签'] },
      { f:'tasks.html', i:'fa-bell', name:'任务与通知', step:'导航 4', desc:'AI 任务队列状态机与站内待办。同批次合并通知，区分排队、运行、部分成功与失败。', tags:['状态机','同批合并','有限重试'] }
    ]
  },
  {
    n: 'GROUP 02', t: '项目总览与八步关口', d: 'PRD §4 项目定义 · §4.3 八步不可跳过',
    items: [
      { f:'project-overview.html', i:'fa-chart-pie', name:'项目总览', step:'核心', desc:'当前阶段、八步记录、六项数量指标口径、待办、费用与报告入口。阶段进展与数量进度并列展示，不用八步完成比例替代交付完成率。', tags:['八步条','指标口径','单篇通过成本'] }
    ]
  },
  {
    n: 'GROUP 03', t: '步骤 1—3：Brief 到生产基线', d: 'PRD §5 §6 §7 · 信息进入生产前的三道关口',
    items: [
      { f:'brief.html', i:'fa-file-lines', name:'Brief 解析与加厚', step:'步骤 1', desc:'原文与结构化拆解对照，10 类必覆盖字段。缺失显示「待补充」，AI 不得填成确定事实；加厚输出四分类可追溯。', tags:['原文对照','待补充','四分类'] },
      { f:'research.html', i:'fa-magnifying-glass', name:'项目研究与资料上传', step:'步骤 2', desc:'推荐关键词与可复制提示词，300,000,000 字节容量条，八种解析状态与证据完整度展示。', tags:['提示词复制','300MB 容量','OCR'] },
      { f:'info-review.html', i:'fa-clipboard-check', name:'信息审核与生产基线', step:'步骤 3', desc:'事实、人群场景、表达限制、证据冲突逐项处置，确认后创建不可原地修改的生产基线快照。', tags:['冲突处置','基线快照','版本绑定'] }
    ]
  },
  {
    n: 'GROUP 04', t: '步骤 4—7：试产到交付', d: 'PRD §8 §9 §10 §11 · 10 条试产 → 30 扩产 → 50 批产',
    items: [
      { f:'trial-config.html', i:'fa-sliders', name:'素材试产配置', step:'步骤 4', desc:'场景与形式比例合计必须 100%，最大余数法换算 10 条清单，生成前展示预估费用与基线方案版本。', tags:['最大余数法','10 条清单','预估费用'] },
      { f:'content-feedback.html', i:'fa-comments', name:'内容反馈与审核', step:'步骤 5', desc:'手机比例预览、五种反馈方式、AI 自检分类、局部重做与作用范围选择。关键事实问题未解决不能通过。', tags:['手机预览','局部重做','作用范围'] },
      { f:'scale-batch.html', i:'fa-layer-group', name:'扩产 / 批量产出', step:'步骤 6—7', desc:'扩产 1—30、批产 1—50 整数校验，批次差异化检查与批次报告，方向覆盖门槛拦截未验证方向。', tags:['数量校验','差异化检查','批次队列'] },
      { f:'delivery.html', i:'fa-box-open', name:'交付记录与下载包', step:'步骤 7', desc:'KOC 轻量登记、ZIP 结构预览、下载不自动等于交付需显式标记，同内容多 KOC 重复表达风险提示。', tags:['ZIP 结构','显式交付','版本锁定'] }
    ]
  },
  {
    n: 'GROUP 05', t: '步骤 8：发布登记与效果闭环', d: 'PRD §12 · 指标字典 · 快照与业务标签',
    items: [
      { f:'data-feedback.html', i:'fa-chart-line', name:'发布登记与数据反馈', step:'步骤 8', desc:'发布记录、六类指标字典、字段状态区分（缺失不填 0）、快照时间差展示、业务标签阈值计算与实验登记。', tags:['指标字典','快照时间差','标签阈值'] },
      { f:'report.html', i:'fa-file-contract', name:'项目报告与结项', step:'输出', desc:'试产总结、阶段报告、结项报告三类。保存统计截止时间、指标口径与引用快照，支持 Markdown / PDF 导出。', tags:['三类报告','截止快照','结项重开'] }
    ]
  },
  {
    n: 'GROUP 06', t: '知识资产与管理中心', d: 'PRD §15 双闭环 · §17 费用 · §2 权限',
    items: [
      { f:'knowledge.html', i:'fa-book-bookmark', name:'知识库', step:'导航 3', desc:'六类资产 CRUD、原始资料层、状态生命周期、动态上下文组装与生成追溯，全员共享检索但不跨品牌迁移事实。', tags:['六类资产','全员共享','生成追溯'] },
      { f:'cost.html', i:'fa-coins', name:'费用管理', step:'管理', desc:'按项目、成员、供应商分组，区分预估、实际、待核对、退款抵扣，预算提醒与硬限制模式。', tags:['四态费用','原始用量','预算硬限'] },
      { f:'members.html', i:'fa-users-gear', name:'成员与供应商设置', step:'管理', desc:'成员邀请停用与负责人转交流程，供应商密钥配置（界面不回显完整密钥）与权限矩阵对照。', tags:['负责人转交','密钥不回显','权限矩阵'] }
    ]
  }
];

const total = groups.reduce((s, g) => s + g.items.length, 0);

let cards = '';
groups.forEach(function (g) {
  let inner = '';
  g.items.forEach(function (it, i) {
    const tagHtml = it.tags.map(function (t) { return '<span>' + t + '</span>'; }).join('');
    const delay = (0.04 * i + 0.02).toFixed(2);
    inner += '\n        <a class="pcard anim" href="' + it.f + '" style="--d:' + delay + 's">'
      + '\n          <div class="pcard-top">'
      + '\n            <span class="pcard-ic"><i class="fa-solid ' + it.i + '"></i></span>'
      + '\n            <span class="pcard-step">' + it.step + '</span>'
      + '\n          </div>'
      + '\n          <div class="pcard-name">' + it.name + '</div>'
      + '\n          <div class="pcard-file">' + it.f + '</div>'
      + '\n          <p class="pcard-desc">' + it.desc + '</p>'
      + '\n          <div class="pcard-tags">' + tagHtml + '</div>'
      + '\n          <div class="pcard-go">打开页面 <i class="fa-solid fa-arrow-right"></i></div>'
      + '\n        </a>';
  });
  cards += '\n      <section class="lp-group">'
    + '\n        <div class="lp-group-head">'
    + '\n          <span class="lp-group-n">' + g.n + '</span>'
    + '\n          <span class="lp-group-t">' + g.t + '</span>'
    + '\n          <span class="lp-group-d">' + g.d + '</span>'
    + '\n        </div>'
    + '\n        <div class="lp-cards">' + inner
    + '\n        </div>'
    + '\n      </section>\n';
});

const html = '<!doctype html>\n'
+ '<html lang="zh-CN">\n'
+ '<head>\n'
+ '<meta charset="utf-8" />\n'
+ '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
+ '<meta name="theme-color" content="#05040a" />\n'
+ '<title>登录 · Rednote 内容生产 AI Native 系统 · 页面导览</title>\n'
+ '<link rel="preconnect" href="https://fonts.googleapis.com" />\n'
+ '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />\n'
+ '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />\n'
+ '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.2/css/all.min.css" crossorigin="anonymous" referrerpolicy="no-referrer" />\n'
+ '<link rel="stylesheet" href="theme.css" />\n'
+ '<link rel="stylesheet" href="components.css" />\n'
+ '<link rel="stylesheet" href="index.css" />\n'
+ '</head>\n'
+ '<body>\n'
+ '<div class="bg-media" aria-hidden="true">\n'
+ '  <video class="bg-video" autoplay muted loop playsinline preload="auto" poster="assets/bg-poster.jpg">\n'
+ '    <source src="assets/bg.mp4" type="video/mp4" />\n'
+ '  </video>\n'
+ '</div>\n'
+ '<div class="grid-bg"></div>\n\n'
+ '<div class="lp">\n\n'
+ '  <header class="lp-header">\n'
+ '    <span class="lp-logo">R</span>\n'
+ '    <nav class="lp-nav">\n'
+ '      <a class="on" href="index.html">导览</a>\n'
+ '      <a href="workbench.html">工作台</a>\n'
+ '      <a href="projects.html">项目</a>\n'
+ '      <a href="knowledge.html">知识库</a>\n'
+ '      <a href="cost.html">费用</a>\n'
+ '    </nav>\n'
+ '    <a class="lp-signin" href="workbench.html">进入工作台 <i class="fa-solid fa-arrow-right" style="margin-left:7px;font-size:10px"></i></a>\n'
+ '  </header>\n\n'
+ '  <section class="lp-hero">\n'
+ '    <span class="lp-badge anim"><span class="bd"></span>需求基线 V1.0 · 2026-09-16 · 内部运营第一版</span>\n'
+ '    <h1 class="lp-h1 anim" style="--d:.08s">\n'
+ '      从 Brief 到发布数据<br><span class="g">全链路内容生产系统</span>\n'
+ '    </h1>\n'
+ '    <p class="lp-sub anim" style="--d:.18s">\n'
+ '      以项目为组织单位，AI 协助完成 Brief 解析、研究整理、内容方案、文案与图片生产，经运营自审与反馈交付真实 KOC，\n'
+ '      回收发布效果并沉淀可追溯的知识资产。八步首次必经，静态图与 Live 图同时首发。\n'
+ '    </p>\n\n'
+ '    <!-- 登录卡：占据原「进入工作台」按钮位置 -->\n'
+ '    <div class="lp-login anim" style="--d:.28s">\n'
+ '      <div class="lp-login-head">\n'
+ '        <div class="lp-login-t">成员登录</div>\n'
+ '        <div class="lp-login-d">管理员邀请制 · 不开放外部自由注册</div>\n'
+ '      </div>\n\n'
+ '      <a class="sso" href="workbench.html"><i class="fa-solid fa-building-shield"></i>使用企业身份登录</a>\n'
+ '      <div class="or">或使用账号登录</div>\n\n'
+ '      <div class="lp-login-row">\n'
+ '        <div class="field">\n'
+ '          <div class="label">企业邮箱 <span class="req">*</span></div>\n'
+ '          <input class="input" type="email" value="member06@example.com" />\n'
+ '        </div>\n'
+ '        <div class="field">\n'
+ '          <div class="label">密码 <span class="req">*</span></div>\n'
+ '          <input class="input" type="password" value="demo-password" />\n'
+ '        </div>\n'
+ '      </div>\n\n'
+ '      <div class="row between mb14">\n'
+ '        <label class="row f12 t-2" style="gap:7px;cursor:pointer">\n'
+ '          <span class="check is-on"></span>保持登录\n'
+ '        </label>\n'
+ '        <a class="f12 t-pur" href="#" data-toast="演示环境：请联系管理员重置密码">忘记密码</a>\n'
+ '      </div>\n\n'
+ '      <div class="lp-login-cta">\n'
+ '        <a class="lp-cta" href="workbench.html"><i class="fa-solid fa-arrow-right-to-bracket"></i>登录并进入工作台</a>\n'
+ '        <a class="lp-cta2" href="project-overview.html"><i class="fa-solid fa-diagram-project"></i>查看八步流程</a>\n'
+ '      </div>\n\n'
+ '      <div class="lp-login-note">\n'
+ '        <i class="fa-solid fa-circle-info"></i>\n'
+ '        <span><b>无法登录？</b>新成员需由管理员邀请开通。已停用成员不能继续操作，历史记录保留。</span>\n'
+ '      </div>\n'
+ '    </div>\n\n'
+ '    <div class="lp-flow anim" style="--d:.36s">\n'
+ '      <span><i class="fa-solid fa-1"></i>Brief 解析</span>\n'
+ '      <span><i class="fa-solid fa-2"></i>项目研究</span>\n'
+ '      <span><i class="fa-solid fa-3"></i>信息审核</span>\n'
+ '      <span><i class="fa-solid fa-4"></i>试产 10 条</span>\n'
+ '      <span><i class="fa-solid fa-5"></i>试产反馈</span>\n'
+ '      <span><i class="fa-solid fa-6"></i>扩产 ≤30</span>\n'
+ '      <span><i class="fa-solid fa-7"></i>批产 ≤50</span>\n'
+ '      <span><i class="fa-solid fa-8"></i>数据回收</span>\n'
+ '    </div>\n'
+ '  </section>\n\n'
+ '</div>\n\n'
+ '<script src="app.js"><\/script>\n'
+ '<script>bindDemo();<\/script>\n'
+ '</body>\n'
+ '</html>\n';

fs.writeFileSync('index.html', html);
console.log('index.html written, total pages = ' + total);
