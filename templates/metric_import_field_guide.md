# 指标导入模板 · 字段逐项说明

> 配套文件：`templates/metric_import.template.csv`（模板）、`docs/m0/WP03-指标口径与导入模板.md`（口径）、`tools/m0/wp03_data/validate_import.py`（校验工具）
> 模板版本：1.0 ｜ 列数：11 ｜ 编码：UTF-8 ｜ 引号：ASCII 直引号 `"`

---

## 0. 通用规则

1. **长表结构**：一行 = 一个指标的一个观测值。同一笔记有 6 个指标就写 6 行。
2. **注释行**：以 `#` 开头的行被校验工具与导入器跳过。正式数据文件可保留也可删除。
3. **表头行**：第一个非注释行必须是表头，列名与顺序不得改动。
4. **缺失不填 0**：拿不到值时 `value` 留空，用 `availability` 说明原因（见 §9）。
5. **引号**：字段内含逗号/换行时用 ASCII 双引号包裹；**不得**使用中文引号 `“ ”`。
6. **编码**：UTF-8（带不带 BOM 均可，工具会处理 BOM）。
7. **示例行**：模板中 `publication_key` 以 `SAMPLE-` 开头的两行是**虚构占位值**，正式导入前必须删除。

---

## 1. `publication_key`

| 项 | 内容 |
|---|---|
| **含义** | 发布记录的业务主键，用于把指标关联到「某条已登记的发布记录」。对应需求 §12.1 的「发布记录编号」 |
| **格式** | 非空字符串；`A-Z a-z 0-9 _ -` 与点号；长度 1–64；**不得含逗号**；不得为纯数字 0 |
| **是否必填** | ✅ 必填 |
| **填写示例** | `PUB-2026-0001`、`SAMPLE-PUB-0001` |
| **常见错误** | ① 留空；② 含逗号（破坏 CSV 列）；③ 同一笔记的多个指标用了不同的 key（应相同）；④ 用笔记标题当 key（标题会变，不稳定） |

> 校验工具会检查该值是否重复出现在**同一指标同一观测时刻**上（重复 = 重复导入）。

---

## 2. `metric_code`

| 项 | 内容 |
|---|---|
| **含义** | 指标代码，决定这一行的语义 |
| **格式** | 必须取自词表（区分大小写） |
| **是否必填** | ✅ 必填 |
| **可选值** | 数值类：`VIEW_COUNT` `IMPRESSION_COUNT` `LIKE_COUNT` `FAVORITE_COUNT` `COMMENT_COUNT` `LINK_CLICK_PV` `LINK_CLICK_UV` `IS_PROMOTED`<br>文本类：`COMMENT_TEXT` `NOTE_URL` `NOTE_ID` `PUBLISHED_AT` `PROMOTION_INFO` `PUBLISH_STATUS` |
| **填写示例** | `LIKE_COUNT` |
| **常见错误** | ① 自造代码（如 `READ_COUNT`、`VIEWS`）；② 小写（如 `like_count`）；③ 把曝光量写成 `VIEW_COUNT`（见 §5 与 §9 的口径告警）；④ 把点击次数与人数混成一个代码 |

---

## 3. `metric_scope`

| 项 | 内容 |
|---|---|
| **含义** | 该值的统计窗口。**累计值与窗口值不可比较、不可相加** |
| **格式** | 必须取自词表（区分大小写） |
| **是否必填** | ✅ 必填 |
| **可选值** | `CUMULATIVE`（累计）｜`LAST_24H`｜`LAST_7D`｜`LAST_30D`｜`LAST_90D`｜`NOT_APPLICABLE`（文本类指标）｜`UNKNOWN`（窗口未知，**会告警**） |
| **填写示例** | `CUMULATIVE` |
| **常见错误** | ① 留空；② 文本类指标（如 `NOTE_URL`）填 `CUMULATIVE`（应填 `NOT_APPLICABLE`）；③ 点击类指标（`LINK_CLICK_PV/UV`）填 `UNKNOWN`——平台未说明窗口时必须填 `UNKNOWN`，但会触发告警，需运营确认；④ 把「近 7 天」的值当累计值登记 |

> **重要**：单次观测无法区分累计值与窗口值。实测协议要求对同一笔记做二次观测（间隔 ≥1 小时）后再判定。

---

## 4. `observed_at`

| 项 | 内容 |
|---|---|
| **含义** | **观测时刻**（不是录入时刻）。每个指标有自己的观测时间，系统必须显示时间差 |
| **格式** | ISO 8601，**必须带时区**。推荐 `YYYY-MM-DDTHH:MM:SS+08:00` |
| **是否必填** | ✅ 必填 |
| **填写示例** | `2026-01-01T10:00:00+08:00` |
| **常见错误** | ① 缺时区（如 `2026-01-01 10:00`）——校验报错；② 用空格代替 `T`——报错；③ 填录入时间而非观测时间（会导致时间差失真）；④ 用未来时间（报错） |

