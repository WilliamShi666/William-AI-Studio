-- ====================================================================
-- PostgreSQL Database Schema for FuFanManus
-- Generated: 2025-09-15
-- ====================================================================

-- 禁用外键检查以避免删除顺序问题
SET session_replication_role = replica;

-- 按依赖关系顺序删除表
DROP TABLE IF EXISTS "events" CASCADE;
DROP TABLE IF EXISTS "sessions" CASCADE;
DROP TABLE IF EXISTS "user_states" CASCADE;
DROP TABLE IF EXISTS "app_states" CASCADE;
DROP TABLE IF EXISTS "agent_runs" CASCADE;
DROP TABLE IF EXISTS "agent_versions" CASCADE;
DROP TABLE IF EXISTS "agent_workflows" CASCADE;
DROP TABLE IF EXISTS "workspace_artifacts" CASCADE;
DROP TABLE IF EXISTS "threads" CASCADE;
DROP TABLE IF EXISTS "messages" CASCADE;
DROP TABLE IF EXISTS "projects" CASCADE;
DROP TABLE IF EXISTS "agents" CASCADE;
DROP TABLE IF EXISTS "user_activities" CASCADE;
DROP TABLE IF EXISTS "user_sessions" CASCADE;
DROP TABLE IF EXISTS "refresh_tokens" CASCADE;
DROP TABLE IF EXISTS "oauth_providers" CASCADE;
DROP TABLE IF EXISTS "users" CASCADE;

-- 重新启用外键检查
SET session_replication_role = DEFAULT;

-- 创建必要的序列
CREATE SEQUENCE IF NOT EXISTS agent_runs_id_seq;

-- 创建更新时间触发器函数
CREATE OR REPLACE FUNCTION update_updated_at() RETURNS TRIGGER AS $$ BEGIN NEW.updated_at = now(); RETURN NEW; END; $$ language 'plpgsql';

-- ----------------------------
-- Table structure for users
-- ----------------------------
CREATE TABLE "users" (
  "id" uuid NOT NULL DEFAULT gen_random_uuid(),
  "email" varchar(255) COLLATE "pg_catalog"."default" NOT NULL,
  "password_hash" varchar(255) COLLATE "pg_catalog"."default",
  "name" varchar(255) COLLATE "pg_catalog"."default" NOT NULL,
  "google_id" varchar(255) COLLATE "pg_catalog"."default",
  "provider" varchar(50) COLLATE "pg_catalog"."default" DEFAULT 'local'::character varying,
  "external_id" varchar(255) COLLATE "pg_catalog"."default",
  "avatar_url" varchar(500) COLLATE "pg_catalog"."default",
  "locale" varchar(10) COLLATE "pg_catalog"."default" DEFAULT 'en'::character varying,
  "status" varchar(20) COLLATE "pg_catalog"."default" DEFAULT 'active'::character varying,
  "role" varchar(20) COLLATE "pg_catalog"."default" DEFAULT 'user'::character varying,
  "email_verified" bool DEFAULT false,
  "email_verified_at" timestamptz(6),
  "metadata" jsonb DEFAULT '{}'::jsonb,
  "preferences" jsonb DEFAULT '{}'::jsonb,
  "created_at" timestamptz(6) DEFAULT now(),
  "updated_at" timestamptz(6) DEFAULT now(),
  "last_login_at" timestamptz(6)
);

-- ----------------------------
-- Table structure for agent_runs
-- ----------------------------
CREATE TABLE "agent_runs" (
  "id" int4 NOT NULL DEFAULT nextval('agent_runs_id_seq'::regclass),
  "thread_id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "agent_id" varchar(128) COLLATE "pg_catalog"."default",
  "agent_version_id" varchar(128) COLLATE "pg_catalog"."default",
  "status" varchar(50) COLLATE "pg_catalog"."default" DEFAULT 'running'::character varying,
  "started_at" timestamptz(6) DEFAULT now(),
  "completed_at" timestamptz(6),
  "error" text COLLATE "pg_catalog"."default",
  "metadata" jsonb DEFAULT '{}'::jsonb,
  "created_at" timestamptz(6) DEFAULT now(),
  "updated_at" timestamptz(6) DEFAULT now(),
  "agent_run_id" uuid NOT NULL DEFAULT gen_random_uuid()
);
COMMENT ON TABLE "agent_runs" IS '代理运行表，记录AI代理执行状态和历史';

