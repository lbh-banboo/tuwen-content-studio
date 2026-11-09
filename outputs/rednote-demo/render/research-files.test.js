/* render/research-files.test.js —— 资料文件核对页（方案 A 纯函数）Node 单测
 * 运行：node render/research-files.test.js   （零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./research-files.js');

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

const META = {
  research: {
    limitBytes: '300000000', effectiveBytes: '150262169', reservedBytes: '0',
    remainingBytes: '149737831', listedBytes: '150262169',
    fileCount: 8, usableEvidenceCount: 6, reconciled: true
  }
};

/* 与真后端 GET /research-files 一致 */
const FILES = [
  {
    fileId: '4ed615c7-7f58-5039-a654-df1ee3c59f67',
    originalName: '场景研究_长文档合集.pdf',
    mimeType: 'application/pdf',
    byteSize: '67318579',
    classification: 'SUPPORTED',
    scanStatus: 'CLEAN',
    purpose: 'RESEARCH',
    effective: true,
    uploadedBy: '365bcf69-fc8b-5dc1-9144-00cc975daeb8',
    createdAt: '2026-09-18T17:00:00+08:00',
    parse: {
      state: 'RUNNING', complete: false, parseRunId: 'a730cca9-91d0-5507-a717-1d52c5fd5f1c',
      parserVersion: 'wp17-1', segmentCount: 4, totalUnits: '240', parsedUnits: '61', errors: []
    },
    evidenceUsable: false
  },
  {
    fileId: '6d7833ea-5198-5e68-99cb-e98618289bd3',
    originalName: '竞品话术拆解_12条.pdf',
    mimeType: 'application/pdf',
    byteSize: '29780787',
    classification: 'SUPPORTED',
    scanStatus: 'CLEAN',
    purpose: 'RESEARCH',
    effective: true,
    uploadedBy: '365bcf69-fc8b-5dc1-9144-00cc975daeb8',
    createdAt: '2026-09-16T17:00:00+08:00',
    parse: {
      state: 'PARTIAL', complete: false, parseRunId: 'fff70dee-db38-54e6-93e8-331188b1ef52',
      parserVersion: 'wp17-1', segmentCount: 4, totalUnits: '96', parsedUnits: '78',
      errors: [{ unit: 5, message: '解析器在复杂版式上提前中止。' }]
    },
    evidenceUsable: true
  },
  {
    fileId: '6839f7ff-7649-57fb-a31c-e16df668d03a',
    originalName: '产品问答_客服纪要.txt',
    mimeType: 'text/plain',
    byteSize: '104858',
    classification: 'SUPPORTED',
    scanStatus: 'CLEAN',
    purpose: 'RESEARCH',
    effective: true,
    uploadedBy: 'e3f629d1-384a-507a-8ce0-5c627c6b52f6',
    createdAt: '2026-09-21T17:00:00+08:00',
    parse: {
      state: 'NONE', complete: false, parseRunId: null, parserVersion: null,
      segmentCount: 0, totalUnits: null, parsedUnits: null, errors: []
    },
    evidenceUsable: false
  }
];

const FILE_DETAIL = {
  fileId: '6839f7ff-7649-57fb-a31c-e16df668d03a',
  projectId: PROJECT.id,
  fileName: '产品问答_客服纪要.txt',
  byteSize: '104858',
  sha256: '9bb035a071f3034bc54c99ee5a942746681a4a1d835893c74c5192c9614fe0b7',
  mimeType: 'text/plain',
  classification: 'SUPPORTED',
  supported: true,
  scanStatus: 'CLEAN',
  storageState: 'ACTIVE',
  uploadedBy: 'e3f629d1-384a-507a-8ce0-5c627c6b52f6',
  createdAt: '2026-09-21T17:00:00+08:00',
  latestParse: null
};

/* ---------------------------- 配额 ---------------------------- */

test('renderQuotaCards: 配额 / 文件数 / 剩余 / 对账', () => {
  const html = R.renderQuotaCards(META);
  assert.equal(occurrences(html, 'class="card metric"'), 4);
  assert.ok(html.includes('150.3 MB'));
  assert.ok(html.includes('50.1%'));
  assert.ok(html.includes('可用证据 6 份'));
  assert.ok(html.includes('已对账'));
});

test('renderQuotaCards: 无 meta → —', () => {
  const html = R.renderQuotaCards(null);
  assert.ok(html.includes('na') && html.includes('meta.research 未提供'));
});

/* ---------------------------- 文件表 ---------------------------- */

