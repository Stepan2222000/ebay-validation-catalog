"""E2e-тест горячего цикла (PLAN, этап 4): 7 сценариев на реальных базах.

Все записи — в транзакциях с откатом, базы остаются нетронутыми.
Синтетический смарт-артикул вставляется напрямую в базу smart (sync-триггер
там сам заполняет part_articles), тоже под откат.
Запуск: python3 tests/test_tick.py
"""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import asyncpg, yaml
from decimal import Decimal
from validator.config import load_config
from validator.events import run_tick, get_cursors, EPOCH

from validator.config import load_dotenv, load_dsns
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
DSN = load_dsns()
PASS=[]; FAIL=[]
def check(name, cond, info=''):
    (PASS if cond else FAIL).append(name)
    print(('  ok  ' if cond else '  FAIL')+f' {name}'+(f'  [{info}]' if info and not cond else ''))

cfgd = {'tick_interval_sec':30,'cursor_overlap_sec':60,'full_reconcile_interval_sec':86400,
        'reparse_done_retention_days':7,'allowed_conditions':['new'],
        'checks':['dedup','condition','blocklist','whitelist','price'],
        'rules':{'blocklist':[{'pattern':'fake','match_type':'word'}],
                 'whitelist':{'require':'any','words':[]}}}
open('/tmp/cfg_tick_test.yaml','w').write(yaml.safe_dump(cfgd))

async def vstate(vd):
    return {r['item_id']:(r['status'],list(r['reject_reasons'])) for r in await vd.fetch('select * from validated_items')}