-- ----------------------------
-- Table structure for agent_versions
-- ----------------------------
CREATE TABLE "agent_versions" (
  "version_id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "agent_id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "version_number" int4 NOT NULL,
  "version_name" varchar(255) COLLATE "pg_catalog"."default" NOT NULL,
  "description" text COLLATE "pg_catalog"."default",
  "system_prompt" text COLLATE "pg_catalog"."default",
  "model" varchar(100) COLLATE "pg_catalog"."default",
  "configured_mcps" jsonb DEFAULT '[]'::jsonb,
  "custom_mcps" jsonb DEFAULT '[]'::jsonb,
  "agentpress_tools" jsonb DEFAULT '{}'::jsonb,
  "is_active" bool DEFAULT true,
  "created_by" varchar(128) COLLATE "pg_catalog"."default",
  "created_at" timestamptz(6) DEFAULT now(),
  "updated_at" timestamptz(6) DEFAULT now(),
  "change_description" text COLLATE "pg_catalog"."default",
  "previous_version_id" varchar(128) COLLATE "pg_catalog"."default",
  "config" jsonb DEFAULT '{}'::jsonb
);
COMMENT ON COLUMN "agent_versions"."version_id" IS '版本唯一标识符';
COMMENT ON COLUMN "agent_versions"."agent_id" IS '所属Agent ID';
COMMENT ON COLUMN "agent_versions"."version_number" IS '版本号';
COMMENT ON COLUMN "agent_versions"."version_name" IS '版本名称';
COMMENT ON COLUMN "agent_versions"."is_active" IS '是否为活跃版本';
COMMENT ON TABLE "agent_versions" IS 'Agent版本表 - 存储Agent的不同版本配置';

-- ----------------------------
-- Table structure for agent_workflows
-- ----------------------------
CREATE TABLE "agent_workflows" (
  "workflow_id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "agent_id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "name" varchar(255) COLLATE "pg_catalog"."default" NOT NULL,
  "description" text COLLATE "pg_catalog"."default",
  "workflow_config" jsonb NOT NULL,
  "is_active" bool DEFAULT true,
  "created_at" timestamptz(6) DEFAULT now(),
  "updated_at" timestamptz(6) DEFAULT now()
);
COMMENT ON TABLE "agent_workflows" IS 'Agent工作流表 - 存储Agent的工作流配置';

-- ----------------------------
-- Table structure for agents
-- ----------------------------
CREATE TABLE "agents" (
  "agent_id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "user_id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "name" varchar(255) COLLATE "pg_catalog"."default" NOT NULL,
  "description" text COLLATE "pg_catalog"."default",
  "system_prompt" text COLLATE "pg_catalog"."default",
  "model" varchar(100) COLLATE "pg_catalog"."default",
  "configured_mcps" jsonb DEFAULT '[]'::jsonb,
  "custom_mcps" jsonb DEFAULT '[]'::jsonb,
  "agentpress_tools" jsonb DEFAULT '{}'::jsonb,
  "is_default" bool DEFAULT false,
  "is_public" bool DEFAULT false,
  "tags" text[] COLLATE "pg_catalog"."default" DEFAULT '{}'::text[],
  "avatar" varchar(500) COLLATE "pg_catalog"."default",
  "avatar_color" varchar(50) COLLATE "pg_catalog"."default",
  "profile_image_url" varchar(500) COLLATE "pg_catalog"."default",
  "current_version_id" varchar(128) COLLATE "pg_catalog"."default",
  "version_count" int4 DEFAULT 1,
  "metadata" jsonb DEFAULT '{}'::jsonb,
  "created_at" timestamptz(6) DEFAULT now(),
  "updated_at" timestamptz(6) DEFAULT now()
);
COMMENT ON COLUMN "agents"."agent_id" IS 'Agent唯一标识符';
COMMENT ON COLUMN "agents"."user_id" IS '所属用户ID';
COMMENT ON COLUMN "agents"."name" IS 'Agent名称';
COMMENT ON COLUMN "agents"."system_prompt" IS '系统提示词';
COMMENT ON COLUMN "agents"."model" IS '使用的模型';
COMMENT ON COLUMN "agents"."configured_mcps" IS '已配置的MCP工具';
COMMENT ON COLUMN "agents"."custom_mcps" IS '自定义MCP工具';
COMMENT ON COLUMN "agents"."agentpress_tools" IS 'AgentPress工具配置';
COMMENT ON COLUMN "agents"."is_default" IS '是否为默认Agent';
COMMENT ON COLUMN "agents"."current_version_id" IS '当前版本ID';
COMMENT ON COLUMN "agents"."version_count" IS '版本数量';
COMMENT ON TABLE "agents" IS 'Agent管理表 - 存储用户的Agent配置';