test('renderFilesTable: 3 行 + 解析状态 + 单元进度 + 错误原因', () => {
  const html = R.renderFilesTable(FILES);
  assert.equal(occurrences(html, '<tr>'), 4); // 表头 + 3 行
  assert.ok(html.includes('场景研究_长文档合集.pdf'));
  assert.ok(html.includes('解析中'));
  assert.ok(html.includes('61 / 240'));
  assert.ok(html.includes('部分成功'));
  assert.ok(html.includes('单元 5：解析器在复杂版式上提前中止。'));
  assert.ok(html.includes('未解析'));
});

test('renderFileRow: 详情按钮带 data-file-id；证据可用标记', () => {
  const html = R.renderFileRow(FILES[1]);
  assert.ok(html.includes('data-file-id="6d7833ea-5198-5e68-99cb-e98618289bd3"'));
  assert.ok(html.includes('>可用<'));
  const noE = R.renderFileRow(FILES[0]);
  assert.ok(noE.includes('>不可用<'));
});

test('renderFileRow: 类型图标按 mimeType 选择', () => {
  assert.ok(R.renderFileRow(FILES[0]).includes('fa-file-pdf'));
  assert.ok(R.renderFileRow(FILES[2]).includes('fa-file-lines'));
});

test('renderFilesTable: 空数组 → 空态', () => {
  assert.ok(R.renderFilesTable([]).includes('暂无研究资料'));
  assert.ok(R.renderFilesTable(null).includes('暂无研究资料'));
});

/* ---------------------------- 详情 ---------------------------- */

test('renderDetail: 文件详情字段来自 GET /files/{id}', () => {
  const html = R.renderDetail(FILE_DETAIL);
  assert.ok(html.includes('产品问答_客服纪要.txt'));
  assert.ok(html.includes('9bb035a071f3034bc54c99ee5a942746681a4a1d835893c74c5192c9614fe0b7'));
  assert.ok(html.includes('ACTIVE'));
  assert.ok(html.includes('SUPPORTED'));
  assert.ok(html.includes('CLEAN'));
  assert.ok(html.includes('104.9 KB'));
  assert.ok(html.includes('2026-09-21 17:00'));
  assert.ok(html.includes('null')); // latestParse
});

test('renderDetail: latestParse 非空时显示状态', () => {
  const html = R.renderDetail(Object.assign({}, FILE_DETAIL, { latestParse: { state: 'SUCCEEDED' } }));
  assert.ok(html.includes('SUCCEEDED'));
});

test('renderDetailPlaceholder / Loading / Error', () => {
  assert.ok(R.renderDetailPlaceholder().includes('选择一份文件查看详情'));
  assert.ok(R.renderDetailLoading().includes('正在读取文件详情…'));
  assert.ok(R.renderCardLoading('读取配额…').includes('读取配额…'));
  assert.ok(R.renderDetailError({ code: 'RESOURCE_NOT_FOUND', message: '不存在' }).includes('不存在'));
  assert.ok(R.renderDetailError(null).includes('读取文件详情失败'));
});

/* ---------------------------- 页头 / 转义 / 错误 ---------------------------- */

test('renderHead: 项目 / 文件数 / 容量', () => {
  const html = R.renderHead(PROJECT, META);
  assert.ok(html.includes('素与 Su&amp; 氨基酸洁面'));
  assert.ok(html.includes('PRJ-202609-7K3M2Q'));
  assert.ok(html.includes('8 份'));
  assert.ok(html.includes('步骤 2 / 8'));
});

test('文件名与 sha 里的 HTML 被转义', () => {
  const html = R.renderFilesTable([{
    fileId: 'f1', originalName: '<script>x</script>.pdf', mimeType: 'application/pdf',
    byteSize: '1', uploadedBy: '"><b>x</b>', createdAt: '2026-01-01T00:00:00+08:00',
    parse: { state: 'NONE', errors: [] }, evidenceUsable: false
  }]);
  assert.equal(html.includes('<script>'), false);
  assert.equal(html.includes('<b>x</b>'), false);
  assert.ok(html.includes('&lt;script&gt;'));
});

test('renderHead / renderDetail: null 不抛错', () => {
  assert.doesNotThrow(() => R.renderHead(null, null));
  assert.doesNotThrow(() => R.renderDetail(null));
});

test('renderCardError 分支', () => {
  assert.ok(R.renderCardError({ code: 'DEPENDENCY_UNAVAILABLE', offline: true }).includes('127.0.0.1:8787'));
  assert.ok(R.renderCardError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderCardError({ message: '<b>x</b>' }).includes('&lt;b&gt;x&lt;/b&gt;'));
});

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'research-files.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) process.exitCode = 1;
