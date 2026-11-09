-- 把共享测试库 rednote_test 补到与迁移 e5f37c0de005(WP37) 一致。
-- 由 alembic 离线模式导出，已去掉对 alembic_version 的 UPDATE（该库不走迁移）。整个文件在一个事务里。
-- 前提：publication_links 为空（2026-10-26 实测 rednote_test 为 0 行）；非空则 ADD COLUMN NOT NULL 会失败，整事务回滚。
BEGIN;

-- Running upgrade d4f33c0de004 -> e5f37c0de005

ALTER TABLE publication_links ADD COLUMN project_id UUID NOT NULL;

CREATE UNIQUE INDEX uq_publication_links_project_canonical ON publication_links (project_id, canonical_url);

ALTER TABLE publication_links ADD CONSTRAINT fk_publication_links_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT;

COMMIT;
