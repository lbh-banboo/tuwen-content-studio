# 回归护栏登记册（Regression Register）

> 版本 v1.7 · 2026-10-18 · 维护方：主 agent（共享文件，分支禁写）
> **机械对账**：`python tools/regression/check_register.py`（CI 门 3 必跑；退出码非 0 即阻断）

## 0. 这张表是干什么的

每修一个 bug，就留一条「先红后绿」的测试钉住它的根因。这张表把这些测试**集中登记**，
回答三个问题：**哪个根因** 被 **哪条测试** 钉住、**它现在还在不在**。

它不是自由文本 —— `check_register.py` 逐行做 4 条判据（ISS-95 教训：文档说「有判据」，
就必须能当场指出脚本里的哪个函数）：

| 判据 | 内容 | 对应函数 |
|---|---|---|
| R1 | 每行的 `文件::函数` 在代码里真实存在 | `main()` → `key not in code` |
| R2 | 每行声明的标记 == 函数上的 `@pytest.mark.<x>` | `main()` → `r["mark"] not in code[key]` |
| R3 | 代码里每个带 `regression`/`cross_wp` 的函数都有一行（无孤儿） | `main()` → R3 循环 |
| R4 | 来源列的 ISS 编号在 `issue-log.jsonl` 存在；无 ISS 的必须写 `ADHOC` | `main()` → R4 |

## 1. 标记体系

| 标记 | 含义 | 跑在哪道门 |
|---|---|---|
| `smoke` | 3 分钟内的最小关键路径（`test_health.py` + `test_contract.py` 整文件，模块级 `pytestmark`） | 门 1：每次 push |
| `regression` | 修过 bug 后的保护测试，防同一根因复现 | 门 2 随全量跑；门 3 显式复跑 |
| `cross_wp` | 新包不得破坏旧包的共享触发器 / 约束 / 受控取值 | 门 2 随全量跑；门 3 显式复跑 |
| `slow` | 需大量数据或长等待（**当前 0 条**，门 2 排除、门 3 启用） | 门 3 |

`smoke` 是文件级标记，不逐行登记（R3 只查 `regression`/`cross_wp`）。

## 2. 准入规则（什么时候必须加一行）

1. **issue-log 里 `status=CLOSED` 且 `resolution` 提到了测试** → 该测试必须带 `regression` 并登记。
2. **Code Review 的 CRITICAL/MAJOR 修复** → 写「先红后绿」测试，带 `regression`，来源写 `ADHOC(<包>-CR-MAJOR-n)`。
3. **往共享表（`version_nodes` / `audit_logs` / `outbox`）挂触发器、扩受控取值** → 至少一条「别家不受影响」的 `cross_wp`。
4. **禁止**把「全项目恰好 N 个」写成回归判据（铁律 1）；`test_router_exposes_exactly_*` 只断言本包自己的路由数，是允许形态。
5. 测试被**改名或删除**时同一提交里改本表，否则 R1/R3 在 CI 门 3 直接红。

## 3. 登记明细

### 3.1 `regression`（201 条）

| 编号 | 来源 | 标记 | 文件 | 函数 | 钉住的根因 |
|---|---|---|---|---|---|
| RG-001 | ADHOC(M1 契约初版) | regression | `test_contract.py` | `test_schema_keeps_required_openapi_fields` | 契约规范化剥掉了 `info.title` |
| RG-002 | ISS-15, ISS-21 | regression | `test_contract.py` | `test_no_unused_framework_error_schemas` | FastAPI 默认 `HTTPValidationError` 混入契约 |
| RG-003 | ISS-22 | regression | `test_projects_wp10.py` | `test_add_member_duplicate_keeps_transaction_usable` | 捕获 IntegrityError 后未回滚 SAVEPOINT 就回查 → 500 |
| RG-004 | ADHOC(WP10-CR) | regression | `test_projects_wp10.py` | `test_assignment_to_existing_collaborator_promotes_membership` | 交接给已有协作者撞唯一约束 |
| RG-005 | ADHOC(WP10-CR) | regression | `test_projects_wp10.py` | `test_project_list_cursor_pagination_is_complete_and_unique` | 游标翻页重/漏 |
| RG-006 | ADHOC(WP08 双防线) | regression | `test_projects_wp10.py` | `test_create_project_requires_csrf` | 写请求缺 CSRF 头未被拒 |
| RG-007 | ISS-26 | regression | `test_versions_wp11.py` | `test_register_version_is_idempotent_for_identical_payload` | 幂等注册未传显式修订号，新增一版被误当幂等命中 |
| RG-008 | ISS-26 | regression | `test_versions_wp11.py` | `test_register_version_without_revision_is_not_idempotent` | 同上（反向判据） |
| RG-009 | ADHOC(WP11-CR) | regression | `test_versions_wp11.py` | `test_reading_a_node_without_changing_it_is_allowed` | 只读触发了不可变守卫 |
| RG-010 | ISS-27 | regression | `test_versions_wp11.py` | `test_cycle_detection_terminates_on_cyclic_graph` | 环检测 CTE 未去重：指数展开 + 漏判深层环 |
| RG-011 | ISS-31 | regression | `test_files_wp12.py` | `test_completed_upload_leaves_no_temporary_parts` | 完成上传后临时分片永久残留 |
| RG-012 | ISS-92 | regression | `test_files_wp12.py` | `test_expire_stale_sessions_releases_reservation` | 全局扫描 + 默认 limit=200 把本用例挤出窗口 |
| RG-013 | ISS-92 | regression | `test_files_wp12.py` | `test_expire_is_idempotent` | 同上（跨运行残留推高全局计数） |
| RG-014 | ISS-49 | regression | `test_jobs_wp13.py` | `test_task_survives_queue_outage` | 依赖全局队列 + limit=100，被后续包挤出窗口 |
| RG-015 | ISS-59 | regression | `test_jobs_wp13.py` | `test_second_claim_gets_higher_fence` | 自留过期租约占满全局回收 limit |
| RG-016 | ISS-36 | regression | `test_jobs_wp13.py` | `test_cancel_request_then_confirm` | finish_job 白名单漏 CANCEL_REQUESTED，取消永远无法确认 |
| RG-017 | ISS-50, ISS-59 | regression | `test_jobs_wp13.py` | `test_waiting_provider_job_survives_lease_expiry` | helper 领取不结束，留 RUNNING+租约跨运行污染 |
| RG-018 | ISS-35 | regression | `test_jobs_wp13.py` | `test_idempotency_new_then_replay` | 幂等首次调用被误判为重放，静默不执行 |
| RG-019 | ISS-34 | regression | `test_jobs_wp13.py` | `test_renew_lease_fails_after_losing_ownership` | 回收未递增 fence_token，失联 Worker 回来仍能续租 |
| RG-020 | ISS-41 | regression | `test_events_wp14.py` | `test_notifier_payload_is_bare_sequence` | 通知 payload 形态 / 证据脚本相对路径缺陷 |
| RG-021 | ISS-46 | regression | `test_gateway_wp16.py` | `test_reconcile_resolves_unknown_and_keeps_reason` | status_unknown_reason 的 CHECK 与核对语义冲突 |
| RG-022 | ISS-46 | regression | `test_gateway_wp16.py` | `test_status_unknown_reason_allowed_on_terminal_state` | 同上（终态允许保留原因） |
| RG-023 | ISS-73 | regression | `test_gateway_wp16.py` | `test_list_due_polls_only_returns_due_calls` | 共享库全局扫描 + 小 limit，数据量依赖型偶发红 |
| RG-024 | ISS-51 | regression | `test_parsing_wp17.py` | `test_parsed_units_is_units_not_segments` | 解析单元数与片段数混用 |
| RG-025 | ISS-51 | regression | `test_parsing_wp17.py` | `test_file_error_forces_zero_parsed_units` | 同上（错误时单元数必须为 0） |
| RG-026 | ISS-51 | regression | `test_parsing_wp17.py` | `test_image_with_ocr_engine_produces_located_segments` | 同上（OCR 路径） |
| RG-027 | ISS-52, ISS-53 | regression | `test_parsing_wp17.py` | `test_execute_parse_persists_segments_and_state` | 片段顺序靠 created_at；裸 SQL 后读回陈旧 ORM 副本 |
| RG-028 | ISS-77 | regression | `test_research_wp19.py` | `test_verification_prompt_media_depends_on_category` | format_media_for_category 死代码，media 恒 ANY |
| RG-029 | ISS-76 | regression | `test_research_wp19.py` | `test_quota_bytes_are_full_scope_not_filtered` | 配额从已筛选行算出，reconciled 假报 False |
| RG-030 | ADHOC(WP19-CR) | regression | `test_research_wp19.py` | `test_files_endpoint_invalid_status_is_422` | 非法 status 被静默忽略 |
| RG-031 | ISS-69, ISS-83 | regression | `test_knowledge_wp20.py` | `test_router_exposes_exactly_nine_endpoints` | router 重复挂载 9→18；全项目等式被后续包打红 |
| RG-032 | ISS-89 | regression | `test_knowledge_wp21.py` | `test_switch_profile_never_leaves_no_active` | switch_profile 先降旧后升新，与 docstring 相反 |
| RG-033 | ADHOC(WP22-CR-MAJOR-3) | regression | `test_baseline_wp22.py` | `test_partial_update_preserves_brief_version_id` | 局部更新清空 brief_version_id |
| RG-034 | ADHOC(WP22-CR-MAJOR-5) | regression | `test_baseline_wp22.py` | `test_write_endpoints_survive_request_id` | 写端点带 request_id 时失败 |
| RG-035 | ADHOC(WP22-CR-MAJOR-1) | regression | `test_baseline_wp22.py` | `test_create_baseline_rejects_non_brief_version_node` | 基线可挂到非 BRIEF 版本节点 |
| RG-036 | ADHOC(WP22-CR-MAJOR-4) | regression | `test_baseline_wp22.py` | `test_rebuild_active_baseline_matches_incremental_path` | 重建路径与增量路径结果不一致 |
| RG-037 | ADHOC(WP22-CR-MAJOR-6) | regression | `test_baseline_wp22.py` | `test_approve_step_rejects_duplicate_after_supersede` | 被取代后可重复审批 |
| RG-038 | ISS-82 | regression | `test_prompts_wp23.py` | `test_write_activation_events_rereads_on_retry` | 「重试重读」判据恒真，需可控替身造出那一刻 |
| RG-039 | ISS-83 | regression | `test_prompts_wp23.py` | `test_router_exposes_exactly_six_endpoints` | 只断言本包路由数（铁律 1 允许形态） |
| RG-040 | ISS-81 | regression | `test_prompts_wp23.py` | `test_list_prompts_pagination_covers_every_row` | 内存分页截断，翻页静默丢尾部 |
| RG-041 | ISS-80 | regression | `test_prompts_wp23.py` | `test_create_prompt_rejects_tools_in_body` | 请求体缺 extra=forbid，allowedTools 静默忽略 |
| RG-042 | ISS-80 | regression | `test_prompts_wp23.py` | `test_add_version_rejects_tools_in_body` | 同上（加版本越权面） |

