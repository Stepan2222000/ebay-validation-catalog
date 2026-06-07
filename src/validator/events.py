"""Горячий цикл (SPEC §5.2): курсорные выборки -> кандидаты -> полные группы -> валидация."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .validation import build_units, validate_groups

log = logging.getLogger('validator.events')

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
CURSOR_NAMES = ('items_first_seen', 'catalog_first_seen', 'changes',
                'shipping_updated', 'smart_prices_updated')

# дельта-источники горячего цикла: имя курсора -> запрос (колонка времени — ts)
DELTA_SQL = {
    'items_first_seen':
        'select item_id, first_seen_at as ts from items where first_seen_at > $1',
    'catalog_first_seen':
        'select item_id, first_seen_at as ts from catalog_items where first_seen_at > $1',
    'changes':
        'select item_id, field_id, changed_at as ts from changes where changed_at > $1',
    'shipping_updated':
        'select item_id, updated_at as ts from item_shipping where updated_at > $1',
    'smart_prices_updated':
        'select smart_part_id, updated_at as ts from buying.smart_prices where updated_at > $1',
}

# полные группы: все строки членства каталога по заданным артикулам
FETCH_GROUPS_SQL = """
select ci.article, ci.context_id, ci.item_id, ci.is_active as catalog_active,
       i.title, i.condition, i.price_usd, i.seller_id, i.is_dead, i.first_seen_at,
       s.shipping_cost
from catalog_items ci
join items i using (item_id)
left join item_shipping s on s.item_id = ci.item_id and s.context_id = ci.context_id
where ci.article = any($1::text[])
"""

REPARSE_SQL = """
insert into reparse_tasks(item_id, reason)
select $1, $2
where not exists (select 1 from reparse_tasks where item_id = $1 and done_at is null)
"""

_title_fid: int | None = None  # field_id поля title в словаре fields — статичен, кэшируем


async def get_cursors(vd) -> dict:
    cur = {r['name']: r['pos'] for r in await vd.fetch('select name, pos from cursors')}
    return {n: cur.get(n, EPOCH) for n in CURSOR_NAMES}


async def set_cursors(vd, positions: dict) -> None:
    await vd.executemany(
        'insert into cursors(name, pos) values($1, $2) '
        'on conflict (name) do update set pos = excluded.pos, updated_at = now()',
        list(positions.items()),
    )


async def load_mapping(sm) -> dict:
    """article -> part_id из базы smart (истинное хранилище смартов), public.part_articles."""
    return {r['article']: r['part_id']
            for r in await sm.fetch('select article, part_id from part_articles')}


async def load_prices(pp) -> dict:
    """part_id -> max_buy_price_usd из parts_prices.buying.smart_prices."""
    return {r['smart_part_id']: r['max_buy_price_usd']
            for r in await pp.fetch('select smart_part_id, max_buy_price_usd from buying.smart_prices')}


async def _delta(conn, name: str, curs: dict, ov: timedelta, new_pos: dict) -> list:
    """Дельта одного источника: строки новее закладки (с перекрытием) + сдвиг закладки."""
    rows = await conn.fetch(DELTA_SQL[name], curs[name] - ov)
    if rows:
        new_pos[name] = max(r['ts'] for r in rows)
    return rows


async def _title_field_id(ed) -> int:
    global _title_fid
    if _title_fid is None:
        _title_fid = await ed.fetchval(
            "select field_id from fields where scope = 'item' and name = 'title'")
    return _title_fid


async def run_tick(ed, vd, pp, sm, cfg) -> dict:
    """Один тик: собрать дельту по курсорам, провалидировать затронутые группы.

    Закладки двигаются только в самом конце, после успешной обработки (SPEC §5.2):
    упавший тик будет повторён целиком, повтор безвреден (валидация идемпотентна).
    Каждый запрос смотрит на overlap назад от закладки — защита от транзакций
    парсера, закоммиченных «в прошлое» (now() в Postgres — время начала транзакции).
    """
    # независимые загрузки из трёх разных баз — параллельно
    mapping, prices, curs, title_fid = await asyncio.gather(
        load_mapping(sm), load_prices(pp), get_cursors(vd), _title_field_id(ed))
    ov = timedelta(seconds=cfg.cursor_overlap_sec)

    new_pos: dict = {}
    # дельты ebay_data — последовательно: они на одном соединении, asyncpg
    # не выполняет запросы одного соединения параллельно
    items_rows = await _delta(ed, 'items_first_seen', curs, ov, new_pos)
    catalog_rows = await _delta(ed, 'catalog_first_seen', curs, ov, new_pos)
    changes_rows = await _delta(ed, 'changes', curs, ov, new_pos)
    shipping_rows = await _delta(ed, 'shipping_updated', curs, ov, new_pos)
    price_rows = await _delta(pp, 'smart_prices_updated', curs, ov, new_pos)

    cand = {r['item_id'] for rows in (items_rows, catalog_rows, changes_rows, shipping_rows)
            for r in rows}
    title_changed = {r['item_id'] for r in changes_rows if r['field_id'] == title_fid}
    # смарты, чьи группы затронуты сменой макс. цены
    force_parts = {r['smart_part_id'] for r in price_rows}

    # кандидаты -> затронутые смарты -> ПОЛНЫЕ группы (дедуп считается по всей группе)
    parts: set = set(force_parts)
    if cand:
        rows = await ed.fetch(
            'select distinct article from catalog_items where item_id = any($1::bigint[])',
            list(cand))
        parts |= {mapping[r['article']] for r in rows if r['article'] in mapping}

    stats = {'validated': 0, 'skipped': 0}
    if parts:
        articles = [a for a, p in mapping.items() if p in parts]
        group_rows = await ed.fetch(FETCH_GROUPS_SQL, articles)
        by_part = build_units(group_rows, mapping)
        stats = await validate_groups(vd, by_part, cfg, prices)

    if title_changed:
        await vd.executemany(REPARSE_SQL, [(i, 'title_changed') for i in sorted(title_changed)])

    if new_pos:
        await set_cursors(vd, new_pos)  # только после успешной обработки

    stats['candidates'] = len(cand)
    stats['parts'] = len(parts)
    stats['reparse'] = len(title_changed)
    return stats
