-- Схема валидатора (база ebay_validation_catalog). Колонки — по SPEC §6.

create table if not exists validated_items (
    item_id            bigint      not null,
    part_id            text        not null,
    context_id         smallint    not null,
    articles           text[]      not null default '{}',
    seller_id          integer,
    title              text,
    title_norm         text,
    condition          text,
    price_usd          numeric,
    shipping_cost      numeric,
    max_buy_price_usd  numeric,
    status             text        not null check (status in ('pending', 'approved', 'rejected')),
    reject_reasons     text[]      not null default '{}',
    fingerprint        bytea,
    revoked_at         timestamptz,
    first_validated_at timestamptz not null default now(),
    validated_at       timestamptz not null default now(),
    last_checked_at    timestamptz not null default now(),
    primary key (item_id, part_id, context_id)
);

create index if not exists idx_vi_part_seller  on validated_items (part_id, seller_id);
create index if not exists idx_vi_part_title   on validated_items (part_id, title_norm);
create index if not exists idx_vi_status       on validated_items (status);
create index if not exists idx_vi_validated_at on validated_items (validated_at);

create table if not exists reparse_tasks (
    task_id    bigserial primary key,
    item_id    bigint      not null,
    reason     text        not null,
    created_at timestamptz not null default now(),
    taken_at   timestamptz,
    done_at    timestamptz
);

-- активные задачи (для дедупа при постановке) и очередь для парсера
create index if not exists idx_rt_active on reparse_tasks (item_id) where done_at is null;
create index if not exists idx_rt_queue  on reparse_tasks (task_id) where taken_at is null;

create table if not exists cursors (
    name       text        primary key,
    pos        timestamptz not null,
    updated_at timestamptz not null default now()
);
