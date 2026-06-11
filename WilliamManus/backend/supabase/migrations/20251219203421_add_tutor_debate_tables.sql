create table if not exists tutor_sessions (
    session_id uuid primary key default gen_random_uuid(),
    account_id uuid not null references users(id),
    collection_name varchar(255) not null,
    title varchar(500),
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create table if not exists tutor_messages (
    message_id uuid primary key default gen_random_uuid(),
    session_id uuid not null references tutor_sessions(session_id) on delete cascade,
    role varchar(20) not null,
    content text not null,
    sources jsonb,
    metadata jsonb,
    created_at timestamptz default now()
);

create table if not exists debate_sessions (
    session_id uuid primary key default gen_random_uuid(),
    account_id uuid not null references users(id),
    motion text not null,
    title varchar(500),
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create table if not exists debate_messages (
    message_id uuid primary key default gen_random_uuid(),
    session_id uuid not null references debate_sessions(session_id) on delete cascade,
    round int not null,
    side varchar(20) not null,
    content text not null,
    metadata jsonb,
    created_at timestamptz default now()
);
