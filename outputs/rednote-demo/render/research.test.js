/* render/research.test.js —— 项目研究页（方案 A 纯函数）Node 单测
 * 运行：node render/research.test.js   （零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./research.js');

let passed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log('[PASS] ' + name);
  } catch (err) {
    failures.push({ name, err });
    console.log('[FAIL] ' + name + '\n       ' + err.message);
  }
}

function occurrences(haystack, needle) {
  return haystack.split(needle).length - 1;
}

const PROJECT = {
  id: '5f2d4fec-1d6d-5fa5-9260-e65cf86e0fc8',
  projectCode: 'PRJ-202609-7K3M2Q',
  name: '素与 Su& 氨基酸洁面'
};

/* 与真后端 GET /research-prompts 一致（无 Brief 版本） */
const PROMPTS = {
  projectId: PROJECT.id,
  productName: '',
  keywords: [],
  prompts: [{
    taskKind: 'EVIDENCE_COLLECTION',
    title: '先完成 Brief 解构',
    text: '这个项目还没有已固化的 Brief 版本。\n请先上传 Brief 原文并完成解构（步骤 1），再回到本页面。',
    formatHint: {
      media: 'ANY',
      fields: ['briefFileId', 'briefVersionId'],
      description: '上传 Brief 原文并完成解构后，本页面会自动给出关键词与缺口清单。'
    },
    relatedFieldPaths: [],
    keywords: []
  }],
  gaps: [],
  summary: { total: 0, coveredCount: 0, missingCount: 0, notApplicableCount: 0, blockingCount: 0 },
  derivation: {
    rulesVersion: 1,
    payloadVersion: 1,
    briefVersionId: null,
    revisionNo: null,
    claimCount: 0,
    truncated: false,
    notes: ['该项目还没有 Brief 版本（可能未上传原文，或解构尚未完成）。']
  }
};

const META = {
  research: {
    limitBytes: '300000000',
    effectiveBytes: '150262169',
    reservedBytes: '0',
    remainingBytes: '149737831',
    listedBytes: '150262169',
    fileCount: 8,
    usableEvidenceCount: 6,
    reconciled: true
  }
};

const FILES = [
  {
    fileId: '6839f7ff-7649-57fb-a31c-e16df668d03a',
    originalName: '产品问答_客服纪要.txt', mimeType: 'text/plain', byteSize: '104858',
    createdAt: '2026-09-21T17:00:00+08:00', evidenceUsable: false,
    parse: { state: 'NONE', totalUnits: null, parsedUnits: null, errors: [] }
  },
  {
    fileId: '4ed615c7-7f58-5039-a654-df1ee3c59f67',
    originalName: '场景研究_长文档合集.pdf', mimeType: 'application/pdf', byteSize: '67318579',
    createdAt: '2026-09-18T17:00:00+08:00', evidenceUsable: false,
    parse: { state: 'RUNNING', totalUnits: '240', parsedUnits: '61', errors: [] }
  },
  {
    fileId: '6d7833ea-5198-5e68-99cb-e98618289bd3',
    originalName: '竞品话术拆解_12条.pdf', mimeType: 'application/pdf', byteSize: '29780787',
    createdAt: '2026-09-16T17:00:00+08:00', evidenceUsable: true,
    parse: { state: 'PARTIAL', totalUnits: '96', parsedUnits: '78', errors: [{ unit: 5, message: '解析器提前中止' }] }
  }
];

const GAPS = [
  {
    claimId: 'c-1', fieldPath: 'product.mechanism', fieldLabel: '原理',
    claimText: '氨基酸表活占比 62%', category: 'SOURCE_FACT', resolution: 'PENDING',
    state: 'MISSING', blocking: true, suggestedKeywords: ['氨基酸表活 占比'],
    note: '缺少品牌官方来源'
  },
  {
    claimId: 'c-2', fieldPath: 'product.images', fieldLabel: '产品图片',
    claimText: '产品图 8 张', category: 'SOURCE_FACT', resolution: 'CONFIRMED',
    state: 'COVERED', blocking: false, suggestedKeywords: [], note: null
  }
];

/* ---------------------------- 容量卡 ---------------------------- */

test('renderQuotaCards: 容量 / 文件数 / 剩余 / 证据可用来自 meta.research', () => {
  const html = R.renderQuotaCards(META);
  assert.equal(occurrences(html, 'class="card metric"'), 4);
  assert.ok(html.includes('150.3 MB'));
  assert.ok(html.includes('300.0 MB'));
  assert.ok(html.includes('50.1%'));
  assert.ok(html.includes('可用证据 6 份'));
  assert.ok(html.includes('149.7 MB'));
  assert.ok(html.includes('配额已对账'));
});

test('renderQuotaCards: meta.research 缺失 → 全部 —，不编数', () => {
  const html = R.renderQuotaCards(null);
  assert.equal(occurrences(html, 'na'), 4);
  assert.ok(html.includes('meta.research 未提供'));
});

/* ---------------------------- 关键词 / 提示词 ---------------------------- */

test('renderKeywordsCard: 空关键词提示需先有 Brief 版本', () => {
  const html = R.renderKeywordsCard(PROMPTS);
  assert.ok(html.includes('尚未生成关键词'));
  assert.ok(html.includes('keywords 为空'));
});