> 校验工具会拒绝**明显未来**的时间戳，并告警**过于久远**（>365 天）的时间戳。

---

## 5. `value`

| 项 | 内容 |
|---|---|
| **含义** | 规范化后的数值，可直接参与求和与阈值判断 |
| **格式** | **非负整数**（不带千分位、不带单位）。**文本类指标必须留空** |
| **是否必填** | 数值类：`availability=ACQUIRED` 时必填；文本类：必须留空 |
| **填写示例** | `12000`（对应 `raw_value="1.2 万"`） |
| **常见错误** | ① 写 `12,000`（千分位，报错）；② 写 `1.2万` 或 `12000次`（带单位，报错）；③ 写 `-5`（负数，报错）；④ 拿不到时填 `0`（**口径违规**，见 §9）；⑤ 文本类指标把内容写进 `value`（如把 URL 写进 `value`，报错）；⑥ 把 `1.2 万` 直接换算成 `12000` 却标 `EXACT`（口径违规） |

---

## 6. `raw_value`

| 项 | 内容 |
|---|---|
| **含义** | 平台**原样显示值**，一个字符都不改。用于保留精度与可追溯性 |
| **格式** | 字符串；`availability=ACQUIRED` 时**必填** |
| **是否必填** | `ACQUIRED` 时 ✅ 必填；`PENDING` 时 ✅ 必填；其余状态可留空 |
| **填写示例** | `1.2 万`、`约 300`、`1.5k`、`100+`、`https://...`（URL 原文）、`2026-01-01`（发布时间原文） |
| **常见错误** | ① 丢失原值只填 `value`（**口径违规**，无法判断约数与阈值跨越）；② 把 `1.2 万` 改写成 `12000`（**口径违规**，约数被伪装成精确值）；③ 单位写成 `万` 但 `value` 是 `120`（量级不一致，报错）；④ 用中文引号包裹 |

> **量级一致性检查**：若 `raw_value` 含「万」而 `value` 不在 `[10000, 99999999]` 区间，校验报错；含「千」/「k」而 `value` 不在 `[1000, 99999]` 区间，报错。

---

## 7. `precision`

| 项 | 内容 |
|---|---|
| **含义** | 该值的精度性质 |
| **格式** | `EXACT` 或 `APPROXIMATE`（区分大小写） |
| **是否必填** | `availability=ACQUIRED` 时 ✅ 必填 |
| **填写示例** | 精确值 → `EXACT`；平台显示 `1.2 万` → `APPROXIMATE` |
| **常见错误** | ① `raw_value` 明显是约数（含「万/千/k/约/+/余」）却标 `EXACT`（**口径违规告警**）；② 留空；③ 写小写 `exact`；④ 写 `APPROX`（不在词表内） |

> **判定口诀**：`raw_value` 里出现单位缩写或模糊词 → 一定是 `APPROXIMATE`。

---

## 8. `availability`

| 项 | 内容 |
|---|---|
| **含义** | 字段状态，说明「这个值是拿到的、还是没有、还是拿不到」 |
| **格式** | 必须取自词表（区分大小写） |
| **是否必填** | ✅ 必填（六态之一，**不得留空**） |
| **可选值** | `ACQUIRED`（已获取）｜`NOT_PROVIDED`（未提供）｜`NO_PERMISSION`（无权限）｜`NOT_APPLICABLE`（不适用）｜`FETCH_FAILED`（获取失败）｜`PENDING`（待确认） |
| **填写示例** | 成功取到 → `ACQUIRED`；平台没有该功能 → `NOT_PROVIDED`；账号看不到 → `NO_PERMISSION` |
| **常见错误** | ① 留空；② 拿不到时填 `ACQUIRED` 并把 `value` 填 `0`（**最严重的口径违规**）；③ 把 `PENDING` 当终态长期挂着（确认后须转 `ACQUIRED`）；④ 用 `UNAVAILABLE`（那是实测协议的等级词，不是本字段的状态词） |

**六态速查**：

| 代码 | 中文 | 参与汇总 | `value` 要求 |
|---|---|---|---|
| `ACQUIRED` | 已获取 | ✅ | 必填 |
| `NOT_PROVIDED` | 未提供 | ❌ | 必须留空 |
| `NO_PERMISSION` | 无权限 | ❌ | 必须留空 |
| `NOT_APPLICABLE` | 不适用 | ❌ | 必须留空 |
| `FETCH_FAILED` | 获取失败 | ❌ | 必须留空 |
| `PENDING` | 待确认 | ❌ | 可填，但 `raw_value` 必填 |

---

## 9. `source_type`

