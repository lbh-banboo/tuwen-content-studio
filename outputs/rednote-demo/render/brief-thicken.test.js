/* render/brief-thicken.test.js —— Brief 加厚页（方案 A 纯函数）Node 单测
 * 运行：node render/brief-thicken.test.js   （零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./brief-thicken.js');

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

function makeBrief(overrides) {
  const draft = Object.assign({
    draftId: '0565ad68-0fd4-48b8-ba70-681d04f89d05',
    lockVersion: 1,
    structuredFields: {},
    claims: [],
    issueResolutions: [],
    suggestions: [],
    suggestionsVersionId: null,
    sourceState: 'NONE',
    updatedAt: '2026-10-17T10:37:54.284853+08:00'
  }, overrides && overrides.draft);
  return Object.assign({
    briefId: '707409d7-f387-42ef-ad72-dfa495ffa24e',
    projectId: PROJECT.id,
    activeVersionId: null,
    draft: draft,
    version: null,
    versions: [],
    blockingIssueCount: 0
  }, overrides && overrides.brief);
}

const SUGGESTIONS = [
  {
    fieldPath: 'product.mechanism',
    text: '氨基酸表活以椰油酰甘氨酸钾为主，pH 5.5±0.5',
    category: 'SOURCE_FACT',
    rationale: '原文 P.5 第 2 节',
    evidence: { segmentId: 'aaaaaaaa-1111-4111-8111-111111111111' }
  },
  {
    fieldPath: 'product.selling_points',
    text: '品牌官方页表述表活占比 ≥60%，与 Brief 62% 不一致',
    category: 'EXTERNAL_EVIDENCE',
    rationale: '需运营确认口径',
    evidence: { url: 'https://brand.example.com/product' }
  },
  {
    fieldPath: 'audience.usage_scenarios',
    text: '建议增加「换季泛红期」人群角度',
    category: 'STRATEGY',
    rationale: 'AI 建议，不当作产品事实'
  },
  {
    fieldPath: 'product.image_usage_rights',
    text: '图片授权期限未提供',
    category: 'UNCONFIRMED',
    rationale: '需运营补充'
  }
];

const ISSUES = [
  {
    id: 'i-1', fieldPath: 'product.image_usage_rights', severity: 'BLOCK',
    description: '图片使用权限未说明', state: 'OPEN'
  },
  {
    id: 'i-2', fieldPath: 'product.mechanism', severity: 'WARN',
    description: '配方备案编号附件缺失', state: 'RESOLVED', resolution: '标记为限制而非事实'
  }
];

/* ---------------------------- 汇总 ---------------------------- */

test('summarize: 四类建议分别计数 + 阻断疑问', () => {
  const brief = makeBrief({ draft: { suggestions: SUGGESTIONS, issueResolutions: ISSUES } });
  const s = R.summarize(brief);
  assert.equal(s.total, 4);
  assert.equal(s.sourceFact, 1);
  assert.equal(s.externalEvidence, 1);
  assert.equal(s.strategy, 1);
  assert.equal(s.unconfirmed, 1);
  assert.equal(s.issues, 2);
  assert.equal(s.blockingOpen, 1);
});

test('summarize: 未知 category 计入 unknown，不丢条', () => {
  const brief = makeBrief({ draft: { suggestions: [{ category: 'DERIVED', text: 'x' }] } });
  const s = R.summarize(brief);
  assert.equal(s.total, 1);
  assert.equal(s.unknown, 1);
});

test('summarize: null / 非对象不抛错', () => {
  assert.doesNotThrow(() => R.summarize(null));
  assert.equal(R.summarize(null).total, 0);
});

/* ---------------------------- 建议 ---------------------------- */

test('renderSuggestionsCard: 四类标签 / 内容 / 字段 / 证据', () => {
  const html = R.renderSuggestionsCard(makeBrief({ draft: { suggestions: SUGGESTIONS } }));
  assert.equal(occurrences(html, '<tr>'), 5); // 表头 + 4 行
  assert.ok(html.includes('原文事实'));
  assert.ok(html.includes('外部证据'));
  assert.ok(html.includes('策略建议'));
  assert.ok(html.includes('待确认'));
  assert.ok(html.includes('椰油酰甘氨酸钾'));
  assert.ok(html.includes('product.mechanism'));
  assert.ok(html.includes('aaaaaaaa…1111')); // segmentId 短 ID
  assert.ok(html.includes('https://brand.example.com/product'));
});

