"""Прогон миграций (SPEC §6)."""
import logging
import os

log = logging.getLogger('validator.db')


async def apply_migrations(vd, ed, base: str = 'migrations') -> None:
    """Валидаторские — один раз с учётом в schema_migrations; ebay_data — идемпотентно при каждом старте."""
    await vd.execute(
        'create table if not exists schema_migrations('
        'name text primary key, applied_at timestamptz not null default now())')
    applied = {r['name'] for r in await vd.fetch('select name from schema_migrations')}
    vdir = os.path.join(base, 'validator')
    for fname in sorted(os.listdir(vdir)):
        if not fname.endswith('.sql') or fname in applied:
            continue
        async with vd.transaction():
            await vd.execute(open(os.path.join(vdir, fname)).read())
            await vd.execute('insert into schema_migrations(name) values($1)', fname)
        log.info('миграция валидатора применена: %s', fname)

    edir = os.path.join(base, 'ebay_data')
    for fname in sorted(os.listdir(edir)):
        if fname.endswith('.sql'):
            await ed.execute(open(os.path.join(edir, fname)).read())
    log.info('NOTIFY-триггеры ebay_data актуализированы')