| RG-043 | ISS-107 | regression | `test_schema_parity.py` | `test_all_fk_deferrability_matches_between_model_and_db` | 模型漏写 `deferrable` → 夹具把迁移的延迟外键「治愈」成非延迟 |
| RG-044 | ISS-107 | regression | `test_schema_parity.py` | `test_deferrable_fk_count_is_stable` | 漏 import 一个模块 → 覆盖范围静默缩水（下界断言本身也要能红） |
| RG-045 | ISS-106 | regression | `test_schema_parity.py` | `test_cross_package_fk_helper_roundtrip_is_identity` | 夹具 `DROP ... CASCADE` 摘除跨包外键；核对助手不得有副作用 |
| RG-046 | ADHOC(WP24 实现期自测·余数口径) | regression | `test_planning_wp24.py` | `test_largest_remainder_regression_300_250_200_150_60` | 余数曾写成 `weight % 1000` —— 权重恒 < 1000 ⇒ 该式**恒等于权重本身**，「按余数降序」静默退化成「按权重降序」。两种口径在此样本上**合计都是 10** ⇒ 任何「和等于 total」的不变量都检测不出，必须逐条点名 |
| RG-047 | ISS-137 | regression | `test_planning_wp24.py` | `test_allocation_invariant_holds_exhaustively` | 3000+ 组穷举的核心不变量是**代数恒等式**（`sum(x) + (total - sum(x)) == total`，两端是同一 dict 的两种投影）⇒ 永不报红；改为与测试侧独立实现 `_expected_largest_remainder` 逐项互证 |
| RG-048 | ADHOC(WP24-CR-错误⑫ 配套) | regression | `test_planning_wp24.py` | `test_runtime_settings_rejects_update_and_delete` | `runtime_settings` 的追加式不可变性是 ADR-13「审计不可篡改」的**唯一实施点**，而服务层没有任何 UPDATE / DELETE 路径 ⇒ 触发器被删不会让任何服务层用例变红（消失是静默的），只能用原生 SQL 钉住 |
| RG-049 | ADHOC(WP24 部分唯一索引·全局) | regression | `test_planning_wp24.py` | `test_partial_unique_index_global_rejects_duplicate_revision` | 全局部分唯一索引是「当前生效值」的结构基础；没有它同一 `(key, 1)` 可有任意多行，「哪条是当前值」只能靠 `max(revision_no)` 猜且平局不报错。先插「同 key 不同修订号必须成功」是为了让**报红原因**指向索引本身 |
| RG-050 | ADHOC(WP24 部分唯一索引·项目) | regression | `test_planning_wp24.py` | `test_partial_unique_index_project_rejects_duplicate_revision` | 第一版**只插一条就断言「第二条会被拒」**（第二条根本没插）⇒ 用例通过但什么都没验证；现在显式插两次 + 补「不同项目同修订号必须成功」，让「键里含 `project_id`」成为被验证的事实 |
| RG-051 | ISS-83 | regression | `test_planning_wp24.py` | `test_router_exposes_exactly_eight_endpoints` | 只断言本包 router 自己的操作数（`router.routes`），不断言全项目路径数 —— 铁律 1 / ISS-83 的**允许形态**（与 RG-039 同形） |
| RG-052 | ISS-145 | regression | `test_test_database_target.py` | `test_tool_reports_red_when_a_default_dsn_points_at_system_database` | 判据 A 的区分力：合规 DSN 改回系统库后必须报红**并指名库名/文件名**。只断言「退出码非 0」不够 —— 脚本因任何原因崩溃也会非 0，「崩溃」与「抓到违规」在退出码上同形（ISS-143） |
| RG-053 | ISS-145 | regression | `test_test_database_target.py` | `test_tool_passes_on_the_current_tree` | 当前真树必须绿 —— 否则 RG-052 的「改后红」无法归因于变异 |
| RG-054 | ISS-145 | regression | `test_test_database_target.py` | `test_static_reserved_databases_covers_all_pg_templates` | `STATIC_RESERVED_DATABASES` 是人写名单（判据刻意纯静态、不连库）⇒ 按铁律 14 必须另有一条判据对账真库的 `datistemplate`。方向为单向包含（多无害、少则漏判） |
| RG-055 | ISS-145 | regression | `test_test_database_target.py` | `test_database_name_parsing` | `database_name` 是判据 A 的**输入**：各种合法 DSN 形态（带 driver / 带 query / 无库名 / 尾斜杠）切出的库名错了，后面的合规判定全错 —— ISS-118 同族：「判据读到的值凭什么是现在的/对的？」 |
| RG-056 | ISS-145 | regression | `test_test_database_target.py` | `test_dsn_literal_detection_requires_scheme_separator` | 只比对前缀 `postgresql+` 会把**驱动名** `"postgresql+psycopg"`（不指向任何库）当成 DSN ⇒ 解析不出库名 ⇒ 该 DSN 被判「不适用」⇒ 覆盖数虚高 |
| RG-057 | ISS-145 | regression | `test_test_database_target.py` | `test_os_walk_counter_matches_rglob_scope` | 覆盖范围由 `TESTS_DIR.rglob("*.py")` 决定。若被写成 `glob`（**只差一个字**）递归消失、扫描缩水，判据照样 `[PASS]` —— 铁律 14 第 5 次形态。本条用 `os.walk`（另一套遍历机制）独立数一遍 |
| RG-058 | ISS-147 | regression | `test_test_residue.py` | `test_default_pytest_args_keeps_m_expression_as_one_argv_element` | `-m "not slow"` 被 `.split()` 拆成 `['-m','"not','slow"']` → pytest 拿 `slow` 当路径 → `no tests ran`（退出码 4）→ **「没跑」伪装成「跑过且无残留」** |
| RG-059 | ISS-147 | regression | `test_test_residue.py` | `test_parse_pytest_args_respects_shell_quoting` | 与 RG-058 互补：查「用环境变量覆盖参数」那条路径也必须按 shell 语义解析 —— 任一拆错都会让「没跑」变「假绿」 |
| RG-060 | ISS-147 | regression | `test_test_residue.py` | `test_parse_pytest_counts` | `_parse_pytest_counts` 是「测试真跑过」这条前置条件的**输入**；`no tests ran` 必须解析成 `(0,0)`，不许「乐观地当成跑了 1 条」。与 RG-055 同一纪律：判据的输入自己也得被判 |
| RG-061 | ISS-147 | regression | `test_test_residue.py` | `test_main_guards_against_zero_collected_tests` | `main()` 里「收集数为 0 → 直接 FAIL」的拦截必须存在。用 **AST** 而非文本查找：判据自己的 docstring 里就写着 `collected == 0` 这个式子，文本查找会把解释文字当实现（本项目已现身 6 次的形态） |
| RG-062 | ISS-147 | regression | `test_test_residue.py` | `test_main_fails_when_pytest_itself_fails` | pytest 自己红了时差分「跑在错的底座上」，结论不可信 —— `main()` 必须拦截 `proc.returncode` 非零 |
| RG-063 | ISS-147 | regression | `test_test_residue.py` | `test_diff_counts_only_additions` | `_diff` 只报**新增**对象；被删的对象是清理、不算残留。快照缺类别时 `KeyError` 是**有意**的：静默跳过会让「没观察这一类」与「这一类没变化」同形（ISS-143 族） |
| RG-064 | ISS-147 | regression | `test_test_residue.py` | `test_diff_requires_every_snapshot_category` | 缺类别时必须**抛错**。若改成 `after.get(kind, set())`，「少观察一类」就与「该类无变化」同形 —— 判据静默缩小观察面照样 `[PASS]`（ISS-143 / ISS-137 族） |
| RG-065 | ISS-147 | regression | `test_test_residue.py` | `test_diff_is_empty_when_structure_is_unchanged` | 结构不变时差分必须为空 —— 否则判据在干净库上恒红 |
| RG-066 | ISS-147 | regression | `test_test_residue.py` | `test_snapshot_queries_are_read_only` | 快照 SQL 必须纯 `select`。判据若自己建/删对象就会**污染基线**（前后快照都被自己改过），差分失去意义 —— 判据成了被测对象的干扰源，比不判更糟 |
| RG-067 | ISS-147 | regression | `test_test_residue.py` | `test_snapshot_covers_the_object_kinds_that_actually_residue` | 快照须覆盖本仓**实测**残留形态（`index` 是主力：8 个 HNSW；`table` 是 §4.2-B 那 74,145 行），且索引/触发器/约束名须拼上表名，否则报告里只有一串无法定位归属的索引名 |
| RG-068 | ISS-147 | regression | `test_test_residue.py` | `test_no_hardcoded_residue_object_names_outside_docstrings` | 判定逻辑里不许写死索引名/表名名单（铁律 14）。实测残留 `ix_chunk_embeddings_hnsw_<profile_uuid>` 名字里**没有任何**测试特征词 —— 退回「查名字名单」会静默漏判。⚠️ 必须剥掉 docstring 再查（模块 docstring 里**故意**写了该名字作反例） |
| RG-069 | ISS-147 | regression | `test_test_residue.py` | `test_classify_only_affects_the_message_not_the_verdict` | `_classify` 的名字启发式只许影响**提示文案**。若被接进判定路径（`if _classify(...)` 决定报不报），判据就退化成「查名字名单」，无特征词的残留被静默放过 |
| RG-070 | ISS-145 | regression | `test_test_residue.py` | `test_default_dsn_does_not_point_at_a_reserved_database` | 判据 B 自己默认要连的库也不许是 `postgres`/`template*` —— 脚本自己犯它要抓的错（ISS-145 一族的自查）。用**判据 A 的解析函数**而非另写一份，避免两份解析漂移成自证 |
| RG-071 | ISS-145 | regression | `test_test_residue.py` | `test_env_override_is_respected_for_the_target_database` | 目标库必须可被 `REDNOTE_TEST_DATABASE_URL` 覆盖 —— 并行会话各自物理隔离的前提（§4.2-D） |
| RG-072 | ISS-146 | regression | `test_migration_model_parity.py` | `test_derived_bookkeeping_name_exists_in_the_real_database` | 三级派生出的记账表名必须**真的存在于迁移建过的库里**：派生逻辑写得多漂亮都不算数，只有磁盘上有那张表才证明派生依据与实现没脱节。方向单向（不断言「真库里只有它一张记账表」，反向由排除集自检承担） |
| RG-073 | ISS-146 | regression | `test_migration_model_parity.py` | `test_bookkeeping_table_is_scanned_from_disk_not_hardcoded` | `_migration_bookkeeping_tables` 源码里不许出现具体表名字面量（铁律 14 机械护栏）。人看得懂「别写死」，但下次改这个函数仍会顺手写 `"alembic_version"` —— 因为它最省事且当下看起来对 |
| RG-074 | ISS-146 | regression | `test_migration_model_parity.py` | `test_ann_access_methods_are_real_postgres_access_methods` | 判据**编一个不存在的 access method** 是它最隐蔽的失效方式：名单里写 `"hnsw_ann"` 则永远不排除任何东西（静默恒真，ISS-115 亲戚）。本脚本初版正是写了磁盘上不存在的索引名（铁律 14 第 7 次） |
| RG-075 | ISS-146 | regression | `test_migration_model_parity.py` | `test_no_hardcoded_index_names_in_the_tool` | 判据源码里不许出现 `ix_...` 具体索引名字面量。排除集必须按 **access method** 判：真索引名由 `embedding_ddl.hnsw_index_name(profile_id)` 运行期拼出（带 profile uuid 后缀），静态名字写不对 |
| RG-076 | ISS-146 | regression | `test_migration_model_parity.py` | `test_without_bookkeeping_removes_all_four_object_kinds` | 记账表参与的四类对象都要剔掉、其他表一个不许碰。只剔 `tables` 而漏 `column` 时，diff 会把记账表的 `version_num` 报成「模型漏了」—— 真差异被淹在假红里 |
| RG-077 | ISS-146 | regression | `test_migration_model_parity.py` | `test_without_bookkeeping_is_identity_when_nothing_to_exclude` | 排除集为空时必须恒等 —— 否则「没东西可排」这条路径完全没被覆盖 |
| RG-078 | ISS-146 | regression | `test_migration_model_parity.py` | `test_derivation_raises_when_no_source_is_readable` | 三个来源都读不到时必须**抛异常**而非返回空集：「没跑通」与「跑过且无排除项」在返回值上同形（ISS-143），返回空集意味着排除逻辑被静默旁路。抛异常是唯一让它当场可见的行为 |
| RG-079 | ISS-145 | regression | `test_migration_model_parity.py` | `test_base_dsn_default_does_not_point_at_a_system_database` | 判据 C 自带默认 DSN 也不许指向 `postgres`：它会**新建/删除**库，基准 DSN 指向 `postgres` 则管理连接落在系统库上 —— 与 ISS-145 同类，脚本自己犯它要抓的错 |
| RG-080 | ISS-158 | regression | `test_test_db_isolation.py` | `test_hardcoded_dsn_still_reports_red` | 豁免扩到四类位置（`replace` / `or` 链 / `assert` / `parametrize` / `environ[…]` 赋值）后，真硬编码必须**仍然**报红。豁免天生只会把红变绿，这条是 ISS-158 修复的护栏（阴性对照） |
| RG-081 | ISS-158 | regression | `test_test_db_isolation.py` | `test_compliant_forms_are_accepted` | 七种真实合规写法必须 0 处报红。21 处假阳性就是这么来的：判据只认①②形态，同义的第三四种被判成硬编码，于是判据开始惩罚唯一在测隔离的代码（阳性对照） |
| RG-082 | ISS-158 | regression | `test_test_db_isolation.py` | `test_exemption_is_by_node_identity_not_by_line` | 豁免必须精确到**节点**：同文件里豁免形态与真硬编码并存时只报后者。改用行号豁免会让第 1 行的豁免顺带放过第 2 行的真硬编码（"顺手放过"的经典形态） |
| RG-083 | ISS-158 | regression | `test_test_db_isolation.py` | `test_env_name_set_actually_drives_the_verdict` | 同一源码在测试口径（只认 TEST 变量）报红、工具口径（两个都认）通过 —— 把"`env_names` 参数真的被用到了"从阅读代码变成可复跑判据 |
| RG-084 | ISS-158 | regression | `test_test_db_isolation.py` | `test_this_file_itself_is_clean` | 本测试文件自己必须 0 违规（它在判据第一段的扫描范围内）。钉住"样本一律用 `_PG + …` 拼接"这条约束，防止将来有人直接写字面量、进而把整个文件塞进 `ALLOW_FILES` 变成法外之地 |
| RG-085 | ISS-158 | regression | `test_test_db_isolation.py` | `test_ci_referenced_scripts_scope_is_derived_and_nonempty` | 第二段覆盖范围必须现场从 `ci.yml` 派生且非空（下界 ≥10）。`_ci_referenced_scripts()` 若因 ci.yml 改格式而静默返回空集，第二段就变成"扫 0 个脚本"——与"全部通过"在计数上同形（ISS-143） |
| RG-086 | ISS-165 | regression | `test_test_database_target.py` | `test_mutation_stays_inside_the_copy` | ISS-165 的反向验证：变异-还原测试必须把变异做在 `tmp_path` **副本**里，**真树只读**。断言口径含 `mtime_ns` 而非仅内容 —— 此缺陷的病灶**恰恰是"内容没变而 mtime 变了"**（`write_text` 必然刷新 mtime），只比内容的话它会通过自己的反向验证（ISS-115：判据对着真实失效面恒真）。⚠️ 漏登记一次：收口第 11 步第一次真正跑通时被 R3 当场点名（"带 ['regression'] 但登记册无此行"）|
| RG-087 | ISS-83, ISS-172 | regression | `test_billing_wp25.py` | `test_router_exposes_exactly_fourteen_endpoints` | 只断言本包 router 声明的操作数（恰好 14），**不断言全项目路径数** —— 铁律 1 / ISS-83 的**允许形态**（与 RG-039 / RG-051 同形）。多一个少一个都会让 `api.py` docstring 那张 14 行表失去可核对性，而那张表是本包对协议的唯一形状声明。⚠️ 漏登记一次：WP25 收口第 11 步复跑时被 R3 点名（"带 ['regression'] 但登记册无此行"）—— 与 RG-086 同一提交范围内的**同型第二次**，见 ISS-172 |

