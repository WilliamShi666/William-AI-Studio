CREATE TABLE IF NOT EXISTS shadow_clone_model_config (
    config_key TEXT PRIMARY KEY CHECK (config_key = 'global'),
    main_model_name TEXT NOT NULL,
    subagent_model_name TEXT NOT NULL,
    updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
);
