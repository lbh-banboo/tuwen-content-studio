/* render/claims.test.js —— 可用主张页（方案 A 纯函数）Node 单测
 * 运行：node render/claims.test.js   （零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./claims.js');

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

const EMPTY_DRAFT = {
  draftId: '59ce5df0-af07-4488-95c0-74c4306c1421',
  projectId: PROJECT.id,
  briefVersionId: null,
  claims: [],
  conflicts: [],
  scenes: [],
  boundaries: {},
  researchSelection: [],
  operatorConfirmed: false,
  lockVersion: 1,
  updatedAt: '2026-10-17T10:55:04.892085+08:00'
};

const FULL_DRAFT = Object.assign({}, EMPTY_DRAFT, {
  claims: [
    { claimId: 'c-1', statement: '氨基酸表活占比 ≥60%', source: { label: 'R-014 品牌官方页' }, state: 'CONFIRMED' },
    { claimId: 'c-2', statement: 'pH 5.5±0.5 弱酸性', source: 'Brief v2 P.5', state: 'CONFIRMED' },
    { claimId: 'c-3', statement: '洗脸后不紧绷', source: null, state: 'PENDING' },
    { claimId: 'c-4', statement: '敏感肌可长期使用', source: { url: 'https://x.example.com/a' }, state: 'CONFLICT' }
  ],
  conflicts: [
    { conflictId: 'k-1', kind: 'SOURCE', state: 'OPEN', blocking: true },
    { conflictId: 'k-2', kind: 'FACT_VS_LIMIT', state: 'SETTLED', action: 'RANGE_STATEMENT' }
  ],
  boundaries: { forbidden: ['医美级', '抗炎'] },
  researchSelection: [{ fileId: 'f-1' }]
});

/* ---------------------------- 汇总 ---------------------------- */

test('summarize: 空草稿与完整草稿', () => {
  assert.equal(R.summarize(EMPTY_DRAFT).claims, 0);
  const s = R.summarize(FULL_DRAFT);
  assert.equal(s.claims, 4);
  assert.equal(s.confirmed, 2);
  assert.equal(s.pending, 1);
  assert.equal(s.conflictClaims, 1);
  assert.equal(s.conflictsOpen, 1);
  assert.equal(s.researchSelection, 1);
  assert.equal(s.boundaries, 1);
});

test('summarize: null 不抛错', () => {
  assert.doesNotThrow(() => R.summarize(null));
  assert.equal(R.summarize(null).claims, 0);
});

/* ---------------------------- 页头 / 指标 ---------------------------- */

test('renderHead: 项目与可用主张计数', () => {
  const html = R.renderHead(PROJECT, FULL_DRAFT);
  assert.ok(html.includes('素与 Su&amp; 氨基酸洁面'));
  assert.ok(html.includes('PRJ-202609-7K3M2Q'));
  assert.ok(html.includes('可用主张 2'));
  assert.ok(html.includes('证据强度、体验缺口与拟用策略字段未提供'));
});

test('renderMetrics: 4 张指标卡', () => {
  const html = R.renderMetrics(FULL_DRAFT);
  assert.equal(occurrences(html, 'class="card metric"'), 4);
  assert.ok(html.includes('主张总数'));
  assert.ok(html.includes('可用'));
  assert.ok(html.includes('待确认'));
});

/* ---------------------------- 主张表 ---------------------------- */

test('renderClaimsCard: 陈述 / 来源 / 状态 / 未提供列', () => {
  const html = R.renderClaimsCard(FULL_DRAFT);
  assert.equal(occurrences(html, '<tr'), 5); // 表头 + 4 行
  assert.ok(html.includes('氨基酸表活占比 ≥60%'));
  assert.ok(html.includes('R-014 品牌官方页'));
  assert.ok(html.includes('pH 5.5±0.5 弱酸性'));
  assert.ok(html.includes('https://x.example.com/a'));
  assert.ok(html.includes('未提供'));
  assert.ok(html.includes('可用'));
  assert.ok(html.includes('冲突'));
  assert.ok(html.includes('待确认'));
  assert.ok(html.includes('接口未提供'));
  assert.ok(html.includes('不编造'));
});

test('renderClaimsCard: 空数组 → 空态', () => {
  const html = R.renderClaimsCard(EMPTY_DRAFT);
  assert.ok(html.includes('暂无可用主张'));
});

test('renderClaimsCard: 未知 state 原样展示', () => {
  const draft = Object.assign({}, EMPTY_DRAFT, {
    claims: [{ claimId: 'c-9', statement: 'x', state: 'OBSOLETE' }]
  });
  const html = R.renderClaimsCard(draft);
  assert.ok(html.includes('OBSOLETE'));
  assert.ok(html.includes('tag-muted'));
});

/* ---------------------------- 冲突 / 边界 / 选用 ---------------------------- */

test('renderConflictsCard: 类型 / 状态 / 阻断', () => {
  const html = R.renderConflictsCard(FULL_DRAFT);
  assert.ok(html.includes('来源冲突') && html.includes('事实与限制冲突'));
  assert.ok(html.includes('待处置') && html.includes('已处置'));
  assert.ok(html.includes('阻断'));
  assert.ok(R.renderConflictsCard(EMPTY_DRAFT).includes('暂无冲突'));
});

test('renderBoundariesCard: 键值数组与空态', () => {
  const html = R.renderBoundariesCard(FULL_DRAFT);
  assert.ok(html.includes('forbidden'));
  assert.ok(html.includes('医美级') && html.includes('抗炎'));
  assert.ok(R.renderBoundariesCard(EMPTY_DRAFT).includes('暂无表达边界'));
});

test('renderSelectionCard: 空数组与条目', () => {
  assert.ok(R.renderSelectionCard(EMPTY_DRAFT).includes('暂无研究资料选用'));
  assert.ok(R.renderSelectionCard(FULL_DRAFT).includes('f-1'));
});

/* ---------------------------- 转义 / 错误 / 纯度 ---------------------------- */

test('主张 / 冲突 / 边界里的 HTML 被转义', () => {
  const draft = Object.assign({}, EMPTY_DRAFT, {
    claims: [{ claimId: 'c-x', statement: '<script>alert(1)</script>', source: '<img src=x>', state: 'PENDING' }],
    boundaries: { note: '<b>bad</b>' }
  });
  const html = R.renderClaimsCard(draft) + R.renderBoundariesCard(draft);
  assert.equal(html.includes('<script>'), false);
  assert.equal(html.includes('<img'), false);
  assert.equal(html.includes('<b>bad</b>'), false);
});

test('null 输入不抛错', () => {
  assert.doesNotThrow(() => R.renderHead(null, null));
  assert.doesNotThrow(() => R.renderMetrics(null));
  assert.doesNotThrow(() => R.renderClaimsCard(null));
  assert.doesNotThrow(() => R.renderConflictsCard(null));
});

test('renderCardLoading / renderCardError 分支', () => {
  assert.ok(R.renderCardLoading('读主张').includes('读主张'));
  assert.ok(R.renderCardError({ code: 'DEPENDENCY_UNAVAILABLE', offline: true }).includes('127.0.0.1:8787'));
  assert.ok(R.renderCardError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderCardError({ message: '<b>x</b>' }).includes('&lt;b&gt;x&lt;/b&gt;'));
});

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'claims.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) process.exitCode = 1;
