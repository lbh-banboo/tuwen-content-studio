-- 把共享测试库 rednote_test 补到与迁移 a1f31c0de001(WP31) + b2f32c0de002(WP32) 一致。
-- 由 alembic 离线模式 (upgrade f3a7c9d15b28:b2f32c0de002 --sql) 导出，已去掉对 alembic_version 的 UPDATE
-- （该库 alembic_version 停在 daea2b27212c，表由各包夹具 create_all 建出，不走迁移）。
-- 整个文件在一个事务里，任何一步失败即整体回滚。
BEGIN;

-- Running upgrade f3a7c9d15b28 -> a1f31c0de001

CREATE TABLE trial_rounds (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    project_id UUID NOT NULL, 
    round_no INTEGER NOT NULL, 
    kind VARCHAR(12) NOT NULL, 
    state VARCHAR(16) DEFAULT 'PLANNED' NOT NULL, 
    plan_version_id UUID NOT NULL, 
    baseline_id UUID NOT NULL, 
    source_round_id UUID, 
    requested_quantity INTEGER NOT NULL, 
    planned_quantity INTEGER NOT NULL, 
    limit_applied INTEGER NOT NULL, 
    limit_source VARCHAR(12) NOT NULL, 
    plan_snapshot JSONB NOT NULL, 
    cancel_reason VARCHAR(500), 
    idempotency_key VARCHAR(160), 
    request_hash VARCHAR(64), 
    created_by UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    lock_version BIGINT DEFAULT 1 NOT NULL, 
    CONSTRAINT pk_trial_rounds PRIMARY KEY (id), 
    CONSTRAINT ck_trial_rounds_supplement_has_source CHECK ((kind = 'SUPPLEMENT') = (source_round_id IS NOT NULL)), 
    CONSTRAINT ck_trial_rounds_cancel_reason_paired_with_cancelled CHECK ((state = 'CANCELLED') = (cancel_reason IS NOT NULL)), 
    CONSTRAINT ck_trial_rounds_plan_snapshot_is_object CHECK (jsonb_typeof(plan_snapshot) = 'object'), 
    CONSTRAINT ck_trial_rounds_kind_valid CHECK (kind IN ('SUPPLEMENT', 'TRIAL')), 
    CONSTRAINT ck_trial_rounds_limit_source_valid CHECK (limit_source IN ('GLOBAL', 'INHERITED', 'MANUAL')), 
    CONSTRAINT ck_trial_rounds_state_valid CHECK (state IN ('CANCELLED', 'GATE_PASSED', 'GENERATING', 'PLANNED', 'REVIEWING')), 
    CONSTRAINT ck_trial_rounds_idempotency_paired CHECK ((idempotency_key IS NULL) = (request_hash IS NULL)), 
    CONSTRAINT ck_trial_rounds_cancel_reason_not_blank CHECK (cancel_reason IS NULL OR char_length(btrim(cancel_reason)) > 0), 
    CONSTRAINT ck_trial_rounds_idempotency_key_not_blank CHECK (idempotency_key IS NULL OR char_length(btrim(idempotency_key)) > 0), 
    CONSTRAINT ck_trial_rounds_limit_applied_positive CHECK (limit_applied >= 1), 
    CONSTRAINT ck_trial_rounds_planned_not_above_requested CHECK (planned_quantity <= requested_quantity), 
    CONSTRAINT ck_trial_rounds_planned_quantity_positive CHECK (planned_quantity >= 1), 
    CONSTRAINT ck_trial_rounds_request_hash_length CHECK (request_hash IS NULL OR char_length(request_hash) = 64), 
    CONSTRAINT ck_trial_rounds_requested_within_bound CHECK (requested_quantity <= 100000), 
    CONSTRAINT ck_trial_rounds_requested_within_limit_applied CHECK (requested_quantity <= limit_applied), 
    CONSTRAINT ck_trial_rounds_requested_quantity_positive CHECK (requested_quantity >= 1), 
    CONSTRAINT ck_trial_rounds_round_no_positive CHECK (round_no >= 1), 
    CONSTRAINT ck_trial_rounds_source_not_self CHECK (source_round_id IS NULL OR source_round_id <> id), 
    CONSTRAINT fk_trial_rounds_baseline_id_production_baselines FOREIGN KEY(baseline_id) REFERENCES production_baselines (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_trial_rounds_created_by_users FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_trial_rounds_plan_version_id_version_nodes FOREIGN KEY(plan_version_id) REFERENCES version_nodes (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_trial_rounds_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_trial_rounds_source_round_id_trial_rounds FOREIGN KEY(source_round_id) REFERENCES trial_rounds (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_trial_rounds_project_id_round_no UNIQUE (project_id, round_no)
);

CREATE INDEX ix_trial_rounds_source_round ON trial_rounds (source_round_id);

CREATE UNIQUE INDEX uq_trial_rounds_project_idempotency ON trial_rounds (project_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX uq_trial_rounds_project_open ON trial_rounds (project_id) WHERE state IN ('GENERATING', 'PLANNED', 'REVIEWING');

CREATE TABLE trial_gate_decisions (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    round_id UUID NOT NULL, 
    approved_scene_keys JSONB NOT NULL, 
    condition_results JSONB NOT NULL, 
    statistics JSONB NOT NULL, 
    basis JSONB NOT NULL, 
    reason VARCHAR(500) NOT NULL, 
    decided_by UUID, 
    decided_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_trial_gate_decisions PRIMARY KEY (id), 
    CONSTRAINT ck_trial_gate_decisions_approved_scene_keys_not_empty CHECK (CASE WHEN jsonb_typeof(approved_scene_keys) = 'array' THEN jsonb_array_length(approved_scene_keys) >= 1 ELSE false END), 
    CONSTRAINT ck_trial_gate_decisions_approved_scene_keys_is_array CHECK (jsonb_typeof(approved_scene_keys) = 'array'), 
    CONSTRAINT ck_trial_gate_decisions_basis_is_array CHECK (jsonb_typeof(basis) = 'array'), 
    CONSTRAINT ck_trial_gate_decisions_condition_results_is_object CHECK (jsonb_typeof(condition_results) = 'object'), 
    CONSTRAINT ck_trial_gate_decisions_statistics_is_object CHECK (jsonb_typeof(statistics) = 'object'), 
    CONSTRAINT ck_trial_gate_decisions_reason_not_blank CHECK (char_length(btrim(reason)) > 0), 
    CONSTRAINT fk_trial_gate_decisions_decided_by_users FOREIGN KEY(decided_by) REFERENCES users (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_trial_gate_decisions_round_id_trial_rounds FOREIGN KEY(round_id) REFERENCES trial_rounds (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_trial_gate_decisions_round_id UNIQUE (round_id)
);

CREATE TABLE trial_round_slots (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    round_id UUID NOT NULL, 
    slot_no INTEGER NOT NULL, 
    scene_key VARCHAR(200) NOT NULL, 
    form_key VARCHAR(200), 
    content_item_id UUID, 
    bound_at TIMESTAMP WITH TIME ZONE, 
    replaces_slot_id UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_trial_round_slots PRIMARY KEY (id), 
    CONSTRAINT ck_trial_round_slots_bound_at_paired_with_content CHECK ((content_item_id IS NULL) = (bound_at IS NULL)), 
    CONSTRAINT ck_trial_round_slots_scene_key_not_blank CHECK (char_length(btrim(scene_key)) > 0), 
    CONSTRAINT ck_trial_round_slots_form_key_not_blank CHECK (form_key IS NULL OR char_length(btrim(form_key)) > 0), 
    CONSTRAINT ck_trial_round_slots_replaces_not_self CHECK (replaces_slot_id IS NULL OR replaces_slot_id <> id), 
    CONSTRAINT ck_trial_round_slots_slot_no_positive CHECK (slot_no >= 1), 
    CONSTRAINT fk_trial_round_slots_content_item_id_content_items FOREIGN KEY(content_item_id) REFERENCES content_items (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_trial_round_slots_replaces_slot_id_trial_round_slots FOREIGN KEY(replaces_slot_id) REFERENCES trial_round_slots (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_trial_round_slots_round_id_trial_rounds FOREIGN KEY(round_id) REFERENCES trial_rounds (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_trial_round_slots_content_item_id UNIQUE (content_item_id), 
    CONSTRAINT uq_trial_round_slots_replaces_slot_id UNIQUE (replaces_slot_id), 
    CONSTRAINT uq_trial_round_slots_round_id_slot_no UNIQUE (round_id, slot_no)
);

CREATE INDEX ix_trial_round_slots_round_scene ON trial_round_slots (round_id, scene_key);

CREATE OR REPLACE FUNCTION rednote_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION USING
        MESSAGE = TG_TABLE_NAME || ' 是不可变记录，不允许 ' || TG_OP
                  || '（数据结构文档 02 §1）',
        ERRCODE = '23001';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_trial_gate_decisions_immutable
BEFORE UPDATE OR DELETE ON trial_gate_decisions
FOR EACH ROW EXECUTE FUNCTION rednote_reject_mutation();

CREATE OR REPLACE FUNCTION rednote_guard_trial_round_transition() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING
            MESSAGE = 'trial_rounds 只取消、不删除（轮 ' || OLD.id::text || '）',
            ERRCODE = '23001';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
             OR NEW.project_id IS DISTINCT FROM OLD.project_id
             OR NEW.round_no IS DISTINCT FROM OLD.round_no
             OR NEW.kind IS DISTINCT FROM OLD.kind
             OR NEW.plan_version_id IS DISTINCT FROM OLD.plan_version_id
             OR NEW.baseline_id IS DISTINCT FROM OLD.baseline_id
             OR NEW.source_round_id IS DISTINCT FROM OLD.source_round_id
             OR NEW.requested_quantity IS DISTINCT FROM OLD.requested_quantity
             OR NEW.planned_quantity IS DISTINCT FROM OLD.planned_quantity
             OR NEW.limit_applied IS DISTINCT FROM OLD.limit_applied
             OR NEW.limit_source IS DISTINCT FROM OLD.limit_source
             OR NEW.plan_snapshot IS DISTINCT FROM OLD.plan_snapshot
             OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
             OR NEW.request_hash IS DISTINCT FROM OLD.request_hash
             OR NEW.created_by IS DISTINCT FROM OLD.created_by
             OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION USING
            MESSAGE = 'trial_rounds ' || OLD.id::text || ' 除 state/cancel_reason/updated_at/lock_version 外的列不可修改',
            ERRCODE = '23001';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state THEN
        -- MATRIX-BEGIN
        IF NOT ((OLD.state = 'GENERATING' AND NEW.state IN ('CANCELLED', 'REVIEWING'))
             OR (OLD.state = 'PLANNED' AND NEW.state IN ('CANCELLED', 'GENERATING'))
             OR (OLD.state = 'REVIEWING' AND NEW.state IN ('CANCELLED', 'GATE_PASSED', 'GENERATING'))) THEN
            RAISE EXCEPTION USING
                MESSAGE = 'trial_rounds ' || OLD.id::text || ' 的状态迁移 ' || OLD.state || ' -> ' || NEW.state || ' 不在合法迁移矩阵内',
                ERRCODE = '23514';
        END IF;
        -- MATRIX-END
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_trial_rounds_transition_guard
BEFORE UPDATE OR DELETE ON trial_rounds
FOR EACH ROW
EXECUTE FUNCTION rednote_guard_trial_round_transition();

CREATE OR REPLACE FUNCTION rednote_check_trial_round_consistency() RETURNS trigger AS $$
DECLARE
    node_type text;
    node_entity uuid;
    node_project uuid;
    baseline_project uuid;
    source_project uuid;
    source_kind text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT entity_type, entity_id, project_id
          INTO node_type, node_entity, node_project
          FROM version_nodes WHERE id = NEW.plan_version_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                MESSAGE = '试产轮 ' || NEW.id::text || ' 的 PLAN 版本不存在',
                ERRCODE = '23514';
        END IF;
        IF node_type IS DISTINCT FROM 'PLAN'
           OR node_entity IS DISTINCT FROM NEW.project_id
           OR node_project IS DISTINCT FROM NEW.project_id THEN
            RAISE EXCEPTION USING
                MESSAGE = '试产轮 ' || NEW.id::text || ' 钉住的版本不是本项目的 PLAN 版本',
                ERRCODE = '23514';
        END IF;
        SELECT project_id INTO baseline_project
          FROM production_baselines WHERE id = NEW.baseline_id;
        IF NOT FOUND OR baseline_project IS DISTINCT FROM NEW.project_id THEN
            RAISE EXCEPTION USING
                MESSAGE = '试产轮 ' || NEW.id::text || ' 钉住的基线不属于本项目',
                ERRCODE = '23514';
        END IF;
        IF NEW.source_round_id IS NOT NULL THEN
            SELECT project_id, kind INTO source_project, source_kind
              FROM trial_rounds WHERE id = NEW.source_round_id;
            IF NOT FOUND
               OR source_project IS DISTINCT FROM NEW.project_id
               OR source_kind IS DISTINCT FROM 'TRIAL' THEN
                RAISE EXCEPTION USING
                    MESSAGE = '补充轮 ' || NEW.id::text || ' 的源轮必须是同项目的 TRIAL 轮',
                    ERRCODE = '23514';
            END IF;
        END IF;
    END IF;
    IF NEW.state = 'GATE_PASSED' THEN
        IF NOT EXISTS (
            SELECT 1 FROM trial_gate_decisions d WHERE d.round_id = NEW.id
        ) THEN
            RAISE EXCEPTION USING
                MESSAGE = '试产轮 ' || NEW.id::text || ' 处于 GATE_PASSED 但没有关口决定（没有人工决定就到不了该状态）',
                ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_trial_rounds_consistency
AFTER INSERT OR UPDATE ON trial_rounds
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION rednote_check_trial_round_consistency();

CREATE OR REPLACE FUNCTION rednote_guard_trial_round_slot() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING
            MESSAGE = 'trial_round_slots 不允许 DELETE（名额 ' || OLD.id::text || '）',
            ERRCODE = '23001';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
             OR NEW.round_id IS DISTINCT FROM OLD.round_id
             OR NEW.slot_no IS DISTINCT FROM OLD.slot_no
             OR NEW.scene_key IS DISTINCT FROM OLD.scene_key
             OR NEW.form_key IS DISTINCT FROM OLD.form_key
             OR NEW.replaces_slot_id IS DISTINCT FROM OLD.replaces_slot_id
             OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION USING
            MESSAGE = 'trial_round_slots ' || OLD.id::text || ' 的方向/序号/替代关系不可修改',
            ERRCODE = '23001';
    END IF;
    IF (OLD.content_item_id IS NOT NULL
        AND NEW.content_item_id IS DISTINCT FROM OLD.content_item_id)
       OR (OLD.bound_at IS NOT NULL
           AND NEW.bound_at IS DISTINCT FROM OLD.bound_at) THEN
        RAISE EXCEPTION USING
            MESSAGE = 'trial_round_slots ' || OLD.id::text || ' 的内容绑定是 write-once，不可改写',
            ERRCODE = '23001';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_trial_round_slots_guard
BEFORE UPDATE OR DELETE ON trial_round_slots
FOR EACH ROW
EXECUTE FUNCTION rednote_guard_trial_round_slot();

CREATE OR REPLACE FUNCTION rednote_check_trial_round_slot_consistency() RETURNS trigger AS $$
DECLARE
    round_project uuid;
    round_state text;
    content_project uuid;
    replaced_round uuid;
    replaced_scene text;
BEGIN
    SELECT project_id, state INTO round_project, round_state
      FROM trial_rounds WHERE id = NEW.round_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            MESSAGE = '名额 ' || NEW.id::text || ' 所属的轮不存在',
            ERRCODE = '23514';
    END IF;
    IF NEW.content_item_id IS NOT NULL THEN
        SELECT project_id INTO content_project
          FROM content_items WHERE id = NEW.content_item_id;
        IF NOT FOUND OR content_project IS DISTINCT FROM round_project THEN
            RAISE EXCEPTION USING
                MESSAGE = '名额 ' || NEW.id::text || ' 绑定的内容不属于本轮所在项目',
                ERRCODE = '23514';
        END IF;
    END IF;
    IF NEW.replaces_slot_id IS NOT NULL THEN
        SELECT round_id, scene_key INTO replaced_round, replaced_scene
          FROM trial_round_slots WHERE id = NEW.replaces_slot_id;
        IF NOT FOUND
           OR replaced_round IS DISTINCT FROM NEW.round_id
           OR replaced_scene IS DISTINCT FROM NEW.scene_key THEN
            RAISE EXCEPTION USING
                MESSAGE = '名额 ' || NEW.id::text || ' 的替代对象必须在同一轮且方向相同',
                ERRCODE = '23514';
        END IF;
    END IF;
    IF TG_OP = 'INSERT' AND round_state IN ('CANCELLED', 'GATE_PASSED') THEN
        RAISE EXCEPTION USING
            MESSAGE = '轮 ' || NEW.round_id::text || ' 已是终态 ' || round_state || '，不得再加名额',
            ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_trial_round_slots_consistency
AFTER INSERT OR UPDATE ON trial_round_slots
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION rednote_check_trial_round_slot_consistency();

CREATE OR REPLACE FUNCTION rednote_check_trial_gate_decision_consistency() RETURNS trigger AS $$
DECLARE
    round_state text;
BEGIN
    SELECT state INTO round_state FROM trial_rounds WHERE id = NEW.round_id;
    IF NOT FOUND OR round_state IS DISTINCT FROM 'GATE_PASSED' THEN
        RAISE EXCEPTION USING
            MESSAGE = '关口决定 ' || NEW.id::text || ' 所属的轮不是 GATE_PASSED（决定存在而状态未推进）',
            ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_trial_gate_decisions_consistency
AFTER INSERT ON trial_gate_decisions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION rednote_check_trial_gate_decision_consistency();


-- Running upgrade a1f31c0de001 -> b2f32c0de002

CREATE TABLE batches (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    project_id UUID NOT NULL, 
    batch_kind VARCHAR(8) NOT NULL, 
    seq_no INTEGER NOT NULL, 
    quantity INTEGER NOT NULL, 
    state VARCHAR(20) DEFAULT 'SUBMITTED' NOT NULL, 
    plan_version_id UUID NOT NULL, 
    baseline_id UUID NOT NULL, 
    allocation JSONB NOT NULL, 
    limit_applied INTEGER NOT NULL, 
    limit_source VARCHAR(16) NOT NULL, 
    estimated_amount NUMERIC(20, 8), 
    estimated_currency VARCHAR(3), 
    estimate_refs JSONB DEFAULT '[]'::jsonb NOT NULL, 
    idempotency_key VARCHAR(200) NOT NULL, 
    request_hash VARCHAR(64) NOT NULL, 
    cancel_requested_at TIMESTAMP WITH TIME ZONE, 
    cancel_requested_by UUID, 
    created_by UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    lock_version BIGINT DEFAULT 1 NOT NULL, 
    CONSTRAINT pk_batches PRIMARY KEY (id), 
    CONSTRAINT ck_batches_cancel_pair_consistent CHECK ((state = 'CANCEL_REQUESTED') = (cancel_requested_at IS NOT NULL)), 
    CONSTRAINT ck_batches_batch_kind_valid CHECK (batch_kind IN ('BULK', 'SCALE', 'TRIAL')), 
    CONSTRAINT ck_batches_allocation_is_object CHECK (jsonb_typeof(allocation) = 'object'), 
    CONSTRAINT ck_batches_estimate_refs_is_array CHECK (jsonb_typeof(estimate_refs) = 'array'), 
    CONSTRAINT ck_batches_limit_source_valid CHECK (limit_source IN ('GLOBAL', 'INHERITED', 'MANUAL')), 
    CONSTRAINT ck_batches_state_valid CHECK (state IN ('CANCEL_REQUESTED', 'SUBMITTED')), 
    CONSTRAINT ck_batches_estimate_pair_consistent CHECK ((estimated_amount IS NULL) = (estimated_currency IS NULL)), 
    CONSTRAINT ck_batches_cancel_actor_requires_time CHECK (cancel_requested_by IS NULL OR cancel_requested_at IS NOT NULL), 
    CONSTRAINT ck_batches_idempotency_key_not_blank CHECK (char_length(idempotency_key) > 0), 
    CONSTRAINT ck_batches_request_hash_not_blank CHECK (char_length(request_hash) > 0), 
    CONSTRAINT ck_batches_estimated_amount_non_negative CHECK (estimated_amount IS NULL OR estimated_amount >= 0), 
    CONSTRAINT ck_batches_limit_applied_positive CHECK (limit_applied >= 1), 
    CONSTRAINT ck_batches_lock_version_positive CHECK (lock_version >= 1), 
    CONSTRAINT ck_batches_quantity_positive CHECK (quantity > 0), 
    CONSTRAINT ck_batches_seq_no_positive CHECK (seq_no >= 1), 
    CONSTRAINT fk_batches_baseline_id_production_baselines FOREIGN KEY(baseline_id) REFERENCES production_baselines (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_batches_cancel_requested_by_users FOREIGN KEY(cancel_requested_by) REFERENCES users (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_batches_created_by_users FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_batches_plan_version_id_version_nodes FOREIGN KEY(plan_version_id) REFERENCES version_nodes (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_batches_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_batches_project_kind_seq UNIQUE (project_id, batch_kind, seq_no), 
    CONSTRAINT uq_batches_project_idempotency_key UNIQUE (project_id, idempotency_key)
);

CREATE INDEX ix_batches_baseline_id ON batches (baseline_id);

CREATE INDEX ix_batches_plan_version_id ON batches (plan_version_id);

CREATE INDEX ix_batches_project_created ON batches (project_id, created_at);

CREATE TABLE batch_items (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    batch_id UUID NOT NULL, 
    ordinal INTEGER NOT NULL, 
    content_item_id UUID NOT NULL, 
    scene_key VARCHAR(100), 
    form_key VARCHAR(100), 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_batch_items PRIMARY KEY (id), 
    CONSTRAINT ck_batch_items_assignment_paired CHECK ((scene_key IS NULL) = (form_key IS NULL)), 
    CONSTRAINT ck_batch_items_form_key_not_blank CHECK (form_key IS NULL OR char_length(form_key) > 0), 
    CONSTRAINT ck_batch_items_ordinal_positive CHECK (ordinal >= 1), 
    CONSTRAINT ck_batch_items_scene_key_not_blank CHECK (scene_key IS NULL OR char_length(scene_key) > 0), 
    CONSTRAINT fk_batch_items_batch_id_batches FOREIGN KEY(batch_id) REFERENCES batches (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_batch_items_content_item_id_content_items FOREIGN KEY(content_item_id) REFERENCES content_items (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_batch_items_batch_ordinal UNIQUE (batch_id, ordinal), 
    CONSTRAINT uq_batch_items_content_item_id UNIQUE (content_item_id)
);

CREATE TABLE batch_item_runs (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    batch_item_id UUID NOT NULL, 
    round_no INTEGER NOT NULL, 
    job_id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_batch_item_runs PRIMARY KEY (id), 
    CONSTRAINT ck_batch_item_runs_round_no_positive CHECK (round_no >= 1), 
    CONSTRAINT fk_batch_item_runs_batch_item_id_batch_items FOREIGN KEY(batch_item_id) REFERENCES batch_items (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_batch_item_runs_job_id_jobs FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_batch_item_runs_item_round UNIQUE (batch_item_id, round_no), 
    CONSTRAINT uq_batch_item_runs_job_id UNIQUE (job_id)
);

CREATE INDEX ix_jobs_batch_id ON jobs (batch_id) WHERE batch_id IS NOT NULL;

ALTER TABLE jobs ADD CONSTRAINT fk_jobs_batch_id_batches FOREIGN KEY (batch_id) REFERENCES batches (id) ON DELETE RESTRICT NOT VALID;

ALTER TABLE jobs VALIDATE CONSTRAINT fk_jobs_batch_id_batches;

CREATE OR REPLACE FUNCTION rednote_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION USING
        MESSAGE = TG_TABLE_NAME || ' 是不可变记录，不允许 ' || TG_OP
                  || '（数据结构文档 02 §1）',
        ERRCODE = '23001';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_batch_items_immutable
BEFORE UPDATE OR DELETE ON batch_items
FOR EACH ROW EXECUTE FUNCTION rednote_reject_mutation();

CREATE OR REPLACE FUNCTION rednote_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION USING
        MESSAGE = TG_TABLE_NAME || ' 是不可变记录，不允许 ' || TG_OP
                  || '（数据结构文档 02 §1）',
        ERRCODE = '23001';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_batch_item_runs_immutable
BEFORE UPDATE OR DELETE ON batch_item_runs
FOR EACH ROW EXECUTE FUNCTION rednote_reject_mutation();

CREATE OR REPLACE FUNCTION rednote_check_batch_item_count() RETURNS trigger AS $$
DECLARE
    expected integer;
    actual integer;
    target_batch uuid;
BEGIN
    IF TG_TABLE_NAME = 'batches' THEN
        target_batch := NEW.id;
        expected := NEW.quantity;
    ELSE
        target_batch := NEW.batch_id;
        SELECT quantity INTO expected FROM batches WHERE id = target_batch;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                MESSAGE = '批次成员 ' || NEW.id::text || ' 的批次 ' || target_batch::text || ' 不存在',
                ERRCODE = '23514';
        END IF;
    END IF;
    SELECT count(*) INTO actual FROM batch_items WHERE batch_id = target_batch;
    IF actual IS DISTINCT FROM expected THEN
        RAISE EXCEPTION USING
            MESSAGE = '批次 ' || target_batch::text || ' 的 quantity=' || expected::text
                      || '，而成员行数=' || actual::text
                      || '（quantity 与成员行数必须一致）',
            ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_batches_item_count_consistency
AFTER INSERT ON batches
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION rednote_check_batch_item_count();

CREATE OR REPLACE FUNCTION rednote_check_batch_item_count() RETURNS trigger AS $$
DECLARE
    expected integer;
    actual integer;
    target_batch uuid;
BEGIN
    IF TG_TABLE_NAME = 'batches' THEN
        target_batch := NEW.id;
        expected := NEW.quantity;
    ELSE
        target_batch := NEW.batch_id;
        SELECT quantity INTO expected FROM batches WHERE id = target_batch;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                MESSAGE = '批次成员 ' || NEW.id::text || ' 的批次 ' || target_batch::text || ' 不存在',
                ERRCODE = '23514';
        END IF;
    END IF;
    SELECT count(*) INTO actual FROM batch_items WHERE batch_id = target_batch;
    IF actual IS DISTINCT FROM expected THEN
        RAISE EXCEPTION USING
            MESSAGE = '批次 ' || target_batch::text || ' 的 quantity=' || expected::text
                      || '，而成员行数=' || actual::text
                      || '（quantity 与成员行数必须一致）',
            ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_batch_items_count_consistency
AFTER INSERT ON batch_items
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION rednote_check_batch_item_count();

CREATE OR REPLACE FUNCTION rednote_check_batch_item_ownership() RETURNS trigger AS $$
DECLARE
    content_owner uuid;
    batch_owner uuid;
BEGIN
    SELECT project_id INTO content_owner
      FROM content_items WHERE id = NEW.content_item_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            MESSAGE = '批次成员 ' || NEW.id::text || ' 的内容 '
                      || NEW.content_item_id::text || ' 不存在',
            ERRCODE = '23514';
    END IF;
    SELECT project_id INTO batch_owner FROM batches WHERE id = NEW.batch_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            MESSAGE = '批次成员 ' || NEW.id::text || ' 的批次 ' || NEW.batch_id::text || ' 不存在',
            ERRCODE = '23514';
    END IF;
    IF content_owner IS DISTINCT FROM batch_owner THEN
        RAISE EXCEPTION USING
            MESSAGE = '批次成员 ' || NEW.id::text || ' 的内容属于项目 '
                      || COALESCE(content_owner::text, '(未知)')
                      || '，而批次属于项目 '
                      || COALESCE(batch_owner::text, '(未知)')
                      || '（批次只能含本项目的内容）',
            ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_batch_items_ownership_consistency
AFTER INSERT ON batch_items
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION rednote_check_batch_item_ownership();

CREATE OR REPLACE FUNCTION rednote_check_batch_item_run_ownership() RETURNS trigger AS $$
DECLARE
    item_batch uuid;
    item_content uuid;
    job_batch uuid;
    job_content uuid;
BEGIN
    SELECT batch_id, content_item_id INTO item_batch, item_content
      FROM batch_items WHERE id = NEW.batch_item_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            MESSAGE = '执行轮次 ' || NEW.id::text || ' 的成员 '
                      || NEW.batch_item_id::text || ' 不存在',
            ERRCODE = '23514';
    END IF;
    SELECT batch_id, content_id INTO job_batch, job_content
      FROM jobs WHERE id = NEW.job_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            MESSAGE = '执行轮次 ' || NEW.id::text || ' 的任务 ' || NEW.job_id::text || ' 不存在',
            ERRCODE = '23514';
    END IF;
    IF job_batch IS DISTINCT FROM item_batch THEN
        RAISE EXCEPTION USING
            MESSAGE = '执行轮次 ' || NEW.id::text || ' 的任务 batch_id='
                      || COALESCE(job_batch::text, '(空)')
                      || '，而成员属于批次 ' || item_batch::text
                      || '（jobs.batch_id 必须非空且与成员批次一致）',
            ERRCODE = '23514';
    END IF;
    IF job_content IS DISTINCT FROM item_content THEN
        RAISE EXCEPTION USING
            MESSAGE = '执行轮次 ' || NEW.id::text || ' 的任务 content_id='
                      || COALESCE(job_content::text, '(空)')
                      || '，而成员内容是 ' || item_content::text,
            ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_batch_item_runs_ownership_consistency
AFTER INSERT ON batch_item_runs
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION rednote_check_batch_item_run_ownership();


COMMIT;