test('renderKeywordsCard: 关键词展示 text + 来源 title', () => {
  const data = Object.assign({}, PROMPTS, {
    keywords: [{ text: '氨基酸洁面', sourceKind: 'FIELD', sourceFieldLabel: '产品名称', sourceFieldPath: 'product.name' }]
  });
  const html = R.renderKeywordsCard(data);
  assert.ok(html.includes('氨基酸洁面'));
  assert.ok(html.includes('title="产品名称"'));
});

test('renderPromptsCard: 标题 / 正文 / 建议返回格式 / 字段', () => {
  const html = R.renderPromptsCard(PROMPTS);
  assert.ok(html.includes('先完成 Brief 解构'));
  assert.ok(html.includes('这个项目还没有已固化的 Brief 版本'));
  assert.ok(html.includes('证据收集'));
  assert.ok(html.includes('不限'));
  assert.ok(html.includes('briefFileId'));
  assert.ok(html.includes('briefVersionId'));
  assert.ok(html.includes('POST /projects/{id}/uploads'));
});

test('renderPromptsCard: 空 prompts → 空态', () => {
  const html = R.renderPromptsCard({ prompts: [] });
  assert.ok(html.includes('暂无推荐提示词'));
});

/* ---------------------------- 缺口 ---------------------------- */

test('renderGapsCard: 阻断缺口行 / 状态 / 建议关键词 / 说明', () => {
  const html = R.renderGapsCard({ gaps: GAPS });
  assert.equal(occurrences(html, '<tr'), 3); // 表头 + 2 行
  assert.ok(html.includes('氨基酸表活占比 62%'));
  assert.ok(html.includes('缺失'));
  assert.ok(html.includes('阻断'));
  assert.ok(html.includes('氨基酸表活 占比'));
  assert.ok(html.includes('缺少品牌官方来源'));
  assert.ok(html.includes('已覆盖'));
});

test('renderGapsCard: 空 gaps → 空态', () => {
  assert.ok(R.renderGapsCard(PROMPTS).includes('暂无覆盖缺口'));
});

/* ---------------------------- 派生 / 文件 / 页头 ---------------------------- */

test('renderSummaryCard: summary 与 derivation 原样呈现', () => {
  const html = R.renderSummaryCard(PROMPTS);
  assert.ok(html.includes('关键词 / 提示词'));
  assert.ok(html.includes('0 / 1'));
  assert.ok(html.includes('Brief 版本'));
  assert.ok(html.includes('null'));
  assert.ok(html.includes('规则版本'));
  assert.ok(html.includes('是否截断'));
  assert.ok(html.includes('该项目还没有 Brief 版本'));
});

test('renderFilesCard: 文件解析状态与单元进度', () => {
  const html = R.renderFilesCard(FILES, PROJECT.id);
  assert.ok(html.includes('产品问答_客服纪要.txt'));
  assert.ok(html.includes('解析中'));
  assert.ok(html.includes('61 / 240'));
  assert.ok(html.includes('部分成功'));
  assert.ok(html.includes('research-files.v2.html?id=' + PROJECT.id));
});

test('renderFilesCard: 空列表 → 空态', () => {
  assert.ok(R.renderFilesCard([], PROJECT.id).includes('暂无研究资料'));
});

test('renderHead: 项目 / 编号 / 阻断缺口计数', () => {
  const html = R.renderHead(PROJECT, PROMPTS);
  assert.ok(html.includes('素与 Su&amp; 氨基酸洁面'));
  assert.ok(html.includes('PRJ-202609-7K3M2Q'));
  assert.ok(html.includes('步骤 2 / 8'));
  assert.ok(html.includes('阻断缺口 0'));
});

/* ---------------------------- 转义 / 错误 / 纯度 ---------------------------- */

test('提示词 / 文件名里的 HTML 被转义', () => {
  const prompts = {
    prompts: [{
      taskKind: 'FACT_VERIFICATION', title: '<script>x</script>', text: '<img src=x>',
      formatHint: { media: 'URL', fields: [] }, relatedFieldPaths: [], keywords: []
    }]
  };
  const html = R.renderPromptsCard(prompts);
  assert.equal(html.includes('<script>'), false);
  assert.equal(html.includes('<img'), false);
  assert.ok(html.includes('&lt;script&gt;'));

  const files = R.renderFilesCard([{
    originalName: '<b>bad</b>.pdf', mimeType: 'application/pdf', byteSize: '1',
    parse: { state: 'NONE', errors: [] }, evidenceUsable: false
  }], 'p');
  assert.equal(files.includes('<b>bad</b>'), false);
});

test('null 输入不抛错', () => {
  assert.doesNotThrow(() => R.renderHead(null, null));
  assert.doesNotThrow(() => R.renderKeywordsCard(null));
  assert.doesNotThrow(() => R.renderPromptsCard(null));
  assert.doesNotThrow(() => R.renderGapsCard(null));
  assert.doesNotThrow(() => R.renderSummaryCard(null));
  assert.doesNotThrow(() => R.renderFilesCard(null, null));
});

test('renderCardLoading / renderCardError 分支', () => {
  assert.ok(R.renderCardLoading('读研究').includes('读研究'));
  assert.ok(R.renderCardError({ code: 'DEPENDENCY_UNAVAILABLE', offline: true }).includes('127.0.0.1:8787'));
  assert.ok(R.renderCardError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderCardError({ message: '<b>x</b>' }).includes('&lt;b&gt;x&lt;/b&gt;'));
});

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'research.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) process.exitCode = 1;
