-- Passive NOTIFY triggers in ebay_data (SPEC §5.2): wake the validator when there
-- may be new work. Statement-level — one notification per statement regardless of
-- row count; identical payloads within a transaction are collapsed by Postgres.
-- Idempotent (CREATE OR REPLACE); applied on every service start (SPEC §6).
--
-- Known gap: inserts that target a partition of `changes` directly (bypassing the
-- parent) do not fire the parent's statement-level trigger — covered by the
-- timer-driven tick (SPEC §5.2).

create or replace function validator_notify() returns trigger language plpgsql as $$
begin
    perform pg_notify('validator_events', json_build_object('tbl', TG_TABLE_NAME, 'op', TG_OP)::text);
    return null;
end $$;

create or replace trigger validator_notify_items
    after insert on items
    for each statement execute function validator_notify();

create or replace trigger validator_notify_catalog
    after insert on catalog_items
    for each statement execute function validator_notify();

create or replace trigger validator_notify_changes
    after insert on changes
    for each statement execute function validator_notify();

create or replace trigger validator_notify_shipping
    after insert or update on item_shipping
    for each statement execute function validator_notify();
