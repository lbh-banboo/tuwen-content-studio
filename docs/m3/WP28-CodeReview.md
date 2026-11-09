# WP28 阶段 4.5 Code Review 记录

- 审查日期：2026-10-16
- 范围：WP28 Live 服务接入与其 WP27 回归修复；不含 WP35/M4 并行变更
- 审查对象：`media/service.py`、`media/repository.py`、`media/billing_bridge.py`、`media/live_pack.py`、`media/handlers.py`、`media/types.py`、`media/schemas.py`、`test_media_wp27.py`、`test_media_wp28_service.py` 与本说明文档
- 结论：**未发现 CRITICAL / MAJOR；本轮发现并修复 1 条文档性误导（常量来源注释）。**

## 第一遍：结构与职责

- Live 形态由 `request.media_form` 单点驱动；发起期物化 1 个 Live 身份行、静态仍为 3 行，并在共用 `sort_order` 空间冲突时给出业务拒绝。
- Live 执行依赖 `motion_generator` 显式注入；服务层在提示词、计价和预占之前 fail-closed，不静默回落到桩。
- 服务层负责预算、出网编排、资产存储与版本事务；`LivePackStub` 保持纯本地封装，组件通过 repository 写入，职责边界与既有拆分一致。
- 4.5 新增测试文件以真 PostgreSQL、真实 repository / file storage / version repository 验证执行链，计费与提示词/provider 等外部接缝采用可观察替身。

## 第二遍：逐项实现审查

| 优先级 | 发现 | 处置 |
|---|---|---|
| [MINOR] | `live_pack.py` 的 `LIVE_GROUP_MAX_BYTES_DEFAULT` 注释仍声称运行期读取 `runtime_settings`，但实现是常量缺省 + service/handler 注入覆盖，与说明文档不一致 | **已修复**为准确说明；该值不属于 `runtime_settings` 白名单 |
| [MINOR] | `_execute_locked` 与测试支持函数较长；简化分析工具报出长函数建议，但它也将超大模块中的多个既有长函数列为候选，无法区分本轮增量；盲目拆分会扩大服务编排改动面 | 本轮刻意不做结构重构；后续重构应单独设计、逐函数行为验证 |
| [NIT] | 测试 helper `_run` 为直接服务调用模拟任务处理器事务边界：业务事务异常回滚后，另开事务将媒体置为 FAILED，再重抛 | 与生产 handler 把业务失败折成 `HandlerOutcome` 的事务语义相容；测试注释应避免被理解成“服务异常事务本身会持久化 FAILED”。当前 helper 已明确独立事务边界，保持不动 |

未发现将用户输入拼入 SQL 的新增路径；组件查询使用 ORM，pending 清空明确写 SQL `NULL`；本次人工核对的错误路径均 fail-closed。未对并行工作树进行代码审查或修改。

## 第三遍：边界与回滚

- 形态冲突双向测试覆盖静态→Live 与 Live→静态，避免 DB 唯一约束成为用户可见错误。
- 缺 `motion_generator` 测试断言估价和预占均未发生。
- Live 计费测试断言 `form=LIVE`、`content_count=1`，由 `calls_for` 折成 2 张；预占与结算都带同一 OP-206 note。静态 note 仍为 `None`。
- 成功路径核对 4 组件、`SYNTHETIC` 标记、STILL 文件 id 与版本 `image_file_id` 一致，且详情能读回形态和组件。
- 超限路径核对版本、组件、生成上下文均无半成品；媒体失败状态持久化、预占释放、无结算。
- JSONB 缺陷回归测试先设置有效绑定，再清空并查询 `IS NULL`，能区分 SQL `NULL` 与 JSON `null`。
- 体积阈值的独立封装层已有超限拒绝前不写入及边界测试；服务层端到端另测整组失败与补偿。

## 关键变异验证

本轮执行两项定向变异验证（变异期间仅短暂替换共享工作树中的目标实现文件；每项测试后立即从内存备份恢复，随后再次核对原文件字节内容并重跑测试）：

| 变异 | 对应判据 | 结果 |
|---|---|---|
| 将封装体积拒绝条件 `total > max_group_bytes` 变异为永不超限 | `test_group_too_large_fails_before_any_write` | 预期失败，确认用例对阈值逻辑敏感 |
| 将账本共用的 OP-206 note 变异为空 | `test_live_reserve_and_settle_both_carry_op206_note` | 预期失败，确认 reserve/settle 标注缺失会被抓出 |

注：这些是定向变异，不替代 4.4 已记录的 6 项封装桩变异，也不声称覆盖所有 Live 分支。STILL 复用、形态冲突、缺动态段守卫与详情读取由专项真库行为断言直接覆盖；本轮不对其逐项做源码变异。

## 已核对但无问题

- 账本数量口径集中在 `calls_for` / `estimate_generation_cost`，服务不重复乘张数。
- `note_for_form` 保持静态 note 原样，对 Live 追加 OP-206 说明。
- Live 组件插入在版本行之后，符合 `media_components.media_version_id` 即时 FK。
- STILL 直接复用正式静态资产 id，没有重复创建 `file_objects`。
- Live 详情仅对 LIVE 版本读取组件；静态路径不增加组件查询。
- 旧请求缺 `mediaForm` 时兼容 STATIC，未知形态拒绝。
- 失败时 service savepoint 回滚版本、组件和上下文写入；handler 的事务结果折算与账本释放仍走既有边界。
- `insert_generation_context` 使用 repository 契约参数 `content_item_id`。

## 测试证据

执行环境：`NO_PROXY='*'`、`PYTHONPATH=src`、私有库 `rednote_test_wp28`。

```text
133 passed in 4.98s
```

覆盖：`test_media_wp28_service.py`、`test_media_wp27.py`、`test_media_wp28_db.py`、`test_media_wp28_live_pack.py`、`test_media_wp28.py`。

针对本轮受审路径的 `git diff --check` 通过。测试命令必须在 `services/backend` 工作目录运行；仓库根目录执行同一相对测试路径会找不到文件，已纠正并在后端目录成功重跑。

## 未纳入本轮的事项

- 未跑全量 pytest（共享默认库与并行会话状态需另行确认）。
- 未生成 WP28 证据采集脚本；属于阶段 4.7。
- 未提交更改；共享工作树含并行 WP35/M4 文件，后续提交须使用显式路径并再次核对归属。
- `live_group_max_bytes` 当前为 20 MiB 常量缺省 + 注入覆盖，不从 `runtime_settings` 读取；文档已记录，上线前需按 OP-76 重裁配置来源。
