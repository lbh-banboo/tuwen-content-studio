/* render/brief.test.js —— Brief 解析页（方案 A 纯函数）Node 单测
 * 运行：node render/brief.test.js   （零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./brief.js');

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
  name: '素与 Su& 氨基酸洁面',
  phase: 'PREPARATION'
};

function emptyFields() {
  const out = {};
  R.FIELDS.forEach((spec) => { out[spec.path] = { value: null, status: 'MISSING' }; });
  return out;
}

/* 形状与真后端 GET /projects/{id}/brief 一致（种子项目为全 MISSING） */
function makeBrief(overrides) {
  const brief = {
    briefId: '707409d7-f387-42ef-ad72-dfa495ffa24e',
    projectId: PROJECT.id,
    activeVersionId: null,
    draft: {
      draftId: '0565ad68-0fd4-48b8-ba70-681d04f89d05',
      baseVersionId: null,
      lockVersion: 1,
      etag: '"0565ad68-0fd4-48b8-ba70-681d04f89d05:1"',
      structuredFields: emptyFields(),
      claims: [],
      issueResolutions: [],
      suggestions: [],
      suggestionsVersionId: null,
      sourceFileId: null,
      sourceParseRunId: null,
      sourceState: 'NONE',
      sourceErrors: [],
      schemaVersion: 1,
      updatedAt: '2026-10-17T10:37:54.284853+08:00'
    },
    version: null,
    versions: [],
    blockingIssueCount: 0
  };
  return Object.assign(brief, overrides || {});
}

/* ---------------------------- 汇总 ---------------------------- */

test('summarize: 全 MISSING 的草稿 → 20 项待补充，5 项必填缺失', () => {
  const s = R.summarize(makeBrief());
  assert.equal(s.total, 20);
  assert.equal(s.filled, 0);
  assert.equal(s.missing, 20);
  assert.equal(s.notApplicable, 0);
  assert.equal(s.requiredMissing, 6);
  assert.equal(s.versions, 0);
});

test('summarize: FILLED / NOT_APPLICABLE 分别计数，必填不适用不算缺失', () => {
  const brief = makeBrief();
  brief.draft.structuredFields['product.name'] = { value: '测试产品', status: 'FILLED' };
  brief.draft.structuredFields['product.brand'] = { value: '测试品牌', status: 'FILLED' };
  brief.draft.structuredFields['product.images'] = { value: null, status: 'NOT_APPLICABLE' };
  const s = R.summarize(brief);
  assert.equal(s.filled, 2);
  assert.equal(s.notApplicable, 1);
  assert.equal(s.requiredMissing, 4);
});

test('summarize: null / 非对象不抛错', () => {
  assert.doesNotThrow(() => R.summarize(null));
  const s = R.summarize('oops');
  assert.equal(s.total, 20);
  assert.equal(s.missing, 20);
});

/* ---------------------------- 页头 / 指标 ---------------------------- */

test('renderHead: 项目名 / 编号 / 步骤 / 未固化 / 必填缺失', () => {
  const html = R.renderHead(PROJECT, makeBrief());
  assert.ok(html.includes('素与 Su&amp; 氨基酸洁面'));
  assert.ok(html.includes('PRJ-202609-7K3M2Q'));
  assert.ok(html.includes('步骤 1 / 8'));
  assert.ok(html.includes('未固化版本'));
  assert.ok(html.includes('必填缺失 6'));
});

test('renderHead: 已固化版本显示短 ID', () => {
  const brief = makeBrief({ activeVersionId: 'aaaaaaaa-1111-4111-8111-111111111111' });
  const html = R.renderHead(PROJECT, brief);
  assert.ok(html.includes('已固化 aaaaaaaa…1111'));
});

test('renderMetrics: 4 张指标卡', () => {
  const html = R.renderMetrics(makeBrief());
  assert.equal(occurrences(html, 'class="card metric"'), 4);
  assert.ok(html.includes('字段总数'));
  assert.ok(html.includes('待补充'));
  assert.ok(html.includes('必填缺失'));
});

/* ---------------------------- 结构化字段 ---------------------------- */

test('renderStructuredCard: 10 组标题 + 20 个字段行 + 状态标签', () => {
  const html = R.renderStructuredCard(makeBrief());
  for (let g = 1; g <= 10; g += 1) {
    assert.ok(html.includes('第 ' + g + ' 组 · '), 'group ' + g);
  }
  assert.equal(occurrences(html, 'class="fld"'), 20);
  assert.equal(occurrences(html, '待补充'), 20);
  assert.ok(html.includes('product.image_usage_rights'));
  assert.ok(html.includes('必填'));
});

