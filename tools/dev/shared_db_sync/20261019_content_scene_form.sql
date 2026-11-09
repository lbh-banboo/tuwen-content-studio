-- 把共享测试库 rednote_test 补到与迁移 f3a7c9d15b28（content_items 加 scene_key / form_key）一致。
-- 按迁移文件 migrations/versions/20261019_1100_f3a7c9d15b28_content_scene_form_seam.py 的 upgrade() 重建。
-- 同样需要对私有库 rednote_test_wp29 / rednote_test_wp30 各执行一次（它们也有 content_items）。
BEGIN;
ALTER TABLE content_items ADD COLUMN scene_key varchar(200);
ALTER TABLE content_items ADD COLUMN form_key varchar(200);
ALTER TABLE content_items ADD CONSTRAINT ck_content_items_scene_key_not_blank
    CHECK (scene_key IS NULL OR char_length(btrim(scene_key)) > 0);
ALTER TABLE content_items ADD CONSTRAINT ck_content_items_form_key_not_blank
    CHECK (form_key IS NULL OR char_length(btrim(form_key)) > 0);
COMMIT;
