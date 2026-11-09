-- 把共享测试库 rednote_test 补到与迁移 d4f33c0de004(WP33) 一致。
-- 由 alembic 离线模式导出，已去掉对 alembic_version 的 UPDATE（该库不走迁移）。整个文件在一个事务里。
BEGIN;

-- Running upgrade c3f34c0de003 -> d4f33c0de004

CREATE TABLE quality_reports (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    project_id UUID NOT NULL, 
    scope_kind VARCHAR(16) NOT NULL, 
    scope_digest VARCHAR(64) NOT NULL, 
    batch_id UUID, 
    current_version_id UUID, 
    current_revision_no INTEGER DEFAULT 0 NOT NULL, 
    created_by UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    lock_version INTEGER DEFAULT 1 NOT NULL, 
    CONSTRAINT pk_quality_reports PRIMARY KEY (id), 
    CONSTRAINT ck_quality_reports_batch_id_paired CHECK ((scope_kind = 'BATCH') = (batch_id IS NOT NULL)), 
    CONSTRAINT ck_quality_reports_scope_kind_valid CHECK (scope_kind IN ('CONTENT_SET', 'PROJECT', 'BATCH')), 
    CONSTRAINT ck_quality_reports_projection_paired CHECK ((current_version_id IS NULL) = (current_revision_no = 0)), 
    CONSTRAINT ck_quality_reports_scope_digest_length CHECK (char_length(scope_digest) = 64), 
    CONSTRAINT ck_quality_reports_revision_no_non_negative CHECK (current_revision_no >= 0), 
    CONSTRAINT ck_quality_reports_lock_version_positive CHECK (lock_version >= 1), 
    CONSTRAINT fk_quality_reports_created_by_users FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_quality_reports_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_quality_reports_project_id_scope_digest UNIQUE (project_id, scope_digest)
);

CREATE INDEX ix_quality_reports_project_id_updated_at ON quality_reports (project_id, updated_at, id);

