-- Add run-scoped workspace artifact uniqueness for Claude SDK local snapshots.
-- Safe to run multiple times.

ALTER TABLE "workspace_artifacts"
  DROP CONSTRAINT IF EXISTS "workspace_artifacts_project_id_path_key";

DROP INDEX IF EXISTS "workspace_artifacts_project_id_path_key";

CREATE UNIQUE INDEX IF NOT EXISTS "idx_workspace_artifacts_project_current_unique"
  ON "workspace_artifacts" ("project_id", "path")
  WHERE COALESCE(metadata ->> 'workspace_scope', 'project_current') <> 'agent_run';

CREATE UNIQUE INDEX IF NOT EXISTS "idx_workspace_artifacts_agent_run_path_unique"
  ON "workspace_artifacts" ("project_id", "agent_run_id", "path")
  WHERE (metadata ->> 'workspace_scope') = 'agent_run'
    AND "agent_run_id" IS NOT NULL;
