-- Add durable workspace artifact manifest storage for /workspace file continuity.
-- Safe to run multiple times.

CREATE TABLE IF NOT EXISTS "workspace_artifacts" (
  "artifact_id" uuid NOT NULL DEFAULT gen_random_uuid(),
  "project_id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "thread_id" varchar(128) COLLATE "pg_catalog"."default",
  "agent_run_id" varchar(128) COLLATE "pg_catalog"."default",
  "path" text COLLATE "pg_catalog"."default" NOT NULL,
  "storage_backend" varchar(64) COLLATE "pg_catalog"."default" NOT NULL,
  "storage_key" text COLLATE "pg_catalog"."default" NOT NULL,
  "content_type" varchar(255) COLLATE "pg_catalog"."default",
  "size_bytes" int8 NOT NULL DEFAULT 0,
  "sha256" varchar(64) COLLATE "pg_catalog"."default" NOT NULL,
  "source" varchar(64) COLLATE "pg_catalog"."default" NOT NULL,
  "sandbox_id" varchar(128) COLLATE "pg_catalog"."default",
  "created_by_user_id" varchar(128) COLLATE "pg_catalog"."default",
  "metadata" jsonb DEFAULT '{}'::jsonb,
  "created_at" timestamptz(6) DEFAULT now(),
  "updated_at" timestamptz(6) DEFAULT now()
);

ALTER TABLE "workspace_artifacts"
  DROP CONSTRAINT IF EXISTS "workspace_artifacts_project_id_path_key";

CREATE UNIQUE INDEX IF NOT EXISTS "idx_workspace_artifacts_project_current_unique"
  ON "workspace_artifacts" ("project_id", "path")
  WHERE COALESCE(metadata ->> 'workspace_scope', 'project_current') <> 'agent_run';

CREATE UNIQUE INDEX IF NOT EXISTS "idx_workspace_artifacts_agent_run_path_unique"
  ON "workspace_artifacts" ("project_id", "agent_run_id", "path")
  WHERE (metadata ->> 'workspace_scope') = 'agent_run'
    AND "agent_run_id" IS NOT NULL;

ALTER TABLE "workspace_artifacts"
  DROP CONSTRAINT IF EXISTS "fk_workspace_artifacts_project_id";

ALTER TABLE "workspace_artifacts"
  ADD CONSTRAINT "fk_workspace_artifacts_project_id"
  FOREIGN KEY ("project_id") REFERENCES "projects" ("project_id")
  ON DELETE CASCADE ON UPDATE NO ACTION;

CREATE INDEX IF NOT EXISTS "idx_workspace_artifacts_project_id"
  ON "workspace_artifacts" USING btree ("project_id");
CREATE INDEX IF NOT EXISTS "idx_workspace_artifacts_project_path"
  ON "workspace_artifacts" USING btree ("project_id", "path");
CREATE INDEX IF NOT EXISTS "idx_workspace_artifacts_project_updated_at"
  ON "workspace_artifacts" USING btree ("project_id", "updated_at");
CREATE INDEX IF NOT EXISTS "idx_workspace_artifacts_agent_run_id"
  ON "workspace_artifacts" USING btree ("agent_run_id");

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'update_workspace_artifacts_updated_at'
  ) THEN
    CREATE TRIGGER "update_workspace_artifacts_updated_at"
    BEFORE UPDATE ON "workspace_artifacts"
    FOR EACH ROW
    EXECUTE PROCEDURE "public"."update_updated_at"();
  END IF;
END $$;