test('renderSuggestionsCard: 无建议 → 空态', () => {
  const html = R.renderSuggestionsCard(makeBrief());
  assert.ok(html.includes('尚无加厚建议'));
});

/* ---------------------------- 前置条件 ---------------------------- */

test('renderPrereqCard: activeVersionId=null → 明确阻止发起', () => {
  const html = R.renderPrereqCard(makeBrief());
  assert.ok(html.includes('未满足'));
  assert.ok(html.includes('activeVersionId 为空'));
  assert.ok(html.includes('brief-enrichment-jobs'));
  assert.ok(html.includes('allowedSourceIds'));
});

test('renderPrereqCard: 有版本 → 可发起说明', () => {
  const brief = makeBrief({ brief: { activeVersionId: 'aaaaaaaa-1111-4111-8111-111111111111' } });
  const html = R.renderPrereqCard(brief);
  assert.ok(html.includes('已具备版本'));
  assert.ok(html.includes('可发起加厚'));
  assert.ok(html.includes('briefVersionId'));
});

/* ---------------------------- 疑问 / 规则 / 页头 ---------------------------- */

test('renderIssuesCard: 级别 / 状态 / 处置理由', () => {
  const html = R.renderIssuesCard(makeBrief({ draft: { issueResolutions: ISSUES } }));
  assert.equal(occurrences(html, 'class="list-item"'), 2);
  assert.ok(html.includes('阻断') && html.includes('待处置'));
  assert.ok(html.includes('已解决'));
  assert.ok(html.includes('标记为限制而非事实'));
});

test('renderIssuesCard: 空数组 → 空态', () => {
  assert.ok(R.renderIssuesCard(makeBrief()).includes('无解构疑问'));
});

test('renderRulesCard: 静态规则明确标注', () => {
  const html = R.renderRulesCard();
  assert.ok(html.includes('规则说明（静态）'));
  assert.ok(html.includes('建议不自动生效'));
  assert.ok(html.includes('版本不可原地修改'));
});

test('renderHead / renderMetrics: 项目与建议计数', () => {
  const brief = makeBrief({ draft: { suggestions: SUGGESTIONS } });
  const head = R.renderHead(PROJECT, brief);
  assert.ok(head.includes('素与 Su&amp; 氨基酸洁面'));
  assert.ok(head.includes('建议 4 条'));
  assert.ok(head.includes('步骤 1 / 8'));
  const metrics = R.renderMetrics(brief);
  assert.equal(occurrences(metrics, 'class="card metric"'), 4);
  assert.ok(metrics.includes('策略建议'));
});

/* ---------------------------- 转义 / 错误 / 纯度 ---------------------------- */

test('建议内容 / 理由里的 HTML 被转义', () => {
  const brief = makeBrief({
    draft: { suggestions: [{ category: 'STRATEGY', text: '<script>x</script>', rationale: '<img src=x>' }] }
  });
  const html = R.renderSuggestionsCard(brief);
  assert.equal(html.includes('<script>'), false);
  assert.equal(html.includes('<img'), false);
  assert.ok(html.includes('&lt;script&gt;'));
});

test('null 输入不抛错', () => {
  assert.doesNotThrow(() => R.renderHead(null, null));
  assert.doesNotThrow(() => R.renderMetrics(null));
  assert.doesNotThrow(() => R.renderSuggestionsCard(null));
  assert.doesNotThrow(() => R.renderPrereqCard(null));
  assert.doesNotThrow(() => R.renderIssuesCard(null));
});

test('renderCardLoading / renderCardError 分支', () => {
  assert.ok(R.renderCardLoading('读加厚').includes('读加厚'));
  assert.ok(R.renderCardError({ code: 'DEPENDENCY_UNAVAILABLE', offline: true }).includes('127.0.0.1:8787'));
  assert.ok(R.renderCardError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderCardError({ message: '<b>x</b>' }).includes('&lt;b&gt;x&lt;/b&gt;'));
});

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'brief-thicken.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) process.exitCode = 1;