| RG-088 | ISS-79 | regression | `test_migration_roundtrip.py` | `test_matcher_selfcheck_passes_on_the_real_repository` | 六格对照在**真仓库**必须全过：A 格直接读 WP11/WP15 两个真实迁移。迁移被改回旧写法、或 docstring 引文被当成真代码，这条立刻红 |
| RG-089 | ISS-79 | regression | `test_migration_roundtrip.py` | `test_matcher_flags_the_original_buggy_form` | 阳性对照：`{item[4] for item in all_trigger_ddl()}` 必须被报违规。认不出真缺陷 ⇒ 判据恒真 ⇒ 连它存在的意义都没有（ISS-115）。写法定格自历史提交，不是编的 |
| RG-090 | ISS-79 | regression | `test_migration_roundtrip.py` | `test_matcher_ignores_the_same_text_quoted_in_a_docstring` | 关键格：旧写法**只**出现在 docstring 里（WP11/WP15 逐字引用了"原写法（已废止）"）⇒ 不许报红。文本正则路线会被自己的文档骗到、制造假红淹没真红。**这也是本工具选纯 AST 的唯一理由**（`ast.parse` 让 docstring 成为不下钻的 `Constant` 节点） |
| RG-091 | ISS-79 | regression | `test_migration_roundtrip.py` | `test_matcher_covers_unpack_and_alias_forms` | 五元组解包 / 先赋值再迭代两种**语义等价**写法必须被认出 —— 只认推导式的话，重构一下就能绕过判据而语义完全没变（铁律 14 的经典形态） |
| RG-092 | ISS-79 | regression | `test_migration_roundtrip.py` | `test_matcher_allows_the_correct_form` | 阴性对照：正确实现（第 2 步迭代 `consistency_items()`）不许误报。误伤会逼实现者去"绕判据"而不是修代码（本仓假红前科 ISS-158 报了 21 处假阳性） |
| RG-093 | ISS-79 | regression | `test_migration_roundtrip.py` | `test_shared_function_name_is_derived_not_hardcoded` | 共用函数名必须从 `rednote.platform.immutability::TRIGGER_FUNCTION` 读（单一事实来源）。写死则改名后"共用函数存活"这条断言变成**在检查一个不存在的对象**，"没通过"与"对象根本不在"同形（铁律 14） |
| RG-094 | ISS-79 | regression | `test_migration_roundtrip.py` | `test_shared_function_is_a_real_function_in_the_database` | 派生值 vs 真值对账：磁盘派生出的共用函数名必须**真的存在于迁移建过的库**（`pg_proc`）。派生逻辑写得多漂亮都不算数 —— 与 RG-072 同款。连不上库则按设计 skip（写明理由，不静默通过） |
| RG-095 | ISS-79 | regression | `test_migration_roundtrip.py` | `test_scan_range_is_disk_derived_and_nonempty` | 扫描范围必须现场从 `migrations/versions/*.py` glob 派生且 ≥18（**下界**，迁移数会随开发增长，写死必失效）。"该查哪些迁移"若由人写名单，新迁移会静默不在覆盖内（铁律 14） |
| RG-096 | ISS-79 | regression | `test_migration_roundtrip.py` | `test_expected_checks_constant_is_declared` | `main()` 用 `len(checks) >= EXPECTED_CHECKS` 判完整性 —— 是 `>=` 语义，**只拦变少、不拦变多**；加判据时忘了同步常量不会报红（ISS-153 原始形态）。本条把"常量存在且 ≥ 静态 4 条"钉住 |
| RG-097 | ISS-169 | regression | `test_migration_roundtrip.py` | `test_guardrail_test_references_resolve_to_real_files` | 注释点名的 `test_*.py` 必须**真实存在**：修 ISS-79 时人工发现 WP11/WP15 承诺的 `test_ddl_downgrade_ownership.py` 全仓不存在 —— **文档声称有防线、入口却打不开**（ISS-95/169 第 3 次复发）。口径 = `tool.GUARDRAIL_SCAN_ROOTS`（`migrations/` + `services/backend/src/`，**含 `__init__.py`**；刻意**不含** `tools/`、`tests/` —— 那两处自己就要写幽灵名当反例与阳性对照，收进来等于判据判自己）。第 4 次复发即 `src/` 侧 4 处（`media/models.py`、`media/schemas.py` ×2、`devtools/__init__.py`），实测扩口径**零假阳性**（`src` 17 处引用 + `migrations` 6 处，共 23）。非空性守卫**逐根**判（每根 ≥1 处引用），只判合计时"某一根源失效"与"一切正常"同形（ISS-143） |
| RG-098 | ISS-169 | regression | `test_migration_roundtrip.py` | `test_guardrail_scanner_flags_a_dangling_reference` | 阳性对照：扫描器必须能认出**不存在的**测试文件名。只断言"真树无悬空"是恒真风险 —— 扫描器正则写错（永不匹配）照样通过。喂一个必然不存在的名字验证区分力，并反向确认真实文件名不被误伤。**含带前缀形态的对照**：`tests/test_baseline_wp22.py` 必须被认出**且前缀被剥掉**只留文件名 —— 前缀若被吞进捕获组，拼出的路径永远不存在，判据会把真文件全误报成悬空（`src/` 实测 5 处是这种写法） |
| RG-113 | ISS-176 | regression | `test_content_wp26.py` | `test_immutable_tables_reject_update_and_delete` | 4 张不可变表的 UPDATE / DELETE 各撞 `23001`。**必须用真实列**：`SET note = note` 在无 `note` 列的表上会先撞 `42703` ⇒ 没走到触发器却返回"非 23001"，判据以"防线失效"报红而真因是列名写错（`test_billing_wp25.py` 已记过同一坑）。**每表须先 `prepare` 一行且与触发语句同事务**：空表 UPDATE 不触发行级触发器；跨事务的准备行会在回滚后残留并污染后续 8 条判据 |
| RG-114 | ISS-176 | regression | `test_content_wp26.py` | `test_op88_reason_one_missing_node_hits_foreign_key` | OP-88 判据 1（节点不存在）实际报 **`23503`** 而非 `23514` —— 延迟 FK 在 `SET CONSTRAINTS ALL IMMEDIATE` 时**先跑**，触发器里那句 RAISE 根本没机会执行。按"触发器 23514"写会**恒红**。断言 `23503` 并**点名**那条约束，将来有人把该 FK 改成非延迟或删掉时会当场看见 |
| RG-115 | ISS-176 | regression | `test_content_wp26.py` | `test_op88_reason_two_and_three_hit_consistency_trigger` | 判据 2（`entity_type != CONTENT`）/ 3（`entity_id` 不匹配）节点**存在**，FK 通过才轮到触发器 ⇒ 报 `23514`。三条判据合在一个函数里，`RAISE EXCEPTION` **不带 `CONSTRAINT` 名** ⇒ 只能断 sqlstate，**消息文本刻意不写进断言**（措辞变化会恒红） |
| RG-116 | ISS-175 | regression | `test_content_wp26.py` | `test_version_nodes_reverse_trigger_rejects_orphan_for_existing_content` | 反向判据的**阳性**面：内容真实存在、节点无配对版本行 ⇒ `23514`。**必须造真实 `content_items` 行** —— 守卫是**收窄**过的（与 WP18/20/22/23 同一处理），随机 `uuid4()` 会走"放行"分支而以 "NO ERROR" 报红，那是预期行为不是缺陷（对照见 §3.2 的 RG-133）。本包写入顺序先落身份行再 `register_version` ⇒ 生产路径上必然已存在 ⇒ 收窄不误放 |
| RG-118 | ISS-176 | regression | `test_content_wp26.py` | `test_cross_table_checks_hit_consistency_trigger` | 蓝图 / 采纳候选 / 上下文版本三条跨内容链接各撞 `23514`。这三条 **FK 管不住** —— FK 只保证"被引用行存在"，不保证"它属于同一条内容"。判据 3.2 必须**穿透两层**（候选 → 其所属版本 → 那条版本的内容身份），不穿透就退化成"候选存在"（= FK 已保证的事，恒真），正是 ISS-137 的形态 |
| RG-119 | ISS-176 | regression | `test_content_wp26.py` | `test_cycle_one_current_version_id_must_exist` | 环 1（`content_items.current_version_id` → `content_versions`）引用不存在版本 ⇒ `23503` + 约束名点名。该 FK **必须延迟**："插内容行"与"插第一版"同事务而内容行指向第二步行，非延迟会让第一步就撞 FK。"延迟形态"本身由 RG-124 单独钉住，本条只钉"确实在检查 + 名字对得上" |
| RG-120 | ISS-176 | regression | `test_content_wp26.py` | `test_cycle_two_generation_context_id_must_exist` | 环 2（`content_versions.generation_context_id` → `generation_contexts`）引用不存在上下文 ⇒ `23503` + 约束名点名。与正向用例（`test_adopt_title_candidate_context_cycle_is_closed`）**合起来**才说明"延迟真在生效"：正向能过（不早拦）+ 反向能拦（不是形同虚设） |
| RG-121 | ISS-176 | regression | `test_content_wp26.py` | `test_non_deferred_foreign_keys_reject_immediately` | **负向**判据：两条 FK **必须不延迟**，坏值当场撞 `23503`。分界口径 = "两表行是否在同一事务里互相依赖"：延迟的 4 条要求"后插入的那行先被引用"，这 2 条不要求（`content_versions.content_item_id` / `title_candidates.content_version_id` 都是必然先后顺序）。延迟它们等于放弃一条免费检查 |
| RG-122 | ISS-176 | regression | `test_content_wp26.py` | `test_check_constraints_reject_bad_values` | 三条代表性 CHECK 各撞 `23514` 并**逐条点名约束**：`23514` 是本包最常见 sqlstate（38 CHECK + 3 一致性触发器同值）。只断 sqlstate 无法区分"题述那条拦住了"与"别的东西拦住了" —— 后者会让判据在题述 CHECK 被删后**照样绿**（ISS-115 形态）。三条覆盖枚举 / 数值下界 / 枚举三种列形态 |
| RG-123 | ISS-176 | regression | `test_content_wp26.py` | `test_truncate_bypasses_immutable_triggers` | 整份文件的清空策略建立在"TRUNCATE 绕过行级触发器"之上。若有人加 `FOR EACH STATEMENT` 的 TRUNCATE 触发器（或 PG 行为变化），30+ 用例会以"清不干净"失败而失败点分散、排障方向指向各自实现。把前提单独测一条 ⇒ 失败时**第一句**就说明是清空机制坏了。与"不可变"不矛盾：TRUNCATE 不做行级检查，生产代码零 TRUNCATE 路径 |
| RG-124 | ISS-176 | regression | `test_content_wp26.py` | `test_deferred_foreign_keys_count_is_exactly_four` | **不能只数个数**："恰好 4 条"被两种方式骗过 —— ① 换掉其中一条（环 1 改非延迟、另一条改延迟）数目不变；② 将来别的包在本包表上加延迟 FK。故做两件事：延迟集合 == `DEFERRED_FOREIGN_KEYS`（逐条点名）+ `NON_DEFERRED_FOREIGN_KEYS` 确实不延迟（后者才是真防线，抓"顺手把所有 FK 都设成延迟"） |
| RG-125 | ISS-176 | regression | `test_content_wp26.py` | `test_cycle_foreign_keys_are_three` | 3 条环 FK 是迁移里**后置**创建的（建表时两表还不都存在）。后置语句漏掉时，`create_all` 库**有**而迁移库**没有**，两种库都能过绝大多数测试 ⇒ 表现为"生产库上环 1/环 2 没检查"而测试库全绿（`test_schema_parity.py` 的 IS-107 正是此形态）。与 RG-124 互补：那条管延迟形态，这条管环 FK 本身在不在 |
| RG-126 | ISS-176 | regression | `test_content_wp26.py` | `test_foreign_key_delete_action_is_restrict` | 全部 21 条出向 FK 都是 `ON DELETE RESTRICT`（`confdeltype='r'`）。本包 4 张不可变、1 张身份行 —— CASCADE 会让"删一条内容"**静默带走**它的版本/蓝图/候选/上下文，而版本是历史事实（R5），其消失必须是显式动作。RESTRICT 把那种情况变成当场报错（"还有 N 行引用它"） |
| RG-127 | ISS-176 | regression | `test_content_wp26.py` | `test_index_names_match_ddl_declaration` | `ddl.index_names()` == 真库非主键索引 == `models.py` 的 `Index(...)`，三源两两相等。**排除 `pk_*` 而非 `_pkey`**：本仓主键索引名是 `pk_<table>`，按"过滤 `_pkey`"会漏掉它们得到 22 ≠ 17 的**假红**（写就前实测踩到） |
| RG-128 | ISS-176 | regression | `test_content_wp26.py` | `test_trigger_names_match_db` | 真库触发器与 `ddl.trigger_names()` 逐项对上，且**必须分两句问两张表**：`trg_version_nodes_content_paired` 挂在 `version_nodes`（不是本包的表）上，与 5 张本包表的 6 个混在一个查询里会得到"7 个触发器挂 5 张表"这种说不清的结论。第 2 句**只问本包那一个**，不断言 `version_nodes` 总数（它还挂着另四包的触发器）—— 铁律 1 |
| RG-129 | ISS-176 | regression | `test_content_wp26.py` | `test_check_constraints_match_db` | 真库 38 条 CHECK（3+9+18+7+1）与 `_EXPECTED_CHECKS` 逐表对上。期望集必须是**人写常量**而非从库查 —— 从库查再比库是恒真式（"库等于库"），期望集须来自独立来源，再与 `models.py`（第 1 层）+ 真库（本条）三方相遇 |
| RG-130 | ISS-176 | regression | `test_content_wp26.py` | `test_cross_package_inbound_foreign_keys_are_whitelisted` | ⚠️ **2026-10-08 本条预告的事件已发生**：WP27 media 落库（`df6bb2f41f84`）⇒ 本包 5 表首次出现跨包入向 FK（2 条，均指向 `content_items`）⇒ 原「零跨包」口径按设计报红。**已按本行预告处置，不是放宽**：口径改为「**未登记的**跨包入向 FK 为零」，配白名单常量 `_KNOWN_CROSS_PACKAGE_FKS`（登记那 2 条 media FK）—— 新增的未登记 FK 仍报红。`conftest` 侧同步加 `truncate_targets()`：与 `drop_own_tables` 的 `kept` 闭包**同算法、一正一反**（DROP 该**保留**被引用表；TRUNCATE 必须**带上**引用方 —— PG 要求整条引用链，缺一张就 `cannot truncate a table referenced in a foreign key constraint`，实测 93 errors）。白名单自身不腐烂由 RG-174 反向对账。原文「实测 9 条入向 FK 全是包内」是 WP26 时点快照。**2026-10-16 同一事件第二次发生**：WP35（`75af54e`）落库 `export_items`，其 `export_items.content_item_id` 指向 `content_items` ⇒ 白名单由 **2 条增至 3 条**（新增 `fk_export_items_content_item_id_content_items`）。⚠️ **本条曾假绿一轮**：当日清晨那轮全量复跑判定 `backend-pytest.log` 为 `2330 passed, 1 skipped` 全绿，真相是 `rednote_test` 尚未迁到含 `export_items` 的 head ⇒ 库侧查不到该 FK，判据**因库落后而通过**（ISS-95 形态：看起来已覆盖）。库升到 head 后即报红（`2331 passed, 1 failed, 1 skipped`）。**教训**：本判据读库，故对"测试库停在旧 head"无免疫 —— 与 RG-174 刻意的"只看元数据不连库"形成对照。**2026-10-18 第三次发生**：WP29（预览/编辑/反馈定位）落库 `content_feedback_items`/`content_feedback_events`/`content_reviews` 三张表，共 6 条 FK 指向 `content_items`/`content_versions` ⇒ 白名单由 3 条增至 9 条。其中两条（`content_feedback_events` 指向 `content_feedback_items`/`content_versions` 的 FK）的约定全名超过 PostgreSQL 63 字节标识符上限会被库静默截断并追加哈希 ⇒ 顺带触发了 `test_schema_parity.py::test_all_fk_deferrability_matches_between_model_and_db` 的假失败（ORM 元数据里的约束名与 `pg_constraint` 实际名字不一致）；处置是在 `modules/feedback/models.py` 对这两条 FK 显式给 `name=` 缩短后的名字，登记进白名单的是**缩短后**的名字。**2026-10-19 第四、五次发生**：WP30（AI 自检/硬软规则）落库 `self_check_runs`，2 条 FK 指向 `content_items`/`content_versions`；WP36（KOC/交付与替换/发布登记）落库 `deliveries`，1 条 FK 指向 `content_items`。白名单由 9 条增至 **12 条**。两包同一天并行落地，FK 名各自互不相关，未发生命名冲突。**2026-10-21 第七、八次发生**：WP31（试产关口 / 淘汰补产）落库 `trial_round_slots`，1 条 FK 指向 `content_items`；WP32（扩产 / 批产）落库 `batch_items`，1 条 FK 指向 `content_items`。白名单由 12 条增至 **14 条**。⚠️ **本条再次先假绿后报红**：收口时共享库 `rednote_test` 尚未补上这 6 张新表（测试夹具用私有库建表），全量 pytest 因此只报 `test_schema_parity` 一条；补表后本判据才读到这两条入向 FK 并报红 —— 与 2026-10-16 同形，处置同样是登记而非放宽。 **2026-10-24 第九次发生**：WP33（重复检查 / 质量报告）落库 `quality_report_items`，3 条 FK 分别指向 `content_items` / `content_versions` / `generation_contexts`。白名单由 14 条增至 **17 条**，处置同为登记而非放宽。 |
| RG-131 | ISS-83 | regression | `test_content_wp26.py` | `test_router_exposes_exactly_six_endpoints` | 只断言**本包 router 自己**声明的操作数（恰好 6），**不断言全项目路径数** —— 铁律 1 / ISS-83 的**允许形态**（与 RG-039 / RG-051 / RG-087 同形）。刻意不数 `app.routes`：`/projects/{projectId}/...` 是几乎所有业务包共用的前缀，按其计数会在别人合法加端点时变红 |
| RG-132 | ISS-176 | regression | `test_content_wp26.py` | `test_main_registers_content_router` | 直接读 `main.py` **源码**里的 `include_router` 调用，不依赖任何 fixture。`api` fixture 会在"未挂载"时**补挂** ⇒ 第 4 层全部用例即使 `main.py` 忘了注册也全绿，测的是"测试自己搭的接线"而非"上线真正生效的接线"（ISS-95 形态：声称有防线，而防线是测试自己搭的） |
| RG-134 | ISS-192 | regression | `test_backup_restore_drill.py` | `test_fingerprint_classes_match_the_sql_table` | `FINGERPRINT_CLASSES` 与 `_FINGERPRINT_SQLS` 两处**手写清单**必须逐项相等且 ≥6 类。少写一类 ⇒ "少比一类"静默发生而判据照样全绿（ISS-153 只防条数变少，不防比对范围变窄） |
| RG-135 | ISS-192 | regression | `test_backup_restore_drill.py` | `test_every_class_sql_selects_at_least_an_identity_column` | 每条指纹 SQL 除定义列外至少还有一列**身份**。身份为空 ⇒ `objects_sha256` 变成一堆空行的哈希 ⇒ "身份集合相等"退化成恒真式（ISS-115）。这是 RG-134 **没覆盖**的那一格：清单对得上不等于每条 SQL 真产出可区分的身份 |
| RG-136 | ISS-192 | regression | `test_backup_restore_drill.py` | `test_normalizer_merges_the_real_dump_restore_artifact` | **本组重点**：用 2026-10-07 真库上抓到的那对字符串做阳性对照（源库 `(ARRAY[...])::text[]` vs 目标库 `ARRAY[(...)::text, ...]`），断言规范化后相等。`normalize_def` 退化成恒等 ⇒ 演练假红（下场通常是被人调绿）；退化成空串 ⇒ 一切定义同形（假绿）。两种退化都被挡住，因为对照是**实测抓的**不是编的 |
| RG-137 | ISS-192 | regression | `test_backup_restore_drill.py` | `test_normalizer_is_idempotent` | 规范化必须幂等。不幂等 ⇒ 源库算一遍、目标库算一遍可能停在不同"不动点"上，比对结果取决于各自算了几次 —— 那种绿是运气，不是结论 |
| RG-138 | ISS-192 | regression | `test_backup_restore_drill.py` | `test_normalizer_keeps_semantically_different_definitions_apart` | 阴性对照：`ACTIVE` vs `ARCHIVED`、>`vs `< 两组语义不同的定义，规范化后**必须仍不同**。规范化必然损失灵敏度（抹掉 `::类型`），这是**明知**的代价 ⇒ 必须有一条判据把代价钉在"只抹该抹的"范围内。这条一红，说明规范化已变成"把红灯调绿的旋钮" |
| RG-139 | ISS-192 | regression | `test_backup_restore_drill.py` | `test_normalizer_preserves_operator_precedence_grouping` | `(a OR b) AND c` ≠ `a OR (b AND c)`。"去冗余括号"最容易顺手写成"去所有括号"，那样逻辑分组信息丢失而**绝大多数定义仍能被区分** ⇒ 这是最难被发现的一种钝化 |
| RG-140 | ISS-174 | regression | `test_backup_restore_drill.py` | `test_dsn_dialects_are_separated` | libpq 口径（`postgresql://`，给 `pg_dump`/`pg_restore`/`psycopg`）与 SQLAlchemy 口径（`postgresql+psycopg://`，给 alembic 的 `env.py`）必须是两个函数。演练两侧都要用，是全仓最易混的地方；喂错会被 `except` 吞成"连不上库"（ISS-174 同族） |
| RG-141 | ADHOC(WP47 铁律14) | regression | `test_backup_restore_drill.py` | `test_row_counts_derives_table_names_from_the_catalog` | `row_counts()` 必须从 `_SQL_TABLES`（系统表）派生表名，不许写死名单。写死 ⇒ 新表静默不在行数对账里，"62 张表逐表相等"变成"名单里那几张表相等" |
| RG-142 | ISS-153 | regression | `test_backup_restore_drill.py` | `test_check_name_count_matches_expected_checks` | 判据名**从 AST 现场抽**（不抄第二份名单）后与 `EXPECTED_CHECKS` 对账。2026-10-07 我把 15 条写成 13 条，被脚本自己的全等断言当场抓出 ⇒ 证明这条对账在干活。⚠️ 抽取必须按 `checks.append((...))` 的**结构**取首元素：早先用"含一个冒号的字符串"形态匹配，把 `postgresql://` 也算进去得到 17≠15 的假红 |
| RG-143 | ISS-110 | regression | `test_backup_restore_drill.py` | `test_reverse_injection_checks_are_still_declared` | 两条 `reverse:*` 判据必须**按名**在列。删掉它们判据仍会全绿，而"全等"退回成无法自证灵敏的恒真式 —— ISS-110 lesson 的直接落地：**"判据变绿了"必须伴随一次注入破坏的反向验证**。RG-142 只管条数（补两条无关判据凑数就能绕过），故本条按名点名 |
| RG-175 | ADHOC(WP47④ 异常矩阵⑭) | regression | `test_backup_restore_drill.py` | `test_queue_digest_covers_every_declared_queue_table` | `QUEUE_TABLES` 与 `QUEUE_DIGEST_COLUMNS` 必须**同时**包含同一批队列表。首跑就是两者脱钩的活证据：四张队列表全 0 行 ⇒ `compare:row-counts-equal` 比的是 `0 == 0` **恒真**，备份把队列内容全丢了照样报绿 —— 判据在自己的队列表上犯了它 docstring 里写下的 ISS-115。脱钩后少一张表**既不报错也不参与对账**（不是假红，是彻底静默） |
| RG-176 | ADHOC(WP47④) | regression | `test_backup_restore_drill.py` | `test_queue_seed_covers_every_state_of_the_model` | 种子状态必须覆盖 ORM 的**全部**状态，状态清单从 `rednote.*` 真读（`_read_model_states()`）。⚠️ 读取失败必须抛、不得 `except: return {}` —— 空集会让 `set(model) - set(seed)` 恒为空、判据恒绿（ISS-115 同形）。实测首版种子只 6 个 jobs（漏 `CANCEL_REQUESTED` / `WAITING_PROVIDER` / `WAITING_RECONCILIATION`），是真跑演练报红后补到 9 的 |
| RG-177 | ADHOC(WP47④) | regression | `test_backup_restore_drill.py` | `test_queue_seed_row_counts_cover_every_state` | **状态清单列全 ≠ 行数够走完状态**：种子按 `i % len(states)` 循环造，行数少于状态数就造不到末几个状态 —— 此时 RG-176 仍全绿（清单里有那一态）而那一态**从未被对账过**。两种缺陷形态不同，必须各有判据（纪律第 4 条） |
| RG-178 | ADHOC(WP47④) | regression | `test_backup_restore_drill.py` | `test_queue_digest_columns_are_declared_as_pairs` | `QUEUE_DIGEST_COLUMNS` 每项必须是 `(表达式, 别名)` 二元组。写成字符串时 `for expr, _ in columns` 解包出两个字符，SQL 变成 `coalesce(i::text, ...)` ⇒ **比的是列首字母** ⇒ 摘要退化成「行数+首字母」而判据照样全绿。⚠️ 已做变异实测（V4 把二元组改字符串）确认会报红 |
| RG-179 | ADHOC(WP47④) | regression | `test_backup_restore_drill.py` | `test_reverse_tool_fails_fast_when_anchor_does_not_hit` | 反向验证脚本的 `_mutate` 必须**锚点恰好命中 1 次才动手**，否则抛异常中止后续变异。退化成 `if hits == 0: return` ⇒ R1~R5 全部被静默跳过而脚本仍打 `RESULT=PASS` —— 「一个变异都没打上」与「全部打上且都报红」读数完全同形 |
| RG-180 | ADHOC(WP47④) | regression | `test_backup_restore_drill.py` | `test_reverse_tool_restore_returns_reason_instead_of_raising` | `_restore` 还原失败必须**返回原因**、不得抛异常。首版写的 `assert new in current` 在共享工作树下会把磁盘**留在变异状态**（`check_backup_restore_drill.py` 并行会话也在动，变异窗口内被改就会崩）。⚠️ 本条判据首版写成「数return 个数 ≥ 2」，被**变异实测 V3 抓出假绿**（删掉二次确认那条 return 后仍绿，因为成功路径的 `return None` 还在计数）⇒ 改为逐条钉住两个语义失败路径 |
| RG-144 | ISS-194 | regression | `test_rollback_drill.py` | `test_table_exists_judges_null_not_row_list` | `select to_regclass('public.不存在的表')` 返回**一行 NULL**（不是零行）⇒ naive 写法 `bool(fetchall())` **恒真**，幕 5①「部分 DDL 已回滚」的存在性判据整条恒真（ISS-115；开发期探针 2026-10-07 实测就是这么写错的）。⚠️ 假游标必须喂 `fetchone()` 的形态 `(None,)`（**一行**），喂成 `fetchall()` 的 `[(None,)]` 会得出**相反**结论、造成假红 —— 本文件首跑实测踩到 |
| RG-145 | ISS-194 | regression | `test_rollback_drill.py` | `test_table_exists_judges_present_table_true` | 阳性对照：真返回了 `public.<表>` 时必须判 `True`。只有阴性对照、没有阳性对照时，「恒假」同样是失效形态 —— 反向过度修（判什么都不存在）会让幕 5① 恒绿，而「恒真」与「恒假」都是判据没在干活（ISS-110 的对称面） |
| RG-146 | ISS-194 | regression | `test_rollback_drill.py` | `test_table_exists_handles_zero_rows` | 另一种「没有」的**返回形态**：零行时 `fetchone()` 返回 `None`（而非 `(None,)`）。只钉「一行 NULL」会漏掉这一格 —— 两种「不存在」形态都得判 `False`，否则返回形态一变判据又悄悄退化 |
| RG-147 | ISS-194 | regression | `test_rollback_drill.py` | `test_table_exists_source_keeps_the_null_check` | 源码层再钉一次：判据必须落在 `row[0] is not None` 上。上一条依赖假 `psycopg`；若有人改成先 `fetchall()` 再 `bool(...)`（形态变了但仍错），这条仍能抓到 ——「看起来像」与「确实是」必须各有一条。⚠️ 断言前须 `_undoc()` 摘掉 docstring（ISS-195 同族：讲解该坑的文案本身会把「源码不许含 X」判红） |
| RG-148 | ADHOC(WP47 铁律14) | regression | `test_rollback_drill.py` | `test_parse_chain_is_linear_and_complete` | 链必须单 head + 线性 + `rev_path` 覆盖每一个 revision。有分叉 ⇒ `downgrade -1` 的目标不确定 ⇒ 幕 3/4 的语义含糊，而判据**照样全绿**（它只走 `order[0]` 那条栈）。覆盖范围由 `migrations/versions/*.py` 的 **glob** 决定，不由人写名单（铁律 14）；链长下界用 `MIN_REVISIONS` 而非写死 |
| RG-149 | ADHOC(WP47 铁律14) | regression | `test_rollback_drill.py` | `test_parse_chain_revision_and_path_come_from_the_same_parse` | `rev_path[rev]` 必须是**真的声明了该 revision 的那个文件**。被 docstring 点名的反例：在外面按 `stem.endswith(f"_{rev}")` 猜文件名 —— 那是手写规则，迁移文件一换命名风格就静默匹配不上 ⇒ 注入方案退化成 `None`（幕 5 无手段可注入）。本条用**原始文本**独立复核「该 revision 字面量确在自己那个文件里」 |
| RG-150 | ADHOC(WP47) | regression | `test_rollback_drill.py` | `test_parse_chain_no_revision_has_two_children` | 任何 revision 都不许被两个迁移当作 `down_revision`（分叉的直接特征）。这是从「链长 == revision 总数」之外**另取一路**证据：两者若共用同一份解析就是「同一事实的两种写法」（ISS-137），故本条**自建 `children` 计数**、独立数一遍父子关系 |
| RG-151 | ISS-137 | regression | `test_rollback_drill.py` | `test_injection_is_derivable_for_the_two_revisions_the_drill_uses` | 演练**只**用 head 与 head-1，故只断言这两个版本能推导出注入手段。刻意**不**遍历全部版本：实测有 2 个版本（既不建表也不 drop 约束）推导不出，而演示不走它们 —— 断言「全部都能推」会造出一条与验收无关的假红。判据纪律第 4 条：区分「这两版能注」与「任谁都行」两种独立事实 |
| RG-152 | ISS-153 | regression | `test_rollback_drill.py` | `test_obstacle_is_the_last_created_object` | 障碍必须挑 `upgrade()` **最后一个**造出的对象（2026-10-09 由「最后一张表」泛化到任意 kind：表/列/索引/约束）。挑中间那个 ⇒ 障碍之后的 op **一次都不会执行** ⇒ 观测不到「部分 DDL 被回滚」，而判据仍然绿（`prior` 里那些确实都回滚了）—— 只是**测的东西变少了**：静默缩小观察面，正是 ISS-153 只防条数变少、不防范围变窄的那种病。⚠️ 改名与泛化同时发生（原 `test_pre_create_table_injection_targets_the_last_created_table`）：head 换成加列型迁移后，只认表的旧断言在**正确的泛化**面前会报红 |
| RG-153 | ISS-195 | regression | `test_rollback_drill.py` | `test_created_tables_iterates_top_level_statements` | `_created_tables()` 必须按 `fn.body` **顶层语句**走，不能用 `ast.walk(fn)`：`ast.walk` 是**广度优先**，对嵌套在 `if/for` 里的语句不保证源码序 ⇒「取最后一张表」会取错，且错得**不报错**（只是观测变少）。⚠️ 断言前须 `_undoc()`：docstring 里就写着 `ast.walk` 这个反例词 |
| RG-154 | ISS-115 | regression | `test_rollback_drill.py` | `test_lower_bounds_are_non_zero` | 三个下界必须为正数：`MIN_PRIOR_TABLES = 0` 时 `not leftover` **恒真** ⇒「部分 DDL 已回滚」退化成恒真式（ISS-115）；`MIN_REVISIONS < 3` 时「`downgrade -2` 需要链上至少 3 个版本」失去意义。**下界自己也得被判** —— 否则它就是「把判据调绿」的旋钮 |
| RG-155 | ISS-153 | regression | `test_rollback_drill.py` | `test_check_name_count_matches_expected_checks` | 磁盘上声明的**不同**判据名数必须 == `EXPECTED_CHECKS`。`main()` 用**全等**核对（ISS-153：`>=` 只防变少不防变多），但常量与真实声明仍可能**脱钩** ⇒ 本条让脱钩在 pytest 阶段就红。⚠️ 抽取必须**按名去重**：同一判据名会在「无法构造」兜底分支与真实分支各出现一次（实测 31 次出现 / 24 个不同名） |
| RG-156 | ISS-196 | regression | `test_rollback_drill.py` | `test_report_asserts_full_equality_not_at_least` | `_report()` 的条数守卫必须是 `!=` **全等**，不是 `<`/`<=`（ISS-153：多出来的重复条不会被发现）。⚠️ 取值必须精确到「那个 `if` 的**谓词**」：`_report` 的 `return` 里同样写了 `total != EXPECTED_CHECKS` ⇒ 一旦退化成「整段源码含该串」，把 `if` 改成 `<` 之后**照样通过**（恒真式，2026-10-07 反向验证当场抓出这条假绿） |
| RG-157 | ISS-110 | regression | `test_rollback_drill.py` | `test_reverse_verification_checks_are_still_declared` | 四条 `reverse:*`（幕 2 指纹灵敏度 2 条 + 幕 6 落点差异 2 条）必须**按名**在列。ISS-110：「判据变绿了」必须伴随一次注入破坏的反向验证，否则分不清「修好了」与「因为瞎所以绿」。条数断言挡得住「删两条」，挡不住「删两条再补两条无关的」⇒ 本条按名点名（与 RG-143 同形，一处管 backup-restore、一处管 rollback） |
| RG-158 | ISS-196 | regression | `test_rollback_drill.py` | `test_every_declared_name_is_a_real_check_prefix` | 判据名必须带已知前缀（`static:`/`build:`/`rollback:`/`refuse:`/`fail:`/`granularity:`/`reverse:`/`evidence:`）。防的是「拼错导致的重复声明」：`rollback:` 写成 `rollbak:` 时集合里**多一个、少一个**，而**总数可能不变**（一个拼错 + 一个补上）⇒ RG-155 的全等断言照样绿，只有前缀校验能当场看见 |
| RG-159 | ISS-95 | regression | `test_rollback_drill.py` | `test_transaction_per_migration_is_declared_in_the_repo_env_py` | 仓库 `migrations/env.py` 必须声明 `transaction_per_migration=True`，且该字面量在文件里**唯一**。这是幕 5② 的**语义前提**：它决定「升级跑到一半失败」停在**起点**还是停在**上一条已提交的迁移**。声明没了 ⇒ 断言在错误前提下的绿（ISS-95：声称有防线，而防线是虚的）；出现 2 次 ⇒ 幕 6 的沙箱翻转失去唯一性 |
| RG-160 | ISS-143 | regression | `test_rollback_drill.py` | `test_stage_migrations_flips_exactly_one_literal` | 翻转必须**生效且只改那一处**，且**不许碰仓库文件**。退化成「没翻到就照原样跑」⇒ 幕 6 两臂变成同一个臂 ⇒ 反向验证恒真。`flip=False` 时须**逐字节原样**（免得「顺手」改了别的）；仓库 `env.py` 内容一个字节都不许变（沙箱必须无副作用） |
| RG-161 | ISS-143 | regression | `test_rollback_drill.py` | `test_stage_migrations_refuses_when_the_literal_is_not_unique` | 目标行**找不到** / **找到多处** ⇒ 必须抛 `RuntimeError`，不许静默照原样跑。这是幕 6 唯一能自证「两臂确实不同」的地方：「没翻到」与「翻到了」在结果上同形（ISS-143）⇒ 只有抛错能让它当场可见（报红总比静默恒真好，且红的时候该查的是这段代码） |
| RG-162 | ISS-143 | regression | `test_rollback_drill.py` | `test_underivable_injection_reports_named_failures_not_skips` | `main()` 里两个 `is None` **兜底分支**必须按名声明那 6 条为 `False`（幕 5① 4 条 + 幕 5② 2 条）。若被改成 `pass`/`continue`，判据条数会少 6 条（被 `EXPECTED_CHECKS` 全等挡住）—— 但「删 6 条再补 6 条无关的」能绕过去 ⇒ 本条按名点名。ISS-143：「没跑」与「跑失败」同形，报 FAIL 才区分得开 |
| RG-163 | ISS-143 | regression | `test_rollback_drill.py` | `test_all_declared_names_are_reachable_in_main` | 所有声明的判据名都必须真的在 `main()` 里出现。防「声明了但忘了调用」：那种缺失会让条数变少（被全等断言挡住），但本条给出更直接的定位 —— 直接把缺的名字打出来，失败信息本身即排障线索（与 RG-162 互补：那条管分支里的取值，这条管调用点存在性） |
| RG-164 | ISS-174 | regression | `test_rollback_drill.py` | `test_dsn_dialects_are_separated` | libpq 口径（`postgresql://`，给 `pg_dump`/`pg_restore`/`psycopg.connect`）与 SQLAlchemy 口径（`postgresql+psycopg://`，给 alembic 的 `env.py`）必须是**两个函数**。`env.py::_resolve_database_url` **强制要求**方言后缀，而 libpq 系列**不认** ⇒ 喂错会被 `except` 吞成「连不上库」⇒ 判据降级成假绿（ISS-174 同族） |
| RG-165 | ISS-197 | regression | `test_rollback_drill.py` | `test_base_dsn_follows_environment` | 基准 DSN 必须**只换库名**地派生自环境变量，不许自己拼本机角色。首版自己拼 `rednote@localhost:5432`，本机跑得通、CI 上必红在 `role "rednote" does not exist` —— 那种红与「回滚路径坏了」毫无关系，是最容易误导人的一种红（ISS-108 原样复发）。两个环境变量名都要认（与 `check_migration_model_parity.py` 同口径） |
| RG-166 | ISS-197 | regression | `test_rollback_drill.py` | `test_pg_bin_prefers_env_then_which_then_homebrew` | `pg_bin()` 三级回退：`REDNOTE_PG_BIN` → `which pg_dump` → `DEFAULT_PG_BIN`（Homebrew）。写死 Homebrew ⇒ CI（客户端来自 apt，路径不同）上 `pg_dump: not found` —— 与上一条同族：**本机口径被当成了通用口径**。三级各测一次，任一级缺失都会让回退退化 |
| RG-167 | ISS-197 | regression | `test_rollback_drill.py` | `test_backup_tool_delegates_pg_bin_instead_of_hardcoding` | `backup_pg` 不许自己再写一份 pg 工具路径解析（回退知识**只允许存在一份**）。两处各写一份 ⇒ 将来改一处忘另一处，另一处**不报错**，只在 CI 上 not found。用 AST 断言 `_pg_bin()` 体内不含 `DEFAULT_PG_BIN`（`_undoc()` 后 unparse），并断言它确实 `return drill_db.pg_bin()` |
| RG-168 | ISS-110 | regression | `test_rollback_drill.py` | `test_reverse_verification_table_exists_predicate_flips` | 破坏：`row[0] is not None` → `row[0:1]`（回到 naive 的「非空即存在」）。**行为级**反向验证：同一个假游标喂给原版与破坏版，断言两者结论**相反**（原版 `False`、破坏版 `True`）。结论没翻转 ⇒ RG-144 那条阴性对照是恒真式 |
| RG-169 | ISS-110 | regression | `test_rollback_drill.py` | `test_reverse_verification_created_tables_order_predicate_flips` | 破坏：顶层语句遍历 → `ast.walk(fn)`。锚点取**带注释的那一行**（`for stmt in fn.body:  # 顶层语句：源码序`），锚点失配即报红（防「改了实现但锚点没匹配上、于是破坏没发生而断言仍绿」）。破坏后须 `fn.body` 缺席且 `ast.walk(fn)` 在场 |
| RG-170 | ISS-196 | regression | `test_rollback_drill.py` | `test_reverse_verification_full_equality_predicate_flips` | 破坏：`total != EXPECTED_CHECKS` → `total < EXPECTED_CHECKS`。并**顺带钉住**「为什么不能只查整段源码里有没有那个字符串」：`return` 那句里同样写了全等形式，纯字符串断言在破坏后**照样通过** —— 这条断言本身就是 ISS-196 那次假绿的固化 |
| RG-171 | ISS-115 | regression | `test_rollback_drill.py` | `test_reverse_verification_lower_bound_predicate_flips` | 破坏：`MIN_PRIOR_TABLES = 2` → `= 0`（下界为 0 ⇒ `not leftover` 恒真）。断言「改 0 之后 `>= 2` 不再成立」—— 证明那条下界判断**有区分力**。与 RG-154 配对：一条守「下界是正数」的声明，一条守「正数这件事真的能被检出异常」 |
| RG-172 | ISS-143 | regression | `test_rollback_drill.py` | `test_reverse_verification_stage_flip_guard_predicate_flips` | 破坏：`if src.count(_TPM_LITERAL) != 1:` → `if False:`（守卫被拆、静默照原样跑）。断言原版在「完全没有那一行」时**抛** `RuntimeError`、破坏版**不再抛错** ⇒ 证明这条守卫真的在挡事、不是装饰（RG-161 的反向面） |
| RG-173 | ISS-195 | regression | `test_rollback_drill.py` | `test_drill_db_module_has_no_second_hardcoded_path_resolution` | 反向兜底：全仓 `tools/**/*.py` 里 `drill_db.DEFAULT_PG_BIN` 该字面量**只许作为代码常量出现在 `drill_db.py` 一处**（唯一事实来源）。⚠️ 必须用 `_code_string_literals()`（AST 取值、先剥 docstring）而非子串查找：讲解该字面量的 docstring 会把子串查找判红（ISS-195 同族在本文件内复发 2 次） |
| RG-174 | ISS-176 | regression | `test_content_wp26.py` | `test_known_cross_package_fk_whitelist_is_not_stale` | RG-130 改白名单口径后，白名单自身会腐烂：被引用表/列改名或那条 FK 被删 ⇒ 条目变成**永远匹配不上**的死条目，而 RG-130 只走单方向（库 → 名单）⇒ **删条目**就能让它变绿。本条走反方向：白名单每一条都必须在**磁盘枚举全部模块 `models.py`** 派生出的集合里（铁律 14：覆盖范围由磁盘决定、禁人写名单；与 `_derived_cross_package_fk_names` 共用同一函数）。**只看 ORM 元数据、不连库** ⇒ 对未升到 head 的旧测试库免疫，不因库落后而假红（与 RG-130 的差别见 `test_content_wp26.py` 顶部口径说明表） |
| RG-181 | ISS-95 | regression | `test_rollback_drill.py` | `test_obstacle_step_rebuilds_the_dropped_constraint` | `drop_owned_dependency` 的 `injection_for()` 返回值**没有 `obstacle` 键**（它不做 pre_create，而是删掉迁移自己要 DROP 的 CHECK）⇒ 障碍对象只能从 `table`/`name` 重建。返回 `None` ⇒ `_obstacle_intact()` 走 fail-closed ⇒ 该手段永判红。⚠️ **这一支在 `main()` 里当前不可达**（head 与 head-1 都是加列型，两条注入都是 `pre_create_constraint`）⇒ 判据层写多少断言都补不上这个盲区，只能靠单测（2026-10-09 变异 M7 实测：把 drop 分支改成恒返回 `None`，`main()` 照样 24/24 绿） |
| RG-182 | ISS-95 | regression | `test_rollback_drill.py` | `test_obstacle_intact_drop_branch_follows_state` | `drop_owned_dependency` 的「效果」= **被我删的对象仍然不在**（不是"桩还在"）。⚠️ 旧版此处写 `return True, "无桩对象"` —— **恒真**；合成后的整个谓词只有"必须为真"一个使用点 ⇒ 桩是 `_apply_injection()` 用 autocommit 建在 Alembic 事务之外的、而失败正是撞它造成的 ⇒ "还在"是结构性必然（**变异 M5 实测：把它改成 `return True` 后 `main()` 24/24 全绿**）。本条两个方向都验（存在 ⇒ False / 不存在 ⇒ True），只验一边会漏掉"反向写死成恒假"这种对称失效（与 RG-145 同款纪律）。⚠️ 该分支在 `main()` 里同样不可达（同 RG-181 的理由） |
| RG-183 | ISS-95 | regression | `test_rollback_drill.py` | `test_obstacle_intact_is_fail_closed_when_step_underivable` | 障碍推不出来（`table`/`name` 皆空 ⇒ `_obstacle_step()` 返回 `None`）时，`_obstacle_intact()` **必须判 `False`**（fail-closed），不许静默 `True`。若返回 True，`drop_owned_dependency` 一旦将来被走到就会给出一条**永远为真**的 `fail:obstacle-intact` —— 与 2026-10-09 那次 M5 假绿同形（ISS-95：「声称有防线而防线是虚的」）。与之配对的是 `main()` 幕 5① 新增的**配对观测**：`fail:obstacle-intact` 断言 `(not 注入前) and 注入后`，让该谓词必须在两个状态下取到不同值 —— 恒真（M5a）与恒假（M5b）都会报红 |
| RG-184 | ISS-211 | regression | `test_rollback_drill.py` | `test_rebuild_migration_falls_back_to_drop_tier_not_pre_create` | **tier-1 挑障碍时必须先剔除「本迁移自己要 drop 的名字」**。tier-1 的手法（预建它要造的对象、让 `create_*` 撞 `DuplicateObject`）**前提是对象迁移前不存在**，而「重建同名对象」型迁移（`e8f07ba8f2bb`：先 `drop_constraint` 再用**同名** `create_check_constraint`）违反该前提 ⇒ 预建桩会撞上**迁移前本来就有的**那条约束、整支判据在注入阶段抛 `DuplicateObject` 崩栈退出（不是某条断言红）。⚠️ **仅干净检出现形**：共享树当时链头是两条纯加列型迁移，障碍都有真新名字 ⇒ 一绿一红，而绿的不是 CI 口径。本条**遍历整条链**断言「tier-1 障碍名 ∉ 本迁移 drop 集合」，并设 tier-1/tier-2 **各至少一次**的活性下界；**刻意不断言「所有迁移都可推」**（纯触发器迁移 `515fb8b0d31a` 本就不该可推，越界断言会把不需要的性质变成红）。守这条修复的必须是单测而非判据本身 —— 判据层的红/绿**依赖链头形态**（在已模拟提交的检出上做同一变异，判据仍绿） |
| RG-185 | ISS-184 | regression | `test_content_wp26.py` | `test_read_generation_contexts_by_prompt_version_filters` | OP-160 的关闭条件写在 WP26 文档 §3.6（"『按 `prompt_version_id` 反查历史生成』的用例通过"），而该用例此前**不存在** ⇒ 机制四件（表/列/索引/反查函数）都在、**没有一次把它们串起来跑**，属"声称有防线"（ISS-184）。本条区分**三种世界**：按 version_a 命中 / 按 version_b 命中 / 按不存在的版本查到空 —— 只断言"查到了"无法区分"实现退化成返回全表"（会过）与"返回空表"（会红但看不出是过滤错了还是数据没造上）。⚠️ 全用例在一个 savepoint 内**造完即回滚**：`prompt_templates`/`prompt_versions`/`version_nodes` 不在本包 `DROP_ORDER` 且都是不可变表（提交后清不掉），又不可 `TRUNCATE version_nodes`（全仓共享表，会毁兄弟包夹具行）—— 更隐蔽的理由是 `prompts_bridge.resolve_active_version` 按 `created_at DESC` 取**最新一条**模板，提交一条无激活版本的 `CONTENT_COPY` 模板会让后续所有该 kind 生成 fail-closed（`PROMPT_NOT_ACTIVATED`）。块外另有一次独立查询核对零残留（自身也是判据） |
| RG-186 | ISS-223 | regression | `test_test_data_volume.py` | `test_thresholds_are_derived_from_measured_baseline` | `MAX_ROWS` 必须等于 `MEASURED_ROWS_PER_RUN × SAFETY_FACTOR` 向上取整，阈值不许是拍脑袋的数字（铁律 14） |
| RG-187 | ISS-223 | regression | `test_test_data_volume.py` | `test_safety_factor_is_within_a_sane_range` | 安全倍数必须落在 [2, 8] 区间，防止被悄悄改成形同虚设的超大值 |
| RG-188 | ISS-223 | regression | `test_test_data_volume.py` | `test_measured_rows_per_run_covers_every_root_table` | 实测基线必须覆盖三张根表且均为正数，防止占位 0 被当真值上线 |
| RG-189 | ISS-223 | regression | `test_test_data_volume.py` | `test_round_up` | `_round_up` 辅助函数的边界值（整除 / 有余数 / 0） |
| RG-190 | ISS-223 | regression | `test_test_data_volume.py` | `test_reading_at_threshold_is_not_a_violation` | 行数恰等于阈值必须放行，不能把"等于"误判成"超过" |
| RG-191 | ISS-223 | regression | `test_test_data_volume.py` | `test_one_over_threshold_is_a_violation` | 行数恰为阈值+1 必须判超阈值并指名该表 |
| RG-192 | ISS-223 | regression | `test_test_data_volume.py` | `test_multiple_tables_over_limit_are_all_named` | 三张表同时超阈值时必须全部指名，不能只报第一个就停 |
| RG-193 | ISS-223 | regression | `test_test_data_volume.py` | `test_all_under_limit_yields_no_violation` | 三张表均在阈值内必须零违规 |
| RG-194 | ISS-223 | regression | `test_test_data_volume.py` | `test_missing_table_is_problem_not_zero` | 表不存在必须走 problem 分支，不能被折算成 0 行放行（ISS-143 同族） |
| RG-195 | ISS-223 | regression | `test_test_data_volume.py` | `test_unreadable_count_is_problem` | 读数缺失（`rows=None`）必须计入 problem 而非 violation |
| RG-196 | ISS-223 | regression | `test_test_data_volume.py` | `test_read_counts_reports_connection_failure_as_problem_not_crash` | 连不上库时 `read_counts` 必须优雅返回带 problem 的读数，不能抛异常崩溃 |
| RG-197 | ISS-223 | regression | `test_test_data_volume.py` | `test_failure_text_names_table_actual_limit_and_reset_tool` | 失败文案必须指名表 / 实际行数 / 阈值并提示重置工具，不能是裸红 |
| RG-198 | ISS-223 | regression | `test_test_data_volume.py` | `test_pass_report_has_no_fail_marker` | PASS 报告不得同时出现 `[FAIL]`（ISS-149 镜像：绿也要有形状） |
| RG-199 | ISS-223 | regression | `test_test_data_volume.py` | `test_main_exits_zero_on_pass` | `main()` 在全部放行时退出码必须为 0 |
| RG-200 | ISS-223 | regression | `test_test_data_volume.py` | `test_main_exits_nonzero_on_violation` | `main()` 在有违规时退出码必须非零 |
| RG-201 | ISS-223 | regression | `test_test_data_volume.py` | `test_main_exits_nonzero_on_connection_failure` | `main()` 在连不上库时退出码必须非零，不能悄悄当成通过 |
| RG-202 | ISS-223 | regression | `test_test_data_volume.py` | `test_env_override_is_respected_for_the_target_database` | 目标库必须可被 `REDNOTE_TEST_DATABASE_URL` 覆盖，否则并发开发线无法隔离 |
| RG-203 | ISS-223 | regression | `test_test_data_volume.py` | `test_default_dsn_is_not_a_reserved_database` | 判据 D 自己的默认目标库不得是保留库（复用判据 A 的解析，不自证） |
| RG-204 | ISS-223 | regression | `test_test_data_volume.py` | `test_default_dsn_matches_conftest_and_residue_judge` | 判据 D / 判据 B / conftest 三处默认 DSN 常量不得漂移（刻意不互相 import，靠对账） |
| RG-205 | ISS-223 | regression | `test_test_data_volume.py` | `test_root_tables_are_exactly_the_three_unowned_roots` | `ROOT_TABLES` 必须恰为 `projects`/`users`/`brands` 三张 |
| RG-206 | ISS-223 | regression | `test_test_data_volume.py` | `test_read_counts_contains_no_write_statements` | `read_counts` 函数体内不得出现任何写操作关键字（纯只读） |
| RG-207 | ISS-223 | regression | `test_reset_shared_test_db.py` | `test_guard_reserved_rejects_postgres` | 保留库 `postgres` 必须被拒绝 |
| RG-208 | ISS-223 | regression | `test_reset_shared_test_db.py` | `test_guard_reserved_rejects_none` | DSN 解析不出库名时必须拒绝，不能当成随便哪个库放行 |
| RG-209 | ISS-223 | regression | `test_reset_shared_test_db.py` | `test_guard_reserved_allows_ordinary_test_database` | 阳性对照：正常测试库名不应被保留库这道门拦住 |
| RG-210 | ISS-223 | regression | `test_reset_shared_test_db.py` | `test_main_rejects_mismatched_confirm` | `--confirm` 必须原样等于目标库名，防止复制上一次命令忘了改库名 |
| RG-211 | ISS-223 | regression | `test_reset_shared_test_db.py` | `test_yes_path_calls_assert_quiet_before_building_write_engine` | `--yes` 分支必须先探活（`assert_quiet`）再建写引擎，顺序不可调换（ISS-160 教训） |
| RG-212 | ISS-223 | regression | `test_reset_shared_test_db.py` | `test_dsn_ignores_redone_database_url` | 本工具只认 `REDNOTE_TEST_DATABASE_URL`，不读 `REDNOTE_DATABASE_URL` |
| RG-213 | ISS-223 | regression | `test_reset_shared_test_db.py` | `test_dsn_env_override_is_respected` | 测试库 DSN 必须可被环境变量覆盖 |
| RG-214 | ISS-223 | regression | `test_reset_shared_test_db.py` | `test_root_tables_match_the_volume_judge` | 重置工具的 `ROOT_TABLES` 不得与判据 D 的 `ROOT_TABLES` 漂移 |
| RG-215 | ISS-223 | regression | `test_reset_shared_test_db.py` | `test_truncate_targets_is_the_shared_db_graph_implementation` | 闭包算法必须复用 `db_graph.truncate_targets`，不得另写一份 |
| RG-216 | ISS-223 | regression | `test_reset_shared_test_db.py` | `test_execute_sets_lock_and_statement_timeout` | `execute()` 必须设 `lock_timeout`/`statement_timeout`，否则并发写入时无限等锁 |
| RG-217 | ISS-223 | regression | `test_reset_shared_test_db.py` | `test_truncate_statement_does_not_use_cascade` | `TRUNCATE` 不得带 `CASCADE`，闭包已显式列出整条引用链（同 ISS-106 论证） |