test('renderStructuredCard: 已填写值 / 多值 / 不适用都正确渲染', () => {
  const brief = makeBrief();
  brief.draft.structuredFields['product.name'] = { value: '氨基酸洁面乳', status: 'FILLED' };
  brief.draft.structuredFields['product.selling_points'] = {
    value: ['温和清洁', '洗后不紧绷'], status: 'FILLED'
  };
  brief.draft.structuredFields['product.images'] = { value: null, status: 'NOT_APPLICABLE' };
  const html = R.renderStructuredCard(brief);
  assert.ok(html.includes('氨基酸洁面乳'));
  assert.ok(html.includes('温和清洁') && html.includes('洗后不紧绷'));
  assert.ok(html.includes('不适用'));
});

test('renderStructuredCard: 响应中的未知字段原样追加，不丢弃', () => {
  const brief = makeBrief();
  brief.draft.structuredFields['custom.field'] = { value: 'x', status: 'FILLED' };
  const html = R.renderStructuredCard(brief);
  assert.ok(html.includes('其他字段'));
  assert.ok(html.includes('custom.field'));
  assert.equal(occurrences(html, 'class="fld"'), 21);
});

test('renderStructuredCard: 草稿为 null 时显示空态', () => {
  const html = R.renderStructuredCard({ draft: null });
  assert.ok(html.includes('尚无结构化草稿'));
});

/* ---------------------------- 原文 / 版本 ---------------------------- */

test('renderSourceCard: 明确说明 GET /brief 不含原文正文', () => {
  const html = R.renderSourceCard(makeBrief());
  assert.ok(html.includes('GET /brief 不返回原文正文'));
  assert.ok(html.includes('未提供原文'));
  assert.ok(html.includes('sourceFileId'));
  assert.ok(html.includes('brief-parse-jobs'));
});

test('renderSourceCard: sourceErrors 逐条展示', () => {
  const brief = makeBrief();
  brief.draft.sourceState = 'FAILED';
  brief.draft.sourceErrors = [{ code: 'OCR_FAILED', message: 'PDF 已加密' }];
  const html = R.renderSourceCard(brief);
  assert.ok(html.includes('解构失败'));
  assert.ok(html.includes('PDF 已加密'));
});

test('renderVersionsCard: 空版本列表显示未固化', () => {
  const html = R.renderVersionsCard(makeBrief());
  assert.ok(html.includes('尚未固化 Brief 版本'));
  assert.ok(html.includes('activeVersionId'));
});

test('renderVersionsCard: 版本行来自 versions[]', () => {
  const brief = makeBrief({
    activeVersionId: 'aaaaaaaa-1111-4111-8111-111111111111',
    versions: [{
      versionId: 'aaaaaaaa-1111-4111-8111-111111111111',
      revisionNo: 2,
      productName: '氨基酸洁面乳',
      summary: '卖点 3 项',
      createdAt: '2026-09-11T11:05:00+08:00'
    }]
  });
  const html = R.renderVersionsCard(brief);
  assert.ok(html.includes('第 2 版'));
  assert.ok(html.includes('氨基酸洁面乳'));
  assert.ok(html.includes('2026-09-11 11:05'));
});

/* ---------------------------- 转义 / 边界 / 错误 ---------------------------- */

test('renderHead / renderStructuredCard: 名称与字段值里的 HTML 被转义', () => {
  const brief = makeBrief();
  brief.draft.structuredFields['product.name'] = { value: '<script>alert(1)</script>', status: 'FILLED' };
  const head = R.renderHead({ name: '<img src=x>', projectCode: '"><b>x</b>' }, brief);
  const fields = R.renderStructuredCard(brief);
  assert.equal(head.includes('<img'), false);
  assert.equal(head.includes('<b>x</b>'), false);
  assert.equal(fields.includes('<script>'), false);
  assert.ok(fields.includes('&lt;script&gt;'));
});

test('renderHead / renderMetrics / renderSourceCard: null 不抛错', () => {
  assert.doesNotThrow(() => R.renderHead(null, null));
  assert.doesNotThrow(() => R.renderMetrics(null));
  assert.doesNotThrow(() => R.renderSourceCard(null));
  assert.ok(R.renderSourceCard(null).includes('未获取到 Brief 数据'));
});

test('renderCardLoading / renderCardError: 加载与三种错误分支', () => {
  assert.ok(R.renderCardLoading('读 Brief').includes('读 Brief'));
  assert.ok(R.renderCardError({ code: 'DEPENDENCY_UNAVAILABLE', offline: true }).includes('127.0.0.1:8787'));
  assert.ok(R.renderCardError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderCardError({ code: 'RESOURCE_NOT_FOUND', retryable: false }).includes('请按错误码排查'));
  assert.ok(R.renderCardError(null).includes('读取失败'));
});

test('renderCardError: 错误文案里的 HTML 被转义', () => {
  const html = R.renderCardError({ message: '<img src=x onerror=alert(1)>' });
  assert.equal(html.includes('<img'), false);
  assert.ok(html.includes('&lt;img'));
});

/* ---------------------------- 纯度（机械证明） ---------------------------- */

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'brief.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) process.exitCode = 1;
