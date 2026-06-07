"""Точка входа сервиса: миграции, LISTEN, цикл тиков, graceful shutdown."""
import asyncio
import logging
import os
import signal
import time

import asyncpg

from validator.config import load_config, load_dotenv, load_dsns
from validator import db as dbmod
from validator.events import run_tick

log = logging.getLogger('validator')


async def listener_task(dsn: str, wake: asyncio.Event, stop: asyncio.Event) -> None:
    """Выделенное соединение под LISTEN + периодический health-check.

    asyncpg не замечает обрыв соединения, пока не выполнит запрос, поэтому
    каждые 30 сек делаем `select 1`; упало — переподключаемся. Потеря
    нотификаций не страшна: тик по таймеру всё равно случится (SPEC §5.2).
    """
    while not stop.is_set():
        conn = None
        try:
            conn = await asyncpg.connect(dsn)
            await conn.add_listener('validator_events',
                                    lambda *_: wake.set())
            log.info('LISTEN validator_events установлен')
            while not stop.is_set():
                await asyncio.sleep(30)
                await conn.execute('select 1', timeout=10)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning('слушатель: %s — переподключение через 3 сек', e)
            await asyncio.sleep(3)
        finally:
            if conn is not None and not conn.is_closed():
                await conn.close()


async def amain() -> None:
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    load_dotenv()
    cfg = load_config(os.environ.get("VALIDATOR_CONFIG", "config.yaml"))
    dsns = load_dsns()

    stop, wake = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: (stop.set(), wake.set()))

    conns: dict = {}

    async def conn(key: str):
        c = conns.get(key)
        if c is None or c.is_closed():
            conns[key] = c = await asyncpg.connect(dsns[key])
        return c

    await dbmod.apply_migrations(await conn('VALIDATOR_DSN'), await conn('EBAY_DATA_DSN'))
    log.info('миграции применены; конфиг: checks=%s, тик=%s сек',
             list(cfg.checks), cfg.tick_interval_sec)

    lt = asyncio.create_task(listener_task(dsns['EBAY_DATA_DSN'], wake, stop))

    while not stop.is_set():
        # clear ДО тика: NOTIFY, пришедший во время тика, снова взведёт флаг,
        # и следующий тик начнётся сразу, а не по таймеру
        wake.clear()
        try:
            tick_started = time.monotonic()
            s = await run_tick(await conn('EBAY_DATA_DSN'), await conn('VALIDATOR_DSN'),
                               await conn('PARTS_PRICES_DSN'), await conn('SMART_DSN'),
                               cfg)
            if s['candidates'] or s['validated'] or s['reparse']:
                log.info('тик: кандидатов=%s, групп=%s, ревалидаций=%s, скипов=%s, перепарс=%s',
                         s['candidates'], s['parts'], s['validated'], s['skipped'], s['reparse'])
            log.debug('тик занял %.2fs', time.monotonic() - tick_started)
        except Exception:
            log.exception('тик упал — курсоры не сдвинуты, событие будет повторено')

        try:
            await asyncio.wait_for(wake.wait(), timeout=cfg.tick_interval_sec)
            if not stop.is_set():
                log.info('NOTIFY: внеочередной тик')
        except TimeoutError:
            pass

    lt.cancel()
    for c in conns.values():
        if not c.is_closed():
            await c.close()
    log.info('остановлен по сигналу')


def main() -> None:
    asyncio.run(amain())


if __name__ == '__main__':
    main()