async def main():
    cfg = load_config('/tmp/cfg_tick_test.yaml')
    ed = await asyncpg.connect(dsn=DSN['EBAY_DATA_DSN'])
    vd = await asyncpg.connect(dsn=DSN['VALIDATOR_DSN'])
    pp = await asyncpg.connect(dsn=DSN['PARTS_PRICES_DSN'])
    sm = await asyncpg.connect(dsn=DSN['SMART_DSN'])
    txs = [c.transaction() for c in (ed,vd,pp,sm)]
    for t in txs: await t.start()
    try:
        # --- синтетика: смарт-артикул прямо в smart (под откат); sync-триггер
        # parts_sync_articles сам разложит массив articles в part_articles ---
        await sm.execute("insert into parts(id,name,articles,is_draft,product_type) "
                         "values('smart_99999901','тестовая деталь',array['TST-A1','TST-A2'],false,'Для водного транспорта')")
        await pp.execute("insert into buying.smart_prices(smart_part_id,max_buy_price_usd,updated_by) values('smart_99999901',100,'e2e-test')")
        # --- синтетика: каталог ---
        await ed.execute("insert into contexts overriding system value values (1,'EBAY_US','10001')")
        await ed.execute("insert into sellers(seller_id,name,first_seen_at) overriding system value select g,'s'||g,now() from generate_series(1,5) g")
        await ed.execute("""insert into items(item_id,title,condition,price_usd,seller_id,first_seen_at,last_seen_at,is_dead) values
            (201,'Test impeller kit one','new',50,1,now()-interval '10 minutes',now(),false),
            (202,'Test impeller kit two','new',60,2,now()-interval '9 minutes',now(),false)""")
        await ed.execute("insert into catalog_items values('TST-A1',1,201,now()-interval '10 minutes',now(),0,true),('TST-A2',1,202,now()-interval '9 minutes',now(),0,true)")

        print('=== T1: bootstrap-тик (курсоры с эпохи) ===')
        s = await run_tick(ed,vd,pp,sm,cfg)
        st = await vstate(vd)
        check('оба item одобрены', st.get(201,(None,))[0]=='approved' and st.get(202,(None,))[0]=='approved', st)
        check('кандидатов >= 2', s['candidates']>=2, s)
        curs = await get_cursors(vd)
        check('курсоры items/catalog сдвинуты с эпохи', curs['items_first_seen']>EPOCH and curs['catalog_first_seen']>EPOCH, curs)
        check('курсор smart_prices сдвинут', curs['smart_prices_updated']>EPOCH, curs)

        print('=== T2: холостой тик (overlap переподбирает, но всё скипается) ===')
        s = await run_tick(ed,vd,pp,sm,cfg)
        check('0 ревалидаций', s['validated']==0, s)
        check('скипы по отпечатку есть (overlap)', s['skipped']>=2, s)

        print('=== T3: новый item ловится курсором items/catalog ===')
        await ed.execute("""insert into items(item_id,title,condition,price_usd,seller_id,first_seen_at,last_seen_at,is_dead)
            values(203,'Test impeller kit three','new',70,3,now(),now(),false)""")
        await ed.execute("insert into catalog_items values('TST-A1',1,203,now(),now(),0,true)")
        s = await run_tick(ed,vd,pp,sm,cfg)
        st = await vstate(vd)
        check('203 одобрен', st.get(203,(None,))[0]=='approved', (s,st))

        print('=== T4: смена title -> changes -> задача на перепарс + ревалидация ===')
        await ed.execute("update items set title='Test impeller kit one V2' where item_id=201")
        await ed.execute("""insert into changes values(now(),201,'TST-A1',1,
            (select field_id from fields where scope='item' and name='title'),'c','old','new','S')""")
        s = await run_tick(ed,vd,pp,sm,cfg)
        tasks = await vd.fetch('select * from reparse_tasks')
        check('задача на перепарс создана', len(tasks)==1 and tasks[0]['item_id']==201, tasks)
        check('201 ревалидирован (validated>=1)', s['validated']>=1, s)
        await ed.execute("""insert into changes values(now(),201,'TST-A1',1,
            (select field_id from fields where scope='item' and name='title'),'c','new','newer','S')""")
        s = await run_tick(ed,vd,pp,sm,cfg)
        tasks = await vd.fetch('select * from reparse_tasks')
        check('дубль активной задачи не создан', len(tasks)==1, tasks)

        print('=== T5: обновление доставки -> ревалидация по цене ===')
        await ed.execute("insert into item_shipping values(202,1,45,now())")  # 60+45 > 100
        s = await run_tick(ed,vd,pp,sm,cfg)
        st = await vstate(vd)
        check('202 отклонён по цене после появления доставки', st[202]==('rejected',['price']), st[202])

        print('=== T6: смена макс. цены -> force группы ===')
        await pp.execute("update buying.smart_prices set max_buy_price_usd=55, updated_at=now() where smart_part_id='smart_99999901'")
        s = await run_tick(ed,vd,pp,sm,cfg)
        st = await vstate(vd)
        check('203 отозван по цене (70>55)', st[203]==('rejected',['price']), st[203])
        rv = await vd.fetchval('select revoked_at from validated_items where item_id=203')
        check('203 revoked_at установлен', rv is not None)

        print('=== T7: падение в середине тика не двигает курсоры ===')
        await ed.execute("""insert into items(item_id,title,condition,price_usd,seller_id,first_seen_at,last_seen_at,is_dead)
            values(204,'Test impeller kit four','new',10,4,now(),now(),false)""")
        await ed.execute("insert into catalog_items values('TST-A2',1,204,now(),now(),0,true)")
        before = await get_cursors(vd)
        import validator.events as E
        orig = E.validate_groups
        async def boom(*a, **k): raise RuntimeError('искусственное падение')
        E.validate_groups = boom
        try:
            await run_tick(ed,vd,pp,sm,cfg)
            check('тик должен был упасть', False)
        except RuntimeError:
            check('тик упал как задумано', True)
        E.validate_groups = orig
        after = await get_cursors(vd)
        check('курсоры не сдвинулись после падения', before==after, (before,after))
        s = await run_tick(ed,vd,pp,sm,cfg)
        st = await vstate(vd)
        check('повторный тик дообработал 204', st.get(204,(None,))[0]=='approved', (s,st))

        print()
        print(f'PASSED {len(PASS)}  FAILED {len(FAIL)}')
        if FAIL: print('FAILED:', FAIL)
        assert not FAIL, f'{len(FAIL)} проверок упало'
    finally:
        for t in txs: await t.rollback()
        print('rollback ok; cursors в реальной БД =', await vd.fetchval('select count(*) from cursors'),
              '; reparse_tasks =', await vd.fetchval('select count(*) from reparse_tasks'))
        for c in (ed,vd,pp,sm): await c.close()

asyncio.run(main())
