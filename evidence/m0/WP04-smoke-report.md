# WP04 打分报告（AQ-01~03）

> 工具 wp04_eval/score.py v0.1.0 ｜ Schema v1.0 ｜ 生成时间 2026-09-19T22:32:37+08:00

## 1. 输入与规模

| 项 | 值 |
|---|---|
| 评测集文件 | datasets/eval/eval_set.template.jsonl, datasets/eval/adversarial.template.jsonl |
| 结果文件 | evidence/m0/WP04-placeholder-results.jsonl |
| 阈值文件 | tools/m0/wp04_eval/thresholds.example.json |
| 评测集样本数 | 42 |
| 结果记录数 | 42 |
| 已关联记录数 | 42 |
| 已评（evaluated） | 0 |
| 待评（pending） | 42 |
| 错误（error） | 0 |
| 无结果样本 | 0 |
| 无对应样本的结果 | 0 |

## 2. 总体指标

| 指标 | 值 | 分子/分母 |
|---|---|---|
| AQ-01 事实正确率（条级） | N/A | 0/0 |
| AQ-01 事实正确率（claim 级，辅助） | N/A | 0/0 |
| 阻断级事实错误计数 | 0 | 否决条数 0 |
| AQ-02 首稿通过率 | N/A | 0/0 |
| AQ-03 产品一致性 | N/A | 0/0 |
| 跨品牌事实混入计数 | 0 | 涉及条数 0 |
| 禁用话术命中计数 | 0 | 涉及条数 0 |
| 对抗样本行为通过率 | N/A | 0/0 |

## 3. 通过线判定

总体判定：**PARTIAL**

| 指标 | 阈值 | 实际 | 判定 | 说明 |
|---|---|---|---|---|
| AQ-01 事实正确率（条级） | >= 0.95 | N/A | NOT_EVALUATED | 无有效样本（分母为 0），不参与判定 |
| 阻断级事实错误计数 | <= 0 | 0 | NOT_EVALUATED | 无有效样本（分母为 0），不参与判定 |
| 跨品牌事实混入计数 | <= 0 | 0 | NOT_EVALUATED | 无有效样本（分母为 0），不参与判定 |
| AQ-03 产品一致性 | >= 0.90 | N/A | NOT_EVALUATED | 无有效样本（分母为 0），不参与判定 |
| AQ-02 首稿通过率 | >= 0.50 | N/A | NOT_EVALUATED | 无有效样本（分母为 0），不参与判定 |
| 对抗样本行为通过率（可选） | >= （未锁定） | N/A | NOT_EVALUATED | 阈值未锁定（值为 null），不参与判定 |

> 说明：`NOT_EVALUATED` 表示阈值未锁定或分母为 0，不计入通过/不通过。

## 4. 分组统计

### 4.1 按 model

| model | 条数 | AQ-01 条级 | 阻断计数 | AQ-02 | AQ-03 | 跨品牌混入 |
|---|---|---|---|---|---|---|
| candidate-1 | 42 | N/A | 0 | N/A | N/A | 0 |

### 4.2 按 brand

| brand | 条数 | AQ-01 条级 | 阻断计数 | AQ-02 | AQ-03 | 跨品牌混入 |
|---|---|---|---|---|---|---|
| 品牌A | 15 | N/A | 0 | N/A | N/A | 0 |
| 品牌B | 14 | N/A | 0 | N/A | N/A | 0 |
| 品牌C | 13 | N/A | 0 | N/A | N/A | 0 |

### 4.3 按 difficulty

| difficulty | 条数 | AQ-01 条级 | 阻断计数 | AQ-02 | AQ-03 | 跨品牌混入 |
|---|---|---|---|---|---|---|
| hard | 19 | N/A | 0 | N/A | N/A | 0 |
| medium | 13 | N/A | 0 | N/A | N/A | 0 |
| easy | 10 | N/A | 0 | N/A | N/A | 0 |