### 3.2 `cross_wp`（13 条）

| 编号 | 来源 | 标记 | 文件 | 函数 | 守护的共享面 |
|---|---|---|---|---|---|
| RG-101 | ADHOC(WP17 共享触发器函数) | cross_wp | `test_parsing_wp17.py` | `test_shared_trigger_function_still_exists` | 共用不可变触发器函数不被后续迁移删掉 |
| RG-102 | ADHOC(WP11 受控取值) | cross_wp | `test_versions_wp11.py` | `test_entity_type_check_constraint_matches_python_set` | `version_nodes.entity_type` CHECK 与代码集合同步 |
| RG-103 | ADHOC(WP20 扩 KNOWLEDGE) | cross_wp | `test_versions_wp11.py` | `test_knowledge_entity_type_is_usable` | WP20 扩取值后 WP11 版本族仍可用 |
| RG-104 | ADHOC(WP18 OP-88) | cross_wp | `test_brief_wp18.py` | `test_trigger_ddl_targets_shared_version_nodes_table` | BRIEF 触发器挂在共享表上 |
| RG-105 | ADHOC(WP18 OP-88) | cross_wp | `test_brief_wp18.py` | `test_node_paired_trigger_is_scoped_to_brief_entity_type` | BRIEF 触发器只对 BRIEF 生效 |
| RG-106 | ADHOC(WP22 接缝) | cross_wp | `test_baseline_wp22.py` | `test_node_paired_trigger_only_fires_for_baseline_nodes` | BASELINE 触发器只对 BASELINE 生效（DDL） |
| RG-107 | ADHOC(WP22 接缝) | cross_wp | `test_baseline_wp22.py` | `test_other_entity_types_are_not_affected_by_baseline_pairing` | 同上（真库行为） |
| RG-108 | ADHOC(WP23 接缝) | cross_wp | `test_prompts_wp23.py` | `test_all_trigger_ddl_mentions_shared_function` | PROMPT 触发器复用共享函数 |
| RG-109 | ADHOC(WP23 接缝) | cross_wp | `test_prompts_wp23.py` | `test_paired_trigger_is_scoped_to_prompt` | PROMPT 触发器只对 PROMPT 生效（DDL） |
| RG-110 | ADHOC(WP23 接缝) | cross_wp | `test_prompts_wp23.py` | `test_brief_node_is_not_affected_by_prompt_trigger` | 同上（真库：BRIEF 节点不受波及） |