-- ----------------------------
-- Table structure for app_states
-- ----------------------------
CREATE TABLE "app_states" (
  "app_name" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "state" jsonb NOT NULL,
  "update_time" timestamp(6) NOT NULL
);
COMMENT ON TABLE "app_states" IS 'ADK框架应用级别状态存储';

-- ----------------------------
-- Table structure for events
-- ----------------------------
CREATE TABLE "events" (
  "id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "app_name" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "user_id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "session_id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "invocation_id" varchar(256) COLLATE "pg_catalog"."default",
  "author" varchar(256) COLLATE "pg_catalog"."default",
  "branch" varchar(256) COLLATE "pg_catalog"."default",
  "timestamp" timestamptz(6) DEFAULT now(),
  "content" jsonb,
  "actions" bytea,
  "long_running_tool_ids_json" text COLLATE "pg_catalog"."default",
  "grounding_metadata" jsonb,
  "custom_metadata" jsonb,
  "usage_metadata" jsonb,
  "citation_metadata" jsonb,
  "partial" bool,
  "turn_complete" bool,
  "error_code" varchar(256) COLLATE "pg_catalog"."default",
  "error_message" varchar(1024) COLLATE "pg_catalog"."default",
  "interrupted" bool,
  "input_transcription" jsonb,
  "output_transcription" jsonb
);
COMMENT ON COLUMN "events"."session_id" IS '关联sessions表中的id字段';
COMMENT ON COLUMN "events"."actions" IS '事件操作数据，使用pickle序列化存储';
COMMENT ON TABLE "events" IS 'ADK框架事件记录';

-- ----------------------------
-- Table structure for messages
-- ----------------------------
CREATE TABLE "messages" (
  "message_id" uuid NOT NULL DEFAULT gen_random_uuid(),
  "thread_id" uuid NOT NULL,
  "project_id" uuid NOT NULL,
  "type" varchar(50) COLLATE "pg_catalog"."default" NOT NULL,
  "role" varchar(50) COLLATE "pg_catalog"."default" NOT NULL,
  "content" jsonb NOT NULL,
  "metadata" jsonb DEFAULT '{}'::jsonb,
  "created_at" timestamptz(6) DEFAULT now(),
  "updated_at" timestamptz(6) DEFAULT now(),
  "agent_id" uuid,
  "agent_version_id" uuid,
  "is_llm_message" bool NOT NULL DEFAULT false
);
COMMENT ON COLUMN "messages"."message_id" IS '消息唯一标识符';
COMMENT ON COLUMN "messages"."thread_id" IS '所属线程ID';
COMMENT ON COLUMN "messages"."project_id" IS '所属项目ID';
COMMENT ON COLUMN "messages"."type" IS '消息类型：user, assistant, tool, system, browser_state, image_context';
COMMENT ON COLUMN "messages"."role" IS '消息角色：user, assistant, system';
COMMENT ON COLUMN "messages"."content" IS '消息内容（JSON格式）';
COMMENT ON COLUMN "messages"."metadata" IS '消息元数据（JSON格式）';
COMMENT ON COLUMN "messages"."created_at" IS '创建时间';
COMMENT ON COLUMN "messages"."updated_at" IS '更新时间';
COMMENT ON COLUMN "messages"."agent_id" IS '关联的代理ID';
COMMENT ON COLUMN "messages"."agent_version_id" IS '关联的代理版本ID';
COMMENT ON COLUMN "messages"."is_llm_message" IS '标识消息是否来自LLM (AI助手)';
COMMENT ON TABLE "messages" IS '存储对话消息的表';

-- ----------------------------
-- Table structure for oauth_providers
-- ----------------------------
CREATE TABLE "oauth_providers" (
  "id" uuid NOT NULL DEFAULT gen_random_uuid(),
  "user_id" uuid NOT NULL,
  "provider" varchar(50) COLLATE "pg_catalog"."default" NOT NULL,
  "provider_user_id" varchar(255) COLLATE "pg_catalog"."default" NOT NULL,
  "provider_email" varchar(255) COLLATE "pg_catalog"."default",
  "access_token" text COLLATE "pg_catalog"."default",
  "refresh_token" text COLLATE "pg_catalog"."default",
  "token_expires_at" timestamptz(6),
  "scope" varchar(500) COLLATE "pg_catalog"."default",
  "provider_data" jsonb DEFAULT '{}'::jsonb,
  "created_at" timestamptz(6) DEFAULT now(),
  "updated_at" timestamptz(6) DEFAULT now()
);