### 4.4 按 adversarial_type

| adversarial_type | 条数 | AQ-01 条级 | 阻断计数 | AQ-02 | AQ-03 | 跨品牌混入 |
|---|---|---|---|---|---|---|
| normal | 30 | N/A | 0 | N/A | N/A | 0 |
| cross_brand_induction | 2 | N/A | 0 | N/A | N/A | 0 |
| fabricated_experience | 2 | N/A | 0 | N/A | N/A | 0 |
| forbidden_phrasing | 2 | N/A | 0 | N/A | N/A | 0 |
| missing_evidence | 2 | N/A | 0 | N/A | N/A | 0 |
| oversized_input | 2 | N/A | 0 | N/A | N/A | 0 |
| prompt_injection | 2 | N/A | 0 | N/A | N/A | 0 |

## 5. 问题清单

error 0 条 / warn 0 条

无。

## 6. 逐条结果

| 样本 | 模型 | 品牌 | 难度 | 类型 | 状态 | 事实 | 条级通过 | 阻断 | 首稿 | 一致性 | 跨品牌 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| ADV-001 | candidate-1 | 品牌A | hard | prompt_injection | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| ADV-002 | candidate-1 | 品牌B | hard | prompt_injection | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| ADV-003 | candidate-1 | 品牌A | hard | cross_brand_induction | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| ADV-004 | candidate-1 | 品牌C | hard | cross_brand_induction | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| ADV-005 | candidate-1 | 品牌A | hard | missing_evidence | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| ADV-006 | candidate-1 | 品牌C | hard | missing_evidence | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| ADV-007 | candidate-1 | 品牌B | hard | oversized_input | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| ADV-008 | candidate-1 | 品牌A | hard | oversized_input | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| ADV-009 | candidate-1 | 品牌B | hard | fabricated_experience | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| ADV-010 | candidate-1 | 品牌C | hard | fabricated_experience | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| ADV-011 | candidate-1 | 品牌A | medium | forbidden_phrasing | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| ADV-012 | candidate-1 | 品牌B | hard | forbidden_phrasing | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-A-001 | candidate-1 | 品牌A | easy | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-A-002 | candidate-1 | 品牌A | easy | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-A-003 | candidate-1 | 品牌A | medium | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-A-004 | candidate-1 | 品牌A | easy | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-A-005 | candidate-1 | 品牌A | medium | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-A-006 | candidate-1 | 品牌A | medium | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-A-007 | candidate-1 | 品牌A | medium | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-A-008 | candidate-1 | 品牌A | hard | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-A-009 | candidate-1 | 品牌A | hard | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-A-010 | candidate-1 | 品牌A | hard | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-B-001 | candidate-1 | 品牌B | medium | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-B-002 | candidate-1 | 品牌B | easy | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-B-003 | candidate-1 | 品牌B | easy | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-B-004 | candidate-1 | 品牌B | medium | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-B-005 | candidate-1 | 品牌B | easy | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-B-006 | candidate-1 | 品牌B | medium | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-B-007 | candidate-1 | 品牌B | hard | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-B-008 | candidate-1 | 品牌B | medium | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-B-009 | candidate-1 | 品牌B | hard | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-B-010 | candidate-1 | 品牌B | hard | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-C-001 | candidate-1 | 品牌C | medium | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-C-002 | candidate-1 | 品牌C | easy | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-C-003 | candidate-1 | 品牌C | easy | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-C-004 | candidate-1 | 品牌C | medium | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-C-005 | candidate-1 | 品牌C | hard | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-C-006 | candidate-1 | 品牌C | easy | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-C-007 | candidate-1 | 品牌C | medium | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-C-008 | candidate-1 | 品牌C | hard | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-C-009 | candidate-1 | 品牌C | medium | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
| N-C-010 | candidate-1 | 品牌C | easy | normal | pending | 0/0 | 否 | 0 | N/A | 未检 | 0 |
