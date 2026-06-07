"""Полная сверка (SPEC §5.3): страховочный проход всего каталога + чистка задач.

Сверка — это просто валидация всех групп чанками: совпавший отпечаток внутри
ядра и есть «сравнение», расхождение автоматически ведёт к ревалидации.
Чанки режутся по границам смарт-артикулов — группа дедупа никогда не рвётся.
"""
import logging

from .events import FETCH_GROUPS_SQL, load_mapping, load_prices, set_cursors
from .validation import build_units, validate_groups

log = logging.getLogger('validator.reconcile')

PARTS_PER_CHUNK = 200  # ~сотни объявлений на смарт -> десятки тысяч строк на чанк


async def run_reconcile(ed, vd, pp, sm, cfg) -> dict:
    mapping = await load_mapping(sm)
    prices = await load_prices(pp)

    by_part_articles: dict = {}
    for article, part_id in mapping.items():
        by_part_articles.setdefault(part_id, []).append(article)
    parts = sorted(by_part_articles)

    total = {'validated': 0, 'skipped': 0, 'parts': len(parts)}
    for i in range(0, len(parts), PARTS_PER_CHUNK):
        chunk = parts[i:i + PARTS_PER_CHUNK]
        articles = [a for p in chunk for a in by_part_articles[p]]
        rows = await ed.fetch(FETCH_GROUPS_SQL, articles)
        if not rows:
            continue
        stats = await validate_groups(vd, build_units(rows, mapping), cfg, prices,
                                      bump_unchanged=False)
        total['validated'] += stats['validated']
        total['skipped'] += stats['skipped']

    # чистка выполненных задач перепарса старше retention
    deleted = await vd.execute(
        "delete from reparse_tasks where done_at is not null "
        "and done_at < now() - $1 * interval '1 day'",
        cfg.reparse_done_retention_days)
    total['tasks_deleted'] = int(deleted.split()[-1])

    await set_cursors(vd, {'last_reconcile_at': await vd.fetchval('select now()')})
    log.info('сверка: смартов=%s, расхождений исправлено=%s, без изменений=%s, задач удалено=%s',
             total['parts'], total['validated'], total['skipped'], total['tasks_deleted'])
    return total