-- ----------------------------
-- Table structure for projects
-- ----------------------------
CREATE TABLE "projects" (
  "project_id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "account_id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "name" varchar(255) COLLATE "pg_catalog"."default" NOT NULL,
  "description" text COLLATE "pg_catalog"."default",
  "status" varchar(50) COLLATE "pg_catalog"."default" DEFAULT 'active'::character varying,
  "metadata" jsonb DEFAULT '{}'::jsonb,
  "sandbox" jsonb DEFAULT '{}'::jsonb,
  "created_at" timestamptz(6) DEFAULT now(),
  "updated_at" timestamptz(6) DEFAULT now()
);
COMMENT ON COLUMN "projects"."project_id" IS '项目唯一标识符';
COMMENT ON COLUMN "projects"."account_id" IS '所属用户ID';
COMMENT ON COLUMN "projects"."name" IS '项目名称';
COMMENT ON COLUMN "projects"."description" IS '项目描述';
COMMENT ON COLUMN "projects"."status" IS '项目状态';
COMMENT ON COLUMN "projects"."metadata" IS '项目元数据';
COMMENT ON COLUMN "projects"."sandbox" IS '沙盒配置信息';
COMMENT ON TABLE "projects" IS '项目表 - 存储用户的对话项目';

-- ----------------------------
-- Table structure for workspace_artifacts
-- ----------------------------
CREATE TABLE "workspace_artifacts" (
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
COMMENT ON TABLE "workspace_artifacts" IS '用户可见 /workspace 文件的持久化清单与 blob 指针';
COMMENT ON COLUMN "workspace_artifacts"."project_id" IS '所属项目ID';
COMMENT ON COLUMN "workspace_artifacts"."thread_id" IS '来源线程ID';
COMMENT ON COLUMN "workspace_artifacts"."agent_run_id" IS '来源运行ID';
COMMENT ON COLUMN "workspace_artifacts"."path" IS '标准化后的 /workspace 文件路径';
COMMENT ON COLUMN "workspace_artifacts"."storage_backend" IS '持久化文件后端类型，如 local 或 aliyun_oss';
COMMENT ON COLUMN "workspace_artifacts"."storage_key" IS '持久化文件后端中的对象键';

-- ----------------------------
-- Table structure for refresh_tokens
-- ----------------------------
CREATE TABLE "refresh_tokens" (
  "id" uuid NOT NULL DEFAULT gen_random_uuid(),
  "user_id" uuid NOT NULL,
  "token_hash" varchar(255) COLLATE "pg_catalog"."default" NOT NULL,
  "session_id" uuid,
  "expires_at" timestamptz(6) NOT NULL,
  "created_at" timestamptz(6) DEFAULT now(),
  "revoked_at" timestamptz(6),
  "is_revoked" bool DEFAULT false
);

-- ----------------------------
-- Table structure for sessions
-- ----------------------------
CREATE TABLE "sessions" (
  "app_name" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "user_id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "state" jsonb NOT NULL,
  "create_time" timestamp(6) NOT NULL,
  "update_time" timestamp(6) NOT NULL
);
COMMENT ON COLUMN "sessions"."id" IS '会话ID，与events表中的session_id对应';
COMMENT ON TABLE "sessions" IS 'ADK框架会话管理';

-- ----------------------------
-- Table structure for threads
-- ----------------------------
CREATE TABLE "threads" (
  "thread_id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "project_id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "account_id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "name" varchar(255) COLLATE "pg_catalog"."default",
  "status" varchar(50) COLLATE "pg_catalog"."default" DEFAULT 'active'::character varying,
  "metadata" jsonb DEFAULT '{}'::jsonb,
  "created_at" timestamptz(6) DEFAULT now(),
  "updated_at" timestamptz(6) DEFAULT now()
);
COMMENT ON COLUMN "threads"."thread_id" IS '线程唯一标识符';
COMMENT ON COLUMN "threads"."project_id" IS '所属项目ID';
COMMENT ON COLUMN "threads"."account_id" IS '所属用户ID';
COMMENT ON COLUMN "threads"."name" IS '线程名称';
COMMENT ON COLUMN "threads"."status" IS '线程状态';
COMMENT ON COLUMN "threads"."metadata" IS '线程元数据';
COMMENT ON TABLE "threads" IS '线程表 - 存储项目中的对话线程';

-- ----------------------------
-- Table structure for user_activities
-- ----------------------------
CREATE TABLE "user_activities" (
  "id" uuid NOT NULL DEFAULT gen_random_uuid(),
  "user_id" uuid NOT NULL,
  "session_id" uuid,
  "activity_type" varchar(50) COLLATE "pg_catalog"."default" NOT NULL,
  "activity_data" jsonb DEFAULT '{}'::jsonb,
  "ip_address" inet,
  "user_agent" text COLLATE "pg_catalog"."default",
  "resource" varchar(255) COLLATE "pg_catalog"."default",
  "created_at" timestamptz(6) DEFAULT now()
);

-- ----------------------------
-- Table structure for user_sessions
-- ----------------------------
CREATE TABLE "user_sessions" (
  "id" uuid NOT NULL DEFAULT gen_random_uuid(),
  "user_id" uuid NOT NULL,
  "session_token" varchar(255) COLLATE "pg_catalog"."default" NOT NULL,
  "access_token" varchar(500) COLLATE "pg_catalog"."default",
  "refresh_token" varchar(500) COLLATE "pg_catalog"."default",
  "device_id" varchar(255) COLLATE "pg_catalog"."default",
  "device_type" varchar(50) COLLATE "pg_catalog"."default",
  "user_agent" text COLLATE "pg_catalog"."default",
  "ip_address" inet,
  "location" jsonb,
  "expires_at" timestamptz(6) NOT NULL,
  "last_activity_at" timestamptz(6) DEFAULT now(),
  "created_at" timestamptz(6) DEFAULT now(),
  "is_active" bool DEFAULT true
);

-- ----------------------------
-- Table structure for user_states
-- ----------------------------
CREATE TABLE "user_states" (
  "app_name" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "user_id" varchar(128) COLLATE "pg_catalog"."default" NOT NULL,
  "state" jsonb NOT NULL,
  "update_time" timestamp(6) NOT NULL
);
COMMENT ON TABLE "user_states" IS 'ADK框架用户级别状态存储';

-- ----------------------------
-- 添加主键约束
-- ----------------------------
ALTER TABLE "agent_runs" ADD CONSTRAINT "agent_runs_pkey" PRIMARY KEY ("id");
ALTER TABLE "agent_versions" ADD CONSTRAINT "agent_versions_pkey" PRIMARY KEY ("version_id");
ALTER TABLE "agent_workflows" ADD CONSTRAINT "agent_workflows_pkey" PRIMARY KEY ("workflow_id");
ALTER TABLE "agents" ADD CONSTRAINT "agents_pkey" PRIMARY KEY ("agent_id");
ALTER TABLE "app_states" ADD CONSTRAINT "app_states_pkey" PRIMARY KEY ("app_name");
ALTER TABLE "events" ADD CONSTRAINT "events_pkey" PRIMARY KEY ("id", "app_name", "user_id", "session_id");
ALTER TABLE "messages" ADD CONSTRAINT "messages_pkey" PRIMARY KEY ("message_id");
ALTER TABLE "oauth_providers" ADD CONSTRAINT "oauth_providers_pkey" PRIMARY KEY ("id");
ALTER TABLE "projects" ADD CONSTRAINT "projects_pkey" PRIMARY KEY ("project_id");
ALTER TABLE "refresh_tokens" ADD CONSTRAINT "refresh_tokens_pkey" PRIMARY KEY ("id");
ALTER TABLE "sessions" ADD CONSTRAINT "sessions_pkey" PRIMARY KEY ("app_name", "user_id", "id");
ALTER TABLE "threads" ADD CONSTRAINT "threads_pkey" PRIMARY KEY ("thread_id");
ALTER TABLE "user_activities" ADD CONSTRAINT "user_activities_pkey" PRIMARY KEY ("id");
ALTER TABLE "user_sessions" ADD CONSTRAINT "user_sessions_pkey" PRIMARY KEY ("id");
ALTER TABLE "user_states" ADD CONSTRAINT "user_states_pkey" PRIMARY KEY ("app_name", "user_id");
ALTER TABLE "users" ADD CONSTRAINT "users_pkey" PRIMARY KEY ("id");

-- ----------------------------
-- 创建索引
-- ----------------------------

-- agent_runs 索引
CREATE INDEX "idx_agent_runs_agent_id" ON "agent_runs" USING btree ("agent_id");
CREATE UNIQUE INDEX "idx_agent_runs_agent_run_id" ON "agent_runs" USING btree ("agent_run_id");
CREATE INDEX "idx_agent_runs_created_at" ON "agent_runs" USING btree ("created_at");
CREATE INDEX "idx_agent_runs_started_at" ON "agent_runs" USING btree ("started_at");
CREATE INDEX "idx_agent_runs_status" ON "agent_runs" USING btree ("status");
CREATE INDEX "idx_agent_runs_thread_id" ON "agent_runs" USING btree ("thread_id");

-- agent_versions 索引
CREATE INDEX "idx_agent_versions_agent_id" ON "agent_versions" USING btree ("agent_id");
CREATE INDEX "idx_agent_versions_created_at" ON "agent_versions" USING btree ("created_at");
CREATE INDEX "idx_agent_versions_is_active" ON "agent_versions" USING btree ("is_active");

-- agent_workflows 索引
CREATE INDEX "idx_agent_workflows_agent_id" ON "agent_workflows" USING btree ("agent_id");
CREATE INDEX "idx_agent_workflows_is_active" ON "agent_workflows" USING btree ("is_active");

-- agents 索引
CREATE INDEX "idx_agents_created_at" ON "agents" USING btree ("created_at");
CREATE INDEX "idx_agents_is_default" ON "agents" USING btree ("is_default");
CREATE INDEX "idx_agents_updated_at" ON "agents" USING btree ("updated_at");
CREATE INDEX "idx_agents_user_default" ON "agents" USING btree ("user_id", "is_default");
CREATE INDEX "idx_agents_user_id" ON "agents" USING btree ("user_id");

-- app_states 索引
CREATE INDEX "idx_app_states_app_name" ON "app_states" USING btree ("app_name");
CREATE INDEX "idx_app_states_update_time" ON "app_states" USING btree ("update_time");

-- events 索引
CREATE INDEX "idx_events_app_name_user_id_session_id" ON "events" USING btree ("app_name", "user_id", "session_id");
CREATE INDEX "idx_events_author" ON "events" USING btree ("author");
CREATE INDEX "idx_events_timestamp" ON "events" USING btree ("timestamp");

-- messages 索引
CREATE INDEX "idx_messages_agent_id" ON "messages" USING btree ("agent_id");
CREATE INDEX "idx_messages_agent_thread" ON "messages" USING btree ("agent_id", "thread_id");
CREATE INDEX "idx_messages_agent_version_id" ON "messages" USING btree ("agent_version_id");
CREATE INDEX "idx_messages_created_at" ON "messages" USING btree ("created_at");
CREATE INDEX "idx_messages_is_llm_message" ON "messages" USING btree ("is_llm_message");
CREATE INDEX "idx_messages_project_id" ON "messages" USING btree ("project_id");
CREATE INDEX "idx_messages_thread_id" ON "messages" USING btree ("thread_id");
CREATE INDEX "idx_messages_thread_type" ON "messages" USING btree ("thread_id", "type");
CREATE INDEX "idx_messages_type" ON "messages" USING btree ("type");

-- oauth_providers 索引
CREATE INDEX "idx_oauth_provider_user" ON "oauth_providers" USING btree ("provider", "provider_user_id");
CREATE INDEX "idx_oauth_user_id" ON "oauth_providers" USING btree ("user_id");

-- projects 索引
CREATE INDEX "idx_projects_account_id" ON "projects" USING btree ("account_id");
CREATE INDEX "idx_projects_created_at" ON "projects" USING btree ("created_at");
CREATE INDEX "idx_projects_status" ON "projects" USING btree ("status");
CREATE INDEX "idx_projects_updated_at" ON "projects" USING btree ("updated_at");

-- workspace_artifacts 索引
CREATE INDEX "idx_workspace_artifacts_project_id" ON "workspace_artifacts" USING btree ("project_id");
CREATE INDEX "idx_workspace_artifacts_project_path" ON "workspace_artifacts" USING btree ("project_id", "path");
CREATE INDEX "idx_workspace_artifacts_project_updated_at" ON "workspace_artifacts" USING btree ("project_id", "updated_at");
CREATE INDEX "idx_workspace_artifacts_agent_run_id" ON "workspace_artifacts" USING btree ("agent_run_id");

-- refresh_tokens 索引
CREATE INDEX "idx_refresh_tokens_expires_at" ON "refresh_tokens" USING btree ("expires_at");
CREATE INDEX "idx_refresh_tokens_revoked" ON "refresh_tokens" USING btree ("is_revoked");
CREATE INDEX "idx_refresh_tokens_token_hash" ON "refresh_tokens" USING btree ("token_hash");
CREATE INDEX "idx_refresh_tokens_user_id" ON "refresh_tokens" USING btree ("user_id");

-- sessions 索引
CREATE INDEX "idx_sessions_app_name" ON "sessions" USING btree ("app_name");
CREATE INDEX "idx_sessions_app_name_user_id" ON "sessions" USING btree ("app_name", "user_id");
CREATE INDEX "idx_sessions_app_user" ON "sessions" USING btree ("app_name", "user_id");
CREATE INDEX "idx_sessions_create_time" ON "sessions" USING btree ("create_time");
CREATE INDEX "idx_sessions_id" ON "sessions" USING btree ("id");
CREATE INDEX "idx_sessions_update_time" ON "sessions" USING btree ("update_time");

-- threads 索引
CREATE INDEX "idx_threads_account_id" ON "threads" USING btree ("account_id");
CREATE INDEX "idx_threads_created_at" ON "threads" USING btree ("created_at");
CREATE INDEX "idx_threads_project_id" ON "threads" USING btree ("project_id");
CREATE INDEX "idx_threads_status" ON "threads" USING btree ("status");

-- user_activities 索引
CREATE INDEX "idx_activities_created_at" ON "user_activities" USING btree ("created_at");
CREATE INDEX "idx_activities_type" ON "user_activities" USING btree ("activity_type");
CREATE INDEX "idx_activities_user_id" ON "user_activities" USING btree ("user_id");

-- user_sessions 索引
CREATE INDEX "idx_sessions_active" ON "user_sessions" USING btree ("is_active");
CREATE INDEX "idx_sessions_device_id" ON "user_sessions" USING btree ("device_id");
CREATE INDEX "idx_sessions_expires_at" ON "user_sessions" USING btree ("expires_at");
CREATE INDEX "idx_sessions_token" ON "user_sessions" USING btree ("session_token");
CREATE INDEX "idx_sessions_user_id" ON "user_sessions" USING btree ("user_id");

-- user_states 索引
CREATE INDEX "idx_user_states_app_name" ON "user_states" USING btree ("app_name");
CREATE INDEX "idx_user_states_app_name_user_id" ON "user_states" USING btree ("app_name", "user_id");
CREATE INDEX "idx_user_states_app_user" ON "user_states" USING btree ("app_name", "user_id");
CREATE INDEX "idx_user_states_update_time" ON "user_states" USING btree ("update_time");
CREATE INDEX "idx_user_states_user_id" ON "user_states" USING btree ("user_id");

-- users 索引
CREATE INDEX "idx_users_created_at" ON "users" USING btree ("created_at");
CREATE INDEX "idx_users_email" ON "users" USING btree ("email");
CREATE INDEX "idx_users_google_id" ON "users" USING btree ("google_id");
CREATE INDEX "idx_users_provider" ON "users" USING btree ("provider");
CREATE INDEX "idx_users_role" ON "users" USING btree ("role");
CREATE INDEX "idx_users_status" ON "users" USING btree ("status");

-- ----------------------------
-- 唯一约束
-- ----------------------------
ALTER TABLE "oauth_providers" ADD CONSTRAINT "oauth_providers_provider_provider_user_id_key" UNIQUE ("provider", "provider_user_id");
ALTER TABLE "refresh_tokens" ADD CONSTRAINT "refresh_tokens_token_hash_key" UNIQUE ("token_hash");
ALTER TABLE "user_sessions" ADD CONSTRAINT "user_sessions_session_token_key" UNIQUE ("session_token");
ALTER TABLE "users" ADD CONSTRAINT "users_email_key" UNIQUE ("email");
ALTER TABLE "users" ADD CONSTRAINT "users_google_id_key" UNIQUE ("google_id");
ALTER TABLE "workspace_artifacts" ADD CONSTRAINT "workspace_artifacts_project_id_path_key" UNIQUE ("project_id", "path");

-- ----------------------------
-- 检查约束
-- ----------------------------
ALTER TABLE "user_activities" ADD CONSTRAINT "valid_activity_type" CHECK (activity_type::text = ANY (ARRAY['login'::character varying, 'logout'::character varying, 'register'::character varying, 'password_change'::character varying, 'email_verify'::character varying, 'profile_update'::character varying, 'session_expire'::character varying]::text[]));
ALTER TABLE "user_sessions" ADD CONSTRAINT "valid_device_type" CHECK (device_type::text = ANY (ARRAY['web'::character varying, 'mobile'::character varying, 'desktop'::character varying, 'unknown'::character varying]::text[]));
ALTER TABLE "users" ADD CONSTRAINT "valid_provider" CHECK (provider::text = ANY (ARRAY['local'::character varying, 'google'::character varying, 'github'::character varying, 'microsoft'::character varying]::text[]));
ALTER TABLE "users" ADD CONSTRAINT "users_role_check" CHECK (role::text = ANY (ARRAY['admin'::character varying, 'user'::character varying]::text[]));
ALTER TABLE "users" ADD CONSTRAINT "valid_status" CHECK (status::text = ANY (ARRAY['active'::character varying, 'inactive'::character varying, 'suspended'::character varying]::text[]));

-- ----------------------------
-- 触发器
-- ----------------------------
CREATE TRIGGER "update_oauth_providers_updated_at" BEFORE UPDATE ON "oauth_providers" FOR EACH ROW EXECUTE PROCEDURE "public"."update_updated_at"();
CREATE TRIGGER "update_users_updated_at" BEFORE UPDATE ON "users" FOR EACH ROW EXECUTE PROCEDURE "public"."update_updated_at"();
CREATE TRIGGER "update_workspace_artifacts_updated_at" BEFORE UPDATE ON "workspace_artifacts" FOR EACH ROW EXECUTE PROCEDURE "public"."update_updated_at"();

-- ----------------------------
-- 外键约束
-- ----------------------------
ALTER TABLE "agent_versions" ADD CONSTRAINT "fk_agent_versions_agent_id" FOREIGN KEY ("agent_id") REFERENCES "agents" ("agent_id") ON DELETE CASCADE ON UPDATE NO ACTION;
ALTER TABLE "agent_workflows" ADD CONSTRAINT "fk_agent_workflows_agent_id" FOREIGN KEY ("agent_id") REFERENCES "agents" ("agent_id") ON DELETE CASCADE ON UPDATE NO ACTION;
ALTER TABLE "events" ADD CONSTRAINT "events_app_name_user_id_session_id_fkey" FOREIGN KEY ("app_name", "user_id", "session_id") REFERENCES "sessions" ("app_name", "user_id", "id") ON DELETE CASCADE ON UPDATE NO ACTION;
ALTER TABLE "oauth_providers" ADD CONSTRAINT "oauth_providers_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users" ("id") ON DELETE CASCADE ON UPDATE NO ACTION;
ALTER TABLE "refresh_tokens" ADD CONSTRAINT "refresh_tokens_session_id_fkey" FOREIGN KEY ("session_id") REFERENCES "user_sessions" ("id") ON DELETE CASCADE ON UPDATE NO ACTION;
ALTER TABLE "refresh_tokens" ADD CONSTRAINT "refresh_tokens_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users" ("id") ON DELETE CASCADE ON UPDATE NO ACTION;
ALTER TABLE "threads" ADD CONSTRAINT "fk_threads_project_id" FOREIGN KEY ("project_id") REFERENCES "projects" ("project_id") ON DELETE CASCADE ON UPDATE NO ACTION;
ALTER TABLE "user_activities" ADD CONSTRAINT "user_activities_session_id_fkey" FOREIGN KEY ("session_id") REFERENCES "user_sessions" ("id") ON DELETE SET NULL ON UPDATE NO ACTION;
ALTER TABLE "user_activities" ADD CONSTRAINT "user_activities_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users" ("id") ON DELETE CASCADE ON UPDATE NO ACTION;
ALTER TABLE "user_sessions" ADD CONSTRAINT "user_sessions_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users" ("id") ON DELETE CASCADE ON UPDATE NO ACTION;
ALTER TABLE "workspace_artifacts" ADD CONSTRAINT "fk_workspace_artifacts_project_id" FOREIGN KEY ("project_id") REFERENCES "projects" ("project_id") ON DELETE CASCADE ON UPDATE NO ACTION;