| RG-111 | ISS-79 | cross_wp | `test_audit_wp15.py` | `test_all_trigger_ddl_points_at_shared_function` | audit 的 `all_trigger_ddl()` 第 5 项指向跨包共用函数 `rednote_reject_mutation()` ⇒ `consistency_items()` **必须为空**，downgrade 第 2 步一次都不执行才不误删。WP15 的 downgrade 就是漏了这一步而删掉共用函数（`alembic downgrade base` 报 DependentObjectsStillExist） |
| RG-112 | ISS-79 | cross_wp | `test_versions_wp11.py` | `test_all_trigger_ddl_mentions_shared_function` | versions 的共用函数名在 `consistency_items()` **必须缺席**、在 `all_trigger_ddl()` **必须在场**：downgrade 删什么由"这个对象是不是我建的"决定，不由"它跟我的表有没有关"决定 |
| RG-133 | ISS-175 | cross_wp | `test_content_wp26.py` | `test_version_nodes_reverse_trigger_allows_orphan_for_unknown_entity` | **本包往共享表 `version_nodes` 挂了 `trg_version_nodes_content_paired`**（与 WP18/WP20/WP22/WP23 的另四个 `*_node_paired` 在同一张表上并存），按准入规则 3 必须有「别家不受影响」的对照。本行守护的共享面 = **那道守卫 `IF NOT EXISTS (SELECT 1 FROM content_items WHERE id = NEW.entity_id) THEN RETURN NULL`**：不让本包的收窄把 WP11 通用用例（`ENTITY_TYPE_CONTENT` 是它 8 类样本实体之一）判死。实测代价：**不收窄时 WP11 既有 2 例报红**（`test_cycle_detection_terminates_on_cyclic_graph` / `test_promote_draft_keeps_entity_type_and_id`）—— 即「别的包的进度被本包阻塞」，见 ISS-175。⚠️ 本条原先误登记在 §3.1 为 RG-117 且标记写 `regression`：它有 `regression` 的形态（钉住本包的一个根因），但它的**守护对象是共享面**，按准入规则 3 归 `cross_wp`；R1~R4 当时**全 PASS**（两侧一致地少登记了这条跨包面，无判据守住）。

