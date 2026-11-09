-- 把共享测试库 rednote_test 补到与迁移 c3f34c0de003(WP34) 一致。
-- 由 alembic 离线模式导出，已去掉对 alembic_version 的 UPDATE（该库不走迁移）。整个文件在一个事务里。
BEGIN;

-- Running upgrade b2f32c0de002 -> c3f34c0de003

CREATE TABLE export_validity_events (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    export_item_id UUID NOT NULL, 
    seq INTEGER NOT NULL, 
    transition VARCHAR(12) NOT NULL, 
    reason VARCHAR(24), 
    observed_version_id UUID NOT NULL, 
    observed_eligibility_hash VARCHAR(64) NOT NULL, 
    scope_kind VARCHAR(16) NOT NULL, 
    source_version_id UUID, 
    created_by UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    CONSTRAINT pk_export_validity_events PRIMARY KEY (id), 
    CONSTRAINT ck_export_validity_events_scope_source_paired CHECK ((scope_kind = 'FROM_VERSION') = (source_version_id IS NOT NULL)), 
    CONSTRAINT ck_export_validity_events_reason_paired CHECK ((transition = 'INVALIDATED') = (reason IS NOT NULL)), 
    CONSTRAINT ck_export_validity_events_observed_hash_shape CHECK (char_length(observed_eligibility_hash) = 64 AND observed_eligibility_hash ~ '^[0-9a-f]+$'), 
    CONSTRAINT ck_export_validity_events_reason_valid CHECK (reason IS NULL OR reason IN ('ASSET_CHANGED', 'VERSION_SUPERSEDED')), 
    CONSTRAINT ck_export_validity_events_scope_kind_valid CHECK (scope_kind IN ('CONTENTS', 'FROM_VERSION')), 
    CONSTRAINT ck_export_validity_events_transition_valid CHECK (transition IN ('INVALIDATED', 'RESTORED')), 
    CONSTRAINT ck_export_validity_events_seq_positive CHECK (seq >= 1), 
    CONSTRAINT fk_export_validity_events_created_by_users FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_export_validity_events_export_item_id_export_items FOREIGN KEY(export_item_id) REFERENCES export_items (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_export_validity_events_observed_version_id_version_nodes FOREIGN KEY(observed_version_id) REFERENCES version_nodes (id) ON DELETE RESTRICT, 
    CONSTRAINT fk_export_validity_events_source_version_id_version_nodes FOREIGN KEY(source_version_id) REFERENCES version_nodes (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_export_validity_events_export_item_id_seq UNIQUE (export_item_id, seq)
);

CREATE OR REPLACE FUNCTION rednote_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION USING
        MESSAGE = TG_TABLE_NAME || ' 是不可变记录，不允许 ' || TG_OP
                  || '（数据结构文档 02 §1）',
        ERRCODE = '23001';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_export_validity_events_immutable
BEFORE UPDATE OR DELETE ON export_validity_events
FOR EACH ROW EXECUTE FUNCTION rednote_reject_mutation();

CREATE OR REPLACE FUNCTION rednote_check_export_validity_event() RETURNS trigger AS $$
DECLARE
    frozen_version uuid;
    frozen_hash varchar(64);
    prev_seq integer;
    prev_transition varchar(12);
BEGIN
    SELECT version_node_id, eligibility_hash INTO frozen_version, frozen_hash
      FROM export_items WHERE id = NEW.export_item_id;
    SELECT seq, transition INTO prev_seq, prev_transition
      FROM export_validity_events
     WHERE export_item_id = NEW.export_item_id AND id <> NEW.id
     ORDER BY seq DESC LIMIT 1;
    IF NOT FOUND THEN
        IF NEW.seq <> 1 OR NEW.transition <> 'INVALIDATED' THEN
            RAISE EXCEPTION USING
                MESSAGE = '导出行 ' || NEW.export_item_id::text
                          || ' 的首条事件必须是 seq=1 的 INVALIDATED',
                ERRCODE = '23514';
        END IF;
    ELSIF NEW.seq <> prev_seq + 1 OR NEW.transition = prev_transition THEN
        RAISE EXCEPTION USING
            MESSAGE = '导出行 ' || NEW.export_item_id::text
                      || ' 的事件序号不连续或未交替（上一条 seq=' || prev_seq::text || '）',
            ERRCODE = '23514';
    END IF;
    IF NEW.transition = 'INVALIDATED' THEN
        IF NEW.observed_eligibility_hash = frozen_hash THEN
            RAISE EXCEPTION USING
                MESSAGE = 'INVALIDATED 事件的观测指纹与冻结指纹相同',
                ERRCODE = '23514';
        END IF;
        IF NEW.reason = 'VERSION_SUPERSEDED' AND NEW.observed_version_id = frozen_version THEN
            RAISE EXCEPTION USING
                MESSAGE = 'VERSION_SUPERSEDED 但观测版本仍等于冻结版本',
                ERRCODE = '23514';
        END IF;
        IF NEW.reason = 'ASSET_CHANGED' AND NEW.observed_version_id <> frozen_version THEN
            RAISE EXCEPTION USING
                MESSAGE = 'ASSET_CHANGED 但观测版本已不是冻结版本',
                ERRCODE = '23514';
        END IF;
    ELSE
        IF NEW.observed_eligibility_hash <> frozen_hash
           OR NEW.observed_version_id <> frozen_version THEN
            RAISE EXCEPTION USING
                MESSAGE = 'RESTORED 事件的观测值未回到冻结值',
                ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_export_validity_events_consistency
AFTER INSERT ON export_validity_events
FOR EACH ROW
EXECUTE FUNCTION rednote_check_export_validity_event();


COMMIT;

