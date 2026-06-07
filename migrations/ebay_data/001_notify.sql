-- Пассивные NOTIFY-триггеры в ebay_data (SPEC §5.2): будят валидатор, когда
-- может появиться работа. Statement-level — одна нотификация на statement
-- независимо от числа строк; одинаковые payload внутри транзакции Postgres
-- схлопывает сам. Идемпотентно (CREATE OR REPLACE); применяется при каждом
-- старте сервиса (SPEC §6).
--
-- Известная дыра: вставка напрямую в партицию `changes` мимо родителя
-- statement-триггер родителя не зажигает — закрыто тиком по таймеру (SPEC §5.2).

-- payload пуст: нотификация — только сигнал «проснись», слушатель её содержимое
-- не читает (вся дельта добирается курсорными запросами)
create or replace function validator_notify() returns trigger language plpgsql as $$
begin
    perform pg_notify('validator_events', '');
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