| 项 | 内容 |
|---|---|
| **含义** | 数据来源类型，用于判断证据强度 |
| **格式** | 必须取自词表（区分大小写） |
| **是否必填** | ✅ 必填 |
| **可选值** | `MANUAL`（人工录入）｜`CSV_IMPORT`｜`XLSX_IMPORT`｜`SCREENSHOT_OCR`（截图识别后确认）｜`AUTO_API`（自动采集，**须有实测证据**） |
| **填写示例** | 运营手工填 → `MANUAL`；从后台导出文件导入 → `CSV_IMPORT`；截图识别 → `SCREENSHOT_OCR` |
| **常见错误** | ① 留空；② 未经实测就填 `AUTO_API`（违反 G0-11「未实测不得标自动」）；③ 填 `OCR`（应为 `SCREENSHOT_OCR`） |

> `AUTO_API` 是本词表中**唯一需要额外举证**的值：必须在 `evidence_ref` 中给出实测证据引用。工具会对此告警提示。

---

## 10. `evidence_ref`

| 项 | 内容 |
|---|---|
| **含义** | 证据引用，指向支撑该值的原始材料（截图/导出文件/记录表） |
| **格式** | 非空字符串（当 `availability=ACQUIRED` 或 `source_type=AUTO_API` 时）；`A-Z a-z 0-9 _ - / .` |
| **是否必填** | `ACQUIRED` 时 ✅ 必填（建议）；`AUTO_API` 时 ✅ 必填 |
| **填写示例** | `WP03-shots/F02-like-20260101.png`、`SAMPLE-EVID-001`、`WP03-export/backend-export-20260101.csv` |
| **常见错误** | ① 成功取到值但无任何证据引用（无法追溯）；② 填「见截图」这类无定位信息的描述；③ 填本地绝对路径（归档后失效） |

---

## 11. `note`

| 项 | 内容 |
|---|---|
| **含义** | 自由备注：口径说明、异常情况、组件标识、读取位置等 |
| **格式** | 字符串，建议 ≤500 字符；含逗号时用 ASCII 双引号包裹 |
| **是否必填** | ❌ 选填 |
| **填写示例** | `口径含曝光，未拆分`、`组件标识 CMP-XXX`、`后台数据页左上角读取`、`示例，勿当真实数据` |
| **常见错误** | ① 用 note 记录本该进专用字段的信息（如把统计窗口写在 note 而 `metric_scope` 填 `UNKNOWN`）；② 把个人隐私信息（评论者昵称、手机号）写进备注 |

---

## 12. 校验与常见告警对照表

运行：

```bash
python3 tools/m0/wp03_data/validate_import.py --file templates/metric_import.template.csv
```

| 告警/错误 | 触发条件 | 处置 |
|---|---|---|
| `VALUE_ZERO_ON_MISSING` | `value=0` 但 `availability` 为 `NO_PERMISSION`/`NOT_PROVIDED`/`NOT_APPLICABLE`/`FETCH_FAILED` | 删掉 `value`，只保留状态（缺失不填 0） |
| `APPROX_MARKED_EXACT` | `raw_value` 含「万/千/k/约/+/余」但 `precision=EXACT` | 改 `precision=APPROXIMATE` |
| `EXACT_MISSING_RAW` | `precision=EXACT` 但 `raw_value` 留空 | 补 `raw_value` |
| `IMPRESSION_IN_VIEW` | `metric_code=VIEW_COUNT` 但 `note` 或 `raw_value` 出现「曝光」 | 改用 `IMPRESSION_COUNT`，或按实测口径登记并注明 |
| `SCOPE_UNKNOWN` | `metric_scope=UNKNOWN` | 向平台/运营确认窗口后回填 |
| `CLICK_SCOPE_MISSING` | `LINK_CLICK_PV/UV` 的 scope 为 `UNKNOWN`/`NOT_APPLICABLE` | 点击类必须写明窗口 |
| `VALUE_ON_MISSING` | 非 `ACQUIRED`/`PENDING` 却有 `value` | 清空 `value` |
| `TEXT_METRIC_HAS_VALUE` | 文本类指标填了 `value` | 内容移到 `raw_value` |
| `NUMERIC_METRIC_NO_VALUE` | 数值类指标 `ACQUIRED` 但 `value` 空 | 补 `value` |
| `RAW_MAGNITUDE_MISMATCH` | `raw_value` 含「万/千/k」但 `value` 量级不符 | 检查换算 |
| `AUTO_WITHOUT_EVIDENCE` | `source_type=AUTO_API` 但 `evidence_ref` 空 | 补实测证据引用（G0-11 要求） |
| `DUPLICATE_ROW` | 同 `publication_key` + 同 `metric_code` + 同 `observed_at` 重复 | 删除重复行 |
| `FUTURE_OBSERVED_AT` | 观测时间在将来 | 修正时间 |

> 完整规则集与自检用例见 `tools/m0/wp03_data/validate_import.py`（`--selftest`）。