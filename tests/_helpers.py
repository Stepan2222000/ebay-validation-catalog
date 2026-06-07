"""Общий харнес e2e-тестов: креды из .env, конфиг-фабрика, чекер, откат соединений.

Все тесты гоняются против настоящих баз, но пишут только внутри транзакций,
которые откатываются, — базы остаются нетронутыми.
"""
import os
import sys
from contextlib import asynccontextmanager

import asyncpg
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from validator.config import load_config, load_dotenv, load_dsns  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
DSN = load_dsns()

PASS, FAIL = [], []


def check(name, cond, info=''):
    (PASS if cond else FAIL).append(name)
    print(('  ok  ' if cond else '  FAIL') + f' {name}' + (f'  [{info}]' if info and not cond else ''))


def finish():
    print()
    print(f'PASSED {len(PASS)}  FAILED {len(FAIL)}')
    if FAIL:
        print('FAILED:', FAIL)
    assert not FAIL, f'{len(FAIL)} проверок упало'


def write_cfg(checks, blocklist=None, whitelist=None, allowed=('new',)):
    """Конфиг-фабрика: собрать YAML во временный файл и загрузить Config."""
    cfg = {
        'tick_interval_sec': 30, 'cursor_overlap_sec': 60,
        'full_reconcile_interval_sec': 86400, 'reparse_done_retention_days': 7,
        'allowed_conditions': list(allowed),
        'checks': list(checks),
        'rules': {'blocklist': blocklist or [],
                  'whitelist': {'require': 'any', 'words': whitelist or []}},
    }
    path = '/tmp/cfg_validator_test.yaml'
    open(path, 'w').write(yaml.safe_dump(cfg))
    return load_config(path)


@asynccontextmanager
async def rollback_conns(*keys):
    """Соединения по именам DSN, каждое в транзакции; на выходе — общий откат."""
    conns = [await asyncpg.connect(dsn=DSN[k]) for k in keys]
    txs = [c.transaction() for c in conns]
    for t in txs:
        await t.start()
    try:
        yield conns
    finally:
        for t in txs:
            await t.rollback()
        for c in conns:
            await c.close()
        print('rollback ok')


async def vstate(vd) -> dict:
    """item_id -> (status, reject_reasons, revoked_at, articles) из validated_items."""
    return {r['item_id']: (r['status'], list(r['reject_reasons']), r['revoked_at'],
                           list(r['articles']))
            for r in await vd.fetch('select * from validated_items order by item_id')}