CREATE TABLE quality_report_versions (
    id UUID NOT NULL, 
    report_id UUID NOT NULL, 
    revision_no INTEGER NOT NULL, 
    input_fingerprint VARCHAR(64) NOT NULL, 
    algo_version INTEGER NOT NULL, 
    schema_version INTEGER NOT NULL, 
    item_count INTEGER NOT NULL, 
    included_item_count INTEGER NOT NULL, 
    excluded_item_count INTEGER NOT NULL, 
    ok_dimension_count INTEGER NOT NULL, 
    alert_dimension_count INTEGER NOT NULL, 
    not_computed_dimension_count INTEGER NOT NULL, 
    outcome VARCHAR(16) NOT NULL, 
    payload JSONB NOT NULL, 
    payload_hash VARCHAR(64) NOT NULL, 
    started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    finished_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    created_by UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_quality_report_versions PRIMARY KEY (id), 
    CONSTRAINT ck_quality_report_versions_outcome_alerted_equivalence CHECK ((outcome = 'ALERTED') = (alert_dimension_count > 0)), 
    CONSTRAINT ck_quality_report_versions_payload_is_object CHECK (jsonb_typeof(payload) = 'object'), 
    CONSTRAINT ck_quality_report_versions_outcome_valid CHECK (outcome IN ('ALERTED', 'NO_ALERT')), 
    CONSTRAINT ck_quality_report_versions_algo_version_positive CHECK (algo_version >= 1), 
    CONSTRAINT ck_quality_report_versions_fingerprint_length CHECK (char_length(input_fingerprint) = 64), 
    CONSTRAINT ck_quality_report_versions_payload_hash_length CHECK (char_length(payload_hash) = 64), 
    CONSTRAINT ck_quality_report_versions_finished_not_before_started CHECK (finished_at >= started_at), 
    CONSTRAINT ck_quality_report_versions_item_counts_non_negative CHECK (included_item_count >= 0 AND excluded_item_count >= 0), 
    CONSTRAINT ck_quality_report_versions_item_count_sum CHECK (item_count = included_item_count + excluded_item_count), 
    CONSTRAINT ck_quality_report_versions_dimension_count_sum CHECK (ok_dimension_count + alert_dimension_count + not_computed_dimension_count = 7), 
    CONSTRAINT ck_quality_report_versions_dimension_counts_non_negative CHECK (ok_dimension_count >= 0 AND alert_dimension_count >= 0 AND not_computed_dimension_count >= 0), 
    CONSTRAINT ck_quality_report_versions_revision_no_positive CHECK (revision_no >= 1), 
    CONSTRAINT ck_quality_report_versions_schema_version_positive CHECK (schema_version >= 1), 
    CONSTRAINT fk_quality_report_versions_created_by_users FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_quality_report_versions_id_version_nodes FOREIGN KEY(id) REFERENCES version_nodes (id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED, 
    CONSTRAINT fk_quality_report_versions_report_id_quality_reports FOREIGN KEY(report_id) REFERENCES quality_reports (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_quality_report_versions_report_id_revision_no UNIQUE (report_id, revision_no)
);

CREATE INDEX ix_quality_report_versions_report_id_revision_no ON quality_report_versions (report_id, revision_no, id);

ALTER TABLE quality_reports ADD CONSTRAINT fk_quality_reports_current_version_id_quality_report_versions FOREIGN KEY(current_version_id) REFERENCES quality_report_versions (id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE quality_dimension_results (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    report_version_id UUID NOT NULL, 
    dimension VARCHAR(24) NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    not_computed_reason VARCHAR(48), 
    verdict VARCHAR(8), 
    compared_item_count INTEGER, 
    flagged_item_count INTEGER, 
    pair_threshold_permille INTEGER, 
    alert_threshold_permille INTEGER, 
    max_similarity_permille INTEGER, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_quality_dimension_results PRIMARY KEY (id), 
    CONSTRAINT ck_quality_dimension_results_computed_columns_present CHECK ((status = 'COMPUTED') = (compared_item_count IS NOT NULL AND flagged_item_count IS NOT NULL AND pair_threshold_permille IS NOT NULL AND alert_threshold_permille IS NOT NULL AND max_similarity_permille IS NOT NULL AND verdict IS NOT NULL)), 
    CONSTRAINT ck_quality_dimension_results_not_computed_columns_absent CHECK ((status = 'NOT_COMPUTED') = (compared_item_count IS NULL AND flagged_item_count IS NULL AND pair_threshold_permille IS NULL AND alert_threshold_permille IS NULL AND max_similarity_permille IS NULL AND verdict IS NULL)), 
    CONSTRAINT ck_quality_dimension_results_reason_paired CHECK ((status = 'NOT_COMPUTED') = (not_computed_reason IS NOT NULL)), 
    CONSTRAINT ck_quality_dimension_results_dimension_valid CHECK (dimension IN ('BODY', 'ENDING', 'IMAGE_SIMILARITY', 'OPENING', 'SCENE_CONCENTRATION', 'STRUCTURE', 'TITLE')), 
    CONSTRAINT ck_quality_dimension_results_unimplemented_not_computed CHECK (dimension NOT IN ('IMAGE_SIMILARITY', 'SCENE_CONCENTRATION', 'STRUCTURE') OR status = 'NOT_COMPUTED'), 
    CONSTRAINT ck_quality_dimension_results_status_valid CHECK (status IN ('COMPUTED', 'NOT_COMPUTED')), 
    CONSTRAINT ck_quality_dimension_results_verdict_alert_equivalence CHECK (verdict IS NULL OR ((verdict = 'ALERT') = (flagged_item_count * 1000 > alert_threshold_permille * compared_item_count))), 
    CONSTRAINT ck_quality_dimension_results_verdict_valid CHECK (verdict IS NULL OR verdict IN ('ALERT', 'OK')), 
    CONSTRAINT ck_quality_dimension_results_thresholds_range CHECK ((pair_threshold_permille IS NULL OR pair_threshold_permille BETWEEN 0 AND 1000) AND (alert_threshold_permille IS NULL OR alert_threshold_permille BETWEEN 0 AND 1000)), 
    CONSTRAINT ck_quality_dimension_results_compared_at_least_two CHECK (compared_item_count IS NULL OR compared_item_count >= 2), 
    CONSTRAINT ck_quality_dimension_results_flagged_range CHECK (flagged_item_count IS NULL OR (flagged_item_count >= 0 AND flagged_item_count <= compared_item_count)), 
    CONSTRAINT ck_quality_dimension_results_flagged_not_one CHECK (flagged_item_count IS NULL OR flagged_item_count <> 1), 
    CONSTRAINT ck_quality_dimension_results_max_similarity_range CHECK (max_similarity_permille IS NULL OR max_similarity_permille BETWEEN 0 AND 1000), 
    CONSTRAINT fk_quality_dimension_results_report_version_id_versions FOREIGN KEY(report_version_id) REFERENCES quality_report_versions (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_quality_dimension_results_version_dimension UNIQUE (report_version_id, dimension)
);

CREATE TABLE quality_report_artifacts (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    report_version_id UUID NOT NULL, 
    format VARCHAR(16) NOT NULL, 
    file_object_id UUID NOT NULL, 
    content_sha256 VARCHAR(64) NOT NULL, 
    byte_size BIGINT NOT NULL, 
    created_by UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_quality_report_artifacts PRIMARY KEY (id), 
    CONSTRAINT ck_quality_report_artifacts_format_valid CHECK (format IN ('JSON')), 
    CONSTRAINT ck_quality_report_artifacts_byte_size_non_negative CHECK (byte_size >= 0), 
    CONSTRAINT ck_quality_report_artifacts_sha256_length CHECK (char_length(content_sha256) = 64), 
    CONSTRAINT fk_quality_report_artifacts_created_by_users FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_quality_report_artifacts_file_object_id_file_objects FOREIGN KEY(file_object_id) REFERENCES file_objects (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_quality_report_artifacts_report_version_id_versions FOREIGN KEY(report_version_id) REFERENCES quality_report_versions (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_quality_report_artifacts_file_object_id UNIQUE (file_object_id), 
    CONSTRAINT uq_quality_report_artifacts_version_format UNIQUE (report_version_id, format)
);

CREATE TABLE quality_report_items (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    report_version_id UUID NOT NULL, 
    content_item_id UUID NOT NULL, 
    content_version_id UUID, 
    position INTEGER NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    exclude_reason VARCHAR(48), 
    paragraph_count INTEGER, 
    generation_context_id UUID, 
    baseline_id UUID, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_quality_report_items PRIMARY KEY (id), 
    CONSTRAINT ck_quality_report_items_exclude_reason_paired CHECK ((status = 'EXCLUDED') = (exclude_reason IS NOT NULL)), 
    CONSTRAINT ck_quality_report_items_included_has_subject CHECK (status <> 'INCLUDED' OR (content_version_id IS NOT NULL AND paragraph_count IS NOT NULL AND paragraph_count >= 1)), 
    CONSTRAINT ck_quality_report_items_status_valid CHECK (status IN ('EXCLUDED', 'INCLUDED')), 
    CONSTRAINT ck_quality_report_items_paragraph_count_non_negative CHECK (paragraph_count IS NULL OR paragraph_count >= 0), 
    CONSTRAINT ck_quality_report_items_position_non_negative CHECK (position >= 0), 
    CONSTRAINT fk_quality_report_items_baseline_id_production_baselines FOREIGN KEY(baseline_id) REFERENCES production_baselines (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_quality_report_items_content_item_id_content_items FOREIGN KEY(content_item_id) REFERENCES content_items (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_quality_report_items_content_version_id_content_versions FOREIGN KEY(content_version_id) REFERENCES content_versions (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_quality_report_items_generation_context_id FOREIGN KEY(generation_context_id) REFERENCES generation_contexts (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_quality_report_items_report_version_id_versions FOREIGN KEY(report_version_id) REFERENCES quality_report_versions (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_quality_report_items_version_content UNIQUE (report_version_id, content_item_id), 
    CONSTRAINT uq_quality_report_items_version_position UNIQUE (report_version_id, position)
);

CREATE INDEX ix_quality_report_items_content_item_id ON quality_report_items (content_item_id);

CREATE TABLE quality_media_alignments (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    report_version_id UUID NOT NULL, 
    content_item_id UUID NOT NULL, 
    sort_order INTEGER NOT NULL, 
    media_item_id UUID, 
    media_state VARCHAR(20), 
    expected_ref VARCHAR(200), 
    actual_ref VARCHAR(200), 
    verdict VARCHAR(16) NOT NULL, 
    reason VARCHAR(48), 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_quality_media_alignments PRIMARY KEY (id), 
    CONSTRAINT ck_quality_media_alignments_aligned_equivalence CHECK ((verdict = 'ALIGNED') = (expected_ref IS NOT NULL AND actual_ref IS NOT NULL AND expected_ref = actual_ref)), 
    CONSTRAINT ck_quality_media_alignments_misaligned_equivalence CHECK ((verdict = 'MISALIGNED') = (expected_ref IS NOT NULL AND actual_ref IS NOT NULL AND expected_ref <> actual_ref)), 
    CONSTRAINT ck_quality_media_alignments_missing_equivalence CHECK ((verdict = 'MISSING') = (media_item_id IS NULL)), 
    CONSTRAINT ck_quality_media_alignments_not_evaluable_equivalence CHECK ((verdict = 'NOT_EVALUABLE') = (expected_ref IS NULL)), 
    CONSTRAINT ck_quality_media_alignments_reason_paired CHECK ((verdict = 'NOT_EVALUABLE') = (reason IS NOT NULL)), 
    CONSTRAINT ck_quality_media_alignments_verdict_valid CHECK (verdict IN ('ALIGNED', 'MISALIGNED', 'MISSING', 'NOT_EVALUABLE')), 
    CONSTRAINT ck_quality_media_alignments_media_columns_paired CHECK ((media_item_id IS NULL) = (media_state IS NULL) AND (media_item_id IS NULL) = (actual_ref IS NULL)), 
    CONSTRAINT ck_quality_media_alignments_some_fact_present CHECK (expected_ref IS NOT NULL OR media_item_id IS NOT NULL), 
    CONSTRAINT ck_quality_media_alignments_sort_order_non_negative CHECK (sort_order >= 0), 
    CONSTRAINT fk_quality_media_alignments_media_item_id_media_items FOREIGN KEY(media_item_id) REFERENCES media_items (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_quality_media_alignments_item FOREIGN KEY(report_version_id, content_item_id) REFERENCES quality_report_items (report_version_id, content_item_id) ON DELETE RESTRICT, 
    CONSTRAINT uq_quality_media_alignments_version_content_slot UNIQUE (report_version_id, content_item_id, sort_order), 
    CONSTRAINT uq_quality_media_alignments_version_media_item UNIQUE (report_version_id, media_item_id)
);

CREATE TABLE quality_similarity_pairs (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    report_version_id UUID NOT NULL, 
    dimension VARCHAR(24) NOT NULL, 
    content_item_a_id UUID NOT NULL, 
    content_item_b_id UUID NOT NULL, 
    intersection_count INTEGER NOT NULL, 
    union_count INTEGER NOT NULL, 
    similarity_permille INTEGER NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_quality_similarity_pairs PRIMARY KEY (id), 
    CONSTRAINT ck_quality_similarity_pairs_dimension_is_text CHECK (dimension IN ('BODY', 'ENDING', 'OPENING', 'TITLE')), 
    CONSTRAINT ck_quality_similarity_pairs_ordered CHECK (content_item_a_id < content_item_b_id), 
    CONSTRAINT ck_quality_similarity_pairs_permille_derived CHECK (similarity_permille = (intersection_count * 1000) / union_count), 
    CONSTRAINT ck_quality_similarity_pairs_counts_valid CHECK (union_count >= 1 AND intersection_count >= 0 AND intersection_count <= union_count), 
    CONSTRAINT fk_quality_similarity_pairs_item_a FOREIGN KEY(report_version_id, content_item_a_id) REFERENCES quality_report_items (report_version_id, content_item_id) ON DELETE RESTRICT, 
    CONSTRAINT fk_quality_similarity_pairs_item_b FOREIGN KEY(report_version_id, content_item_b_id) REFERENCES quality_report_items (report_version_id, content_item_id) ON DELETE RESTRICT, 
    CONSTRAINT fk_quality_similarity_pairs_dimension_result FOREIGN KEY(report_version_id, dimension) REFERENCES quality_dimension_results (report_version_id, dimension) ON DELETE RESTRICT, 
    CONSTRAINT uq_quality_similarity_pairs_version_dimension_pair UNIQUE (report_version_id, dimension, content_item_a_id, content_item_b_id)
);

CREATE INDEX ix_quality_similarity_pairs_version_dimension_permille ON quality_similarity_pairs (report_version_id, dimension, similarity_permille, id);

CREATE INDEX ix_quality_similarity_pairs_version_item_b ON quality_similarity_pairs (report_version_id, content_item_b_id);

CREATE OR REPLACE FUNCTION rednote_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION USING
        MESSAGE = TG_TABLE_NAME || ' 是不可变记录，不允许 ' || TG_OP
                  || '（数据结构文档 02 §1）',
        ERRCODE = '23001';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_quality_report_versions_immutable
BEFORE UPDATE OR DELETE ON quality_report_versions
FOR EACH ROW EXECUTE FUNCTION rednote_reject_mutation();

CREATE OR REPLACE FUNCTION rednote_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION USING
        MESSAGE = TG_TABLE_NAME || ' 是不可变记录，不允许 ' || TG_OP
                  || '（数据结构文档 02 §1）',
        ERRCODE = '23001';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_quality_report_items_immutable
BEFORE UPDATE OR DELETE ON quality_report_items
FOR EACH ROW EXECUTE FUNCTION rednote_reject_mutation();

CREATE OR REPLACE FUNCTION rednote_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION USING
        MESSAGE = TG_TABLE_NAME || ' 是不可变记录，不允许 ' || TG_OP
                  || '（数据结构文档 02 §1）',
        ERRCODE = '23001';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_quality_dimension_results_immutable
BEFORE UPDATE OR DELETE ON quality_dimension_results
FOR EACH ROW EXECUTE FUNCTION rednote_reject_mutation();

CREATE OR REPLACE FUNCTION rednote_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION USING
        MESSAGE = TG_TABLE_NAME || ' 是不可变记录，不允许 ' || TG_OP
                  || '（数据结构文档 02 §1）',
        ERRCODE = '23001';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_quality_similarity_pairs_immutable
BEFORE UPDATE OR DELETE ON quality_similarity_pairs
FOR EACH ROW EXECUTE FUNCTION rednote_reject_mutation();

CREATE OR REPLACE FUNCTION rednote_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION USING
        MESSAGE = TG_TABLE_NAME || ' 是不可变记录，不允许 ' || TG_OP
                  || '（数据结构文档 02 §1）',
        ERRCODE = '23001';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_quality_media_alignments_immutable
BEFORE UPDATE OR DELETE ON quality_media_alignments
FOR EACH ROW EXECUTE FUNCTION rednote_reject_mutation();

CREATE OR REPLACE FUNCTION rednote_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION USING
        MESSAGE = TG_TABLE_NAME || ' 是不可变记录，不允许 ' || TG_OP
                  || '（数据结构文档 02 §1）',
        ERRCODE = '23001';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_quality_report_artifacts_immutable
BEFORE UPDATE OR DELETE ON quality_report_artifacts
FOR EACH ROW EXECUTE FUNCTION rednote_reject_mutation();

CREATE OR REPLACE FUNCTION rednote_check_quality_report_current_version() RETURNS trigger AS $$
DECLARE
    v_report uuid;
    v_revision integer;
BEGIN
    IF NEW.current_version_id IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT report_id, revision_no INTO v_report, v_revision
      FROM quality_report_versions WHERE id = NEW.current_version_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            MESSAGE = '质量报告 ' || NEW.id::text || ' 的当前版本 ' || NEW.current_version_id::text || ' 不存在',
            ERRCODE = '23514';
    END IF;
    IF v_report IS DISTINCT FROM NEW.id THEN
        RAISE EXCEPTION USING
            MESSAGE = '质量报告 ' || NEW.id::text || ' 的当前版本属于别的报告 ' || COALESCE(v_report::text, '(未知)'),
            ERRCODE = '23514';
    END IF;
    IF v_revision IS DISTINCT FROM NEW.current_revision_no THEN
        RAISE EXCEPTION USING
            MESSAGE = '质量报告 ' || NEW.id::text || ' 的 current_revision_no=' || NEW.current_revision_no::text || '，而当前版本的 revision_no=' || v_revision::text,
            ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_quality_reports_current_version
AFTER INSERT OR UPDATE ON quality_reports
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION rednote_check_quality_report_current_version();

CREATE OR REPLACE FUNCTION rednote_check_quality_report_version_consistency() RETURNS trigger AS $$
DECLARE
    n_type text;
    n_entity uuid;
    n_revision integer;
    n_hash text;
    d_total integer;
    d_ok integer;
    d_alert integer;
    d_nc integer;
    i_inc integer;
    i_exc integer;
BEGIN
    SELECT entity_type, entity_id, revision_no, payload_hash
      INTO n_type, n_entity, n_revision, n_hash
      FROM version_nodes WHERE id = NEW.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            MESSAGE = 'quality_report_versions.id 没有对应的 version_nodes 行：' || NEW.id::text,
            ERRCODE = '23514';
    END IF;
    IF n_type <> 'REPORT' THEN
        RAISE EXCEPTION USING
            MESSAGE = '版本节点 ' || NEW.id::text || ' 的 entity_type 是 ' || n_type || '，不是 REPORT',
            ERRCODE = '23514';
    END IF;
    IF n_entity IS DISTINCT FROM NEW.report_id THEN
        RAISE EXCEPTION USING
            MESSAGE = '版本节点 ' || NEW.id::text || ' 的 entity_id 与 quality_report_versions.report_id 不一致',
            ERRCODE = '23514';
    END IF;
    IF n_revision IS DISTINCT FROM NEW.revision_no THEN
        RAISE EXCEPTION USING
            MESSAGE = '版本节点 ' || NEW.id::text || ' 的 revision_no 与 quality_report_versions 不一致',
            ERRCODE = '23514';
    END IF;
    IF n_hash IS DISTINCT FROM NEW.payload_hash THEN
        RAISE EXCEPTION USING
            MESSAGE = '版本节点 ' || NEW.id::text || ' 的 payload_hash 与 quality_report_versions 不一致',
            ERRCODE = '23514';
    END IF;
    SELECT count(*),
           count(*) FILTER (WHERE verdict = 'OK'),
           count(*) FILTER (WHERE verdict = 'ALERT'),
           count(*) FILTER (WHERE status = 'NOT_COMPUTED')
      INTO d_total, d_ok, d_alert, d_nc
      FROM quality_dimension_results WHERE report_version_id = NEW.id;
    IF d_total <> 7 THEN
        RAISE EXCEPTION USING
            MESSAGE = '报告版本 ' || NEW.id::text || ' 的维度行数是 ' || d_total::text || '，必须恰为 7',
            ERRCODE = '23514';
    END IF;
    IF d_ok <> NEW.ok_dimension_count OR d_alert <> NEW.alert_dimension_count
       OR d_nc <> NEW.not_computed_dimension_count THEN
        RAISE EXCEPTION USING
            MESSAGE = '报告版本 ' || NEW.id::text || ' 的维度汇总计数与 7 个维度行的实际分布不符',
            ERRCODE = '23514';
    END IF;
    SELECT count(*) FILTER (WHERE status = 'INCLUDED'),
           count(*) FILTER (WHERE status = 'EXCLUDED')
      INTO i_inc, i_exc
      FROM quality_report_items WHERE report_version_id = NEW.id;
    IF i_inc <> NEW.included_item_count OR i_exc <> NEW.excluded_item_count THEN
        RAISE EXCEPTION USING
            MESSAGE = '报告版本 ' || NEW.id::text || ' 的纳入 / 排除篇数与 items 行的实际分布不符',
            ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_quality_report_versions_consistency
AFTER INSERT ON quality_report_versions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION rednote_check_quality_report_version_consistency();

CREATE OR REPLACE FUNCTION rednote_check_quality_report_node_paired() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM quality_reports WHERE id = NEW.entity_id) THEN
        RETURN NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM quality_report_versions WHERE id = NEW.id) THEN
        RAISE EXCEPTION USING
            MESSAGE = '版本节点 ' || NEW.id::text || ' 声明属于质量报告 ' || NEW.entity_id::text || '，但没有任何 quality_report_versions 行（孤立节点）',
            ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_version_nodes_report_paired
AFTER INSERT ON version_nodes
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW WHEN (NEW.entity_type = 'REPORT')
EXECUTE FUNCTION rednote_check_quality_report_node_paired();

CREATE OR REPLACE FUNCTION rednote_check_quality_report_item() RETURNS trigger AS $$
DECLARE
    v_owner uuid;
    c_project uuid;
    r_project uuid;
BEGIN
    IF NEW.content_version_id IS NOT NULL THEN
        SELECT content_item_id INTO v_owner
          FROM content_versions WHERE id = NEW.content_version_id;
        IF v_owner IS DISTINCT FROM NEW.content_item_id THEN
            RAISE EXCEPTION USING
                MESSAGE = '报告成员 ' || NEW.id::text || ' 钉死的内容版本不属于内容 ' || NEW.content_item_id::text,
                ERRCODE = '23514';
        END IF;
    END IF;
    SELECT project_id INTO c_project FROM content_items WHERE id = NEW.content_item_id;
    SELECT qr.project_id INTO r_project
      FROM quality_report_versions v JOIN quality_reports qr ON qr.id = v.report_id
     WHERE v.id = NEW.report_version_id;
    IF c_project IS DISTINCT FROM r_project THEN
        RAISE EXCEPTION USING
            MESSAGE = '报告成员 ' || NEW.id::text || ' 的内容属于项目 ' || COALESCE(c_project::text, '(未知)') || '，而报告属于项目 ' || COALESCE(r_project::text, '(未知)'),
            ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_quality_report_items_consistency
AFTER INSERT ON quality_report_items
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION rednote_check_quality_report_item();

CREATE OR REPLACE FUNCTION rednote_check_quality_dimension_flagged() RETURNS trigger AS $$
DECLARE
    actual integer;
BEGIN
    IF NEW.status <> 'COMPUTED' THEN
        RETURN NULL;
    END IF;
    SELECT count(DISTINCT x) INTO actual FROM (
        SELECT content_item_a_id AS x FROM quality_similarity_pairs
         WHERE report_version_id = NEW.report_version_id AND dimension = NEW.dimension
        UNION
        SELECT content_item_b_id FROM quality_similarity_pairs
         WHERE report_version_id = NEW.report_version_id AND dimension = NEW.dimension
    ) s;
    IF actual <> NEW.flagged_item_count THEN
        RAISE EXCEPTION USING
            MESSAGE = '维度 ' || NEW.dimension || ' 的 flagged_item_count=' || NEW.flagged_item_count::text || '，而相似对涉及的去重篇数=' || actual::text,
            ERRCODE = '23514';
    END IF;
    IF NEW.flagged_item_count > 0
       AND NEW.max_similarity_permille < NEW.pair_threshold_permille THEN
        RAISE EXCEPTION USING
            MESSAGE = '维度 ' || NEW.dimension || ' 有被标记的篇目，但最大相似度低于单对阈值',
            ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_quality_dimension_results_flagged
AFTER INSERT ON quality_dimension_results
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION rednote_check_quality_dimension_flagged();

CREATE OR REPLACE FUNCTION rednote_check_quality_pair() RETURNS trigger AS $$
DECLARE
    d_status text;
    d_threshold integer;
BEGIN
    SELECT status, pair_threshold_permille INTO d_status, d_threshold
      FROM quality_dimension_results
     WHERE report_version_id = NEW.report_version_id AND dimension = NEW.dimension;
    IF NOT FOUND OR d_status <> 'COMPUTED' THEN
        RAISE EXCEPTION USING
            MESSAGE = '相似对 ' || NEW.id::text || ' 所在维度 ' || NEW.dimension || ' 不是 COMPUTED（NOT_COMPUTED 维度不得有相似对）',
            ERRCODE = '23514';
    END IF;
    IF NEW.intersection_count * 1000 < d_threshold * NEW.union_count THEN
        RAISE EXCEPTION USING
            MESSAGE = '相似对 ' || NEW.id::text || ' 的相似度低于该维度的单对阈值（只存命中）',
            ERRCODE = '23514';
    END IF;
    IF (SELECT count(*) FROM quality_report_items
         WHERE report_version_id = NEW.report_version_id
           AND content_item_id IN (NEW.content_item_a_id, NEW.content_item_b_id)
           AND status = 'INCLUDED') <> 2 THEN
        RAISE EXCEPTION USING
            MESSAGE = '相似对 ' || NEW.id::text || ' 涉及的两篇必须都是 INCLUDED 的报告成员',
            ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_quality_similarity_pairs_consistency
AFTER INSERT ON quality_similarity_pairs
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION rednote_check_quality_pair();

CREATE OR REPLACE FUNCTION rednote_check_quality_alignment() RETURNS trigger AS $$
DECLARE
    m_owner uuid;
    i_status text;
    i_version uuid;
BEGIN
    IF NEW.media_item_id IS NOT NULL THEN
        SELECT content_item_id INTO m_owner FROM media_items WHERE id = NEW.media_item_id;
        IF m_owner IS DISTINCT FROM NEW.content_item_id THEN
            RAISE EXCEPTION USING
                MESSAGE = '对位行 ' || NEW.id::text || ' 的媒体位不属于内容 ' || NEW.content_item_id::text,
                ERRCODE = '23514';
        END IF;
    END IF;
    SELECT status, content_version_id INTO i_status, i_version
      FROM quality_report_items
     WHERE report_version_id = NEW.report_version_id AND content_item_id = NEW.content_item_id;
    IF NOT FOUND OR i_version IS NULL THEN
        RAISE EXCEPTION USING
            MESSAGE = '对位行 ' || NEW.id::text || ' 所属成员没有内容版本（NO_VERSION 篇目不应有对位行）',
            ERRCODE = '23514';
    END IF;
    IF (i_status = 'INCLUDED') <> (NEW.verdict <> 'NOT_EVALUABLE') THEN
        RAISE EXCEPTION USING
            MESSAGE = '对位行 ' || NEW.id::text || ' 的 verdict 与成员状态矛盾（NOT_EVALUABLE 只出现在被排除的篇目上）',
            ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_quality_media_alignments_consistency
AFTER INSERT ON quality_media_alignments
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION rednote_check_quality_alignment();


COMMIT;