## 4. 已知缺口（登记在案，未补）

| 缺口 | 说明 | 计划 |
|---|---|---|
| issue-log 中 ISS-24 | 修的是证据脚本 `_pytest_result()`，不是 pytest 测试 | 由 `check_evidence_scripts.py` 兜住，不入本表 |
| ISS-55/65/68 | 修的是测试夹具自身，无独立回归函数 | 夹具修复后整文件用例即守护；如再现则补专用测试 |
| `slow` 标记 0 条 | 当前无超长用例（全量用时**见 `evidence/m1/backend-pytest.log`，不在此写死**） | WP24 引入评测集后首批打 `slow` |
| 前端 Demo 无 E2E 回归 | P0 三项（图标断网/复选框三态/分段控件双 DOM）无自动化 | 见 `docs/研发计划修正清单_2026-09-27.md` 第 ⑦ 项「测试 9 层落地表」 |
| audit_logs / outbox 共享面无 `cross_wp` | 目前只覆盖 `version_nodes` | 下一次有包往这两表写时补 |
| **准入规则 3 无机械判据守住**（ISS-176 同族第 5 次现身） | R1~R4 只能对账「登记册 ⇄ 代码标记」，**两侧一致地漏掉**跨包面时全 PASS —— WP26 往共享表 `version_nodes` 挂了 `trg_version_nodes_content_paired`，且测试已写好（`test_version_nodes_reverse_trigger_allows_orphan_for_unknown_entity`），却把标记写成 `regression`、登记在 §3.1 ⇒ 规则 3 要求的「别家不受影响」对照**形式上缺席**，而判据无感。已按规则 3 改标 `cross_wp` 并重登记为 **RG-133**（§3.2 计数 12→13） | 由人巡检「往 `version_nodes`/`audit_logs`/`outbox` 挂触发器的包是否都有 `cross_wp`」；**判据化待做**：可由 `check_register.py` 增一条 R5（扫 `src` 里对三张共享表的 `CREATE CONSTRAINT TRIGGER`，要求同名模块的测试文件至少有一个 `cross_wp`） |
