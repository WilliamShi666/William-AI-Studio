create table if not exists qa_sessions (
    session_id uuid primary key default gen_random_uuid(),
    account_id uuid not null references users(id) on delete cascade,
    title varchar(500),
    model_name varchar(100),
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create table if not exists qa_messages (
    message_id uuid primary key default gen_random_uuid(),
    session_id uuid not null references qa_sessions(session_id) on delete cascade,
    role varchar(20) not null,
    content text not null,
    images jsonb,
    files jsonb,
    reasoning_content text,
    metadata jsonb,
    created_at timestamptz default now()
);

create index if not exists idx_qa_sessions_account on qa_sessions(account_id);
create index if not exists idx_qa_sessions_updated on qa_sessions(updated_at desc);
create index if not exists idx_qa_messages_session on qa_messages(session_id);
create index if not exists idx_qa_messages_created on qa_messages(created_at);
