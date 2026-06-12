"""E2e-тест полной сверки (PLAN, этап 5) на реальных базах, всё под откат.

Сценарий «тихой правки»: в ebay_data журнал changes ведут триггеры самой базы
(items_log и др.), поэтому настоящая тихая правка возможна только в обход
триггеров — имитируем это через session_replication_role=replica. Горячий
цикл такого не видит, сверка обязана найти и исправить.
Запуск: python3 tests/test_reconcile.py
"""
import asyncio

from _helpers import check, finish, rollback_conns, write_cfg
from validator.events import run_tick
from validator.reconcile import run_reconcile


async def main():
    cfg = write_cfg(['dedup', 'condition', 'blocklist', 'whitelist', 'price'])
    async with rollback_conns('EBAY_DATA_DSN', 'VALIDATOR_DSN',
                              'PARTS_PRICES_DSN', 'SMART_DSN') as (ed, vd, pp, sm):
        await sm.execute("insert into parts(id,name,articles,is_draft) values "
                         "('smart_99999902','тестовая деталь','{TST-R1}',false),"
                         "('smart_99999903','соседняя деталь','{TST-R2}',false)")
        await pp.execute("insert into buying.smart_prices(smart_part_id,max_buy_price_usd,updated_by,updated_at) values('smart_99999902',100,'e2e-test',now()-interval '1 day')")
        await ed.execute("insert into contexts overriding system value values (1,'EBAY_US','10001')")
        await ed.execute("insert into search_profiles(profile_id,context_id,condition) overriding system value values (91,1,'new')")
        await ed.execute("insert into sellers(seller_id,name,first_seen_at) overriding system value values (1,'s1',now()) on conflict (seller_id) do nothing")
        # 301 — старый item (вне overlap-окна); 302 — свежий И В ДРУГОМ СМАРТЕ:
        # продвигает курсоры, не затягивая группу 301 в горячий цикл
        await ed.execute("""insert into items(item_id,title,condition,price_usd,seller_id,first_seen_at,last_seen_at,is_dead) values
            (301,'Reconcile test impeller','new',50,1,now()-interval '1 day',now(),false),
            (302,'Reconcile fresh impeller','new',60,1,now(),now(),false)""")
        await ed.execute("""insert into catalog_items(article,profile_id,item_id,first_seen_at) values
            ('TST-R1',91,301,now()-interval '1 day'),
            ('TST-R2',91,302,now())""")

        print('=== подготовка: тик валидирует item ===')
        await run_tick(ed,vd,pp,sm,cfg)
        st = await vd.fetchrow("select status from validated_items where item_id=301")
        check('301 одобрен тиком', st['status']=='approved', st)

        print('=== тихая правка: цена меняется мимо журнала и курсоров ===')
        # состарить журнальные записи от наших вставок (они дёргали бы overlap-окно)
        await ed.execute("update changes set changed_at = changed_at - interval '1 day' where item_id in (301,302)")
        # и тестовую смарт-цену: курсор встал ровно на неё, overlap переподбирал бы
        # её каждый тик и форсил ревалидацию группы — сверке не осталось бы работы.
        # touch-триггер smart_prices перетирает updated_at на now() — обходим его
        await pp.execute("set session_replication_role = replica")
        await pp.execute("update buying.smart_prices set updated_at = now() - interval '2 days' where smart_part_id='smart_99999902'")
        await pp.execute("set session_replication_role = origin")
        # сама правка — в обход триггеров (имитация «мимо журнала»)
        await ed.execute("set session_replication_role = replica")
        await ed.execute("update items set price_usd=500 where item_id=301")
        await ed.execute("set session_replication_role = origin")
        s = await run_tick(ed,vd,pp,sm,cfg)
        st = await vd.fetchrow("select status from validated_items where item_id=301")
        check('горячий цикл правку НЕ увидел (item всё ещё approved)', st['status']=='approved', (s, dict(st)))

        print('=== сверка находит и исправляет ===')
        # задачи перепарса для проверки чистки: старая done, свежая done, активная
        await vd.execute("""insert into reparse_tasks(item_id,reason,created_at,taken_at,done_at) values
            (901,'old',now()-interval '30 days',now()-interval '30 days',now()-interval '20 days'),
            (902,'fresh',now(),now(),now()),
            (903,'active',now(),null,null)""")
        total = await run_reconcile(ed,vd,pp,sm,cfg)
        st = await vd.fetchrow("select status, reject_reasons, revoked_at from validated_items where item_id=301")
        check('301 отозван по цене (500>100)', st['status']=='rejected' and list(st['reject_reasons'])==['price'], dict(st))
        check('revoked_at установлен', st['revoked_at'] is not None)
        check('расхождений исправлено >= 1', total['validated']>=1, total)
        # фильтр по тестовым id: тик в этой же транзакции мог создать задачи и для реальных items
        tasks = {r['item_id'] for r in await vd.fetch('select item_id from reparse_tasks where item_id in (901,902,903)')}
        check('старая done-задача удалена, остальные живы', tasks=={902,903}, tasks)
        curs = await vd.fetchval("select pos from cursors where name='last_reconcile_at'")
        check('last_reconcile_at записан', curs is not None)

        print('=== повторная сверка: расхождений нет ===')
        total = await run_reconcile(ed,vd,pp,sm,cfg)
        check('0 исправлений', total['validated']==0, total)

        finish()

asyncio.run(main())
