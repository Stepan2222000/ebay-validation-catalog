"""E2e-тест ядра валидации (PLAN, этап 3).

Гоняется против НАСТОЯЩИХ серверов ebay_data / ebay_validation_catalog, но все
записи происходят в транзакциях, которые в конце откатываются — обе базы
остаются нетронутыми. Запуск: python3 tests/test_e2e.py
"""
import asyncio
from decimal import Decimal

from _helpers import check, finish, rollback_conns, vstate, write_cfg
from validator.events import FETCH_GROUPS_SQL
from validator.fingerprint import fingerprint, norm_title
from validator.validation import build_units, validate_groups

BLOCK = [{'pattern': 'fake', 'match_type': 'word'}, {'pattern': 'ban', 'match_type': 'word'}]
WHITE = [{'pattern': 'impeller', 'match_type': 'word'}]


async def groups(ed, mapping):
    return build_units(await ed.fetch(FETCH_GROUPS_SQL, list(mapping)), mapping)


async def main():
    async with rollback_conns('EBAY_DATA_DSN', 'VALIDATOR_DSN') as (ed, vd):
        # ---------- синтетические данные в ebay_data ----------
        await ed.execute("insert into contexts overriding system value values (1,'EBAY_US','10001')")
        await ed.execute("insert into sellers(seller_id,name,first_seen_at) overriding system value select g,'s'||g,now() from generate_series(1,12) g")
        items = [  # id, тайтл, condition, цена, продавец, +сек к first_seen_at
          (101,'Suzuki impeller kit 17400-90J11','new',50,1,0),
          (102,'Suzuki impeller repair kit OEM','new',55,1,1),
          (103,'Suzuki  IMPELLER kit  17400-90J11!','new',60,2,2),
          (104,'FAKE Suzuki impeller kit','new',40,3,3),
          (105,'Suzuki water pump housing only','new',45,4,4),
          (106,'Suzuki impeller kit premium','new',95,5,5),
          (107,'Suzuki impeller kit used cond','other',30,2,6),
          (109,'Carbon fiber impeller kit','new',70,6,8),
          (110,'Vintage IMPELLERS lot','new',20,7,9),
          (111,'Expensive impeller kit ten-thousand','new',10000,8,10),
          (120,'FAKE impeller bargain','new',10,9,11),
          (121,'Good impeller bargain','new',12,9,12),
          (140,'Tie impeller alpha','new',30,10,20),
          (141,'Tie impeller beta','new',31,10,20),  # тот же first_seen_at, что у 140
        ]
        for iid,t,c,p,s,off in items:
            await ed.execute("""insert into items(item_id,title,condition,price_usd,seller_id,first_seen_at,last_seen_at,is_dead)
                values($1,$2,$3,$4,$5,timestamptz '2026-06-01 00:00:00+00' + $6*interval '1 second',now(),false)""",iid,t,c,p,s,off)
        await ed.execute("insert into items(item_id,first_seen_at,last_seen_at,is_dead) values(108,now(),now(),false)")  # заглушка
        await ed.execute("insert into items(item_id,title,condition,price_usd,seller_id,first_seen_at,last_seen_at,is_dead) values(130,'Lonely impeller no seller','new',15,null,now(),now(),false)")
        # членства: smart_X = A1+A2; smart_Y = B1; smart_Z = C1 (сценарий порядка дедупа); 140/141 в smart_W = D1
        mem = [('A1',101),('A2',101),('A1',102),('A2',103),('A1',104),('A1',105),('A1',106),('A1',107),
               ('A1',108),('A1',109),('A1',110),('B1',111),('C1',120),('C1',121),('D1',140),('D1',141),('B1',130)]
        for a,iid in mem:
            await ed.execute("insert into catalog_items values($1,1,$2,now(),now(),0,true)",a,iid)
        await ed.execute("insert into item_shipping values(101,1,10,now()),(103,1,5,now()),(106,1,20,now()),(102,1,5,now())")
        mapping={'A1':'smart_X','A2':'smart_X','B1':'smart_Y','C1':'smart_Z','D1':'smart_W'}
        prices={'smart_X':Decimal(100),'smart_Y':None,'smart_Z':None,'smart_W':None}

        cfg = write_cfg(['dedup','condition','blocklist','whitelist','price'], BLOCK, WHITE)

        print('=== S1: первичный прогон, dedup ПЕРВЫМ (как в config.yaml) ===')
        r = await validate_groups(vd, await groups(ed,mapping), cfg, prices)
        st = await vstate(vd)
        check('101 approved', st[101][0]=='approved', st[101])
        check('101 articles agg [A1,A2]', st[101][3]==['A1','A2'], st[101])
        check('102 seller_dup', st[102][1]==['seller_dup'], st[102])
        check('103 title_dup (норм. тайтла)', st[103][1]==['title_dup'], st[103])
        check('104 blocklist', st[104][1]==['blocklist'], st[104])
        check('105 whitelist', st[105][1]==['whitelist'], st[105])
        check('106 price (95+20>100)', st[106][1]==['price'], st[106])
        check('107 seller_dup ДО condition (dedup первым)', st[107][1]==['seller_dup'], st[107])
        check('108 pending', st[108][0]=='pending', st[108])
        check('109 approved (ban НЕ матчит carbon, word)', st[109][0]=='approved', st[109])
        check('110 whitelist (impellers != impeller, word)', st[110][1]==['whitelist'], st[110])
        check('111 approved (цена 10000, max NULL)', st[111][0]=='approved', st[111])
        check('130 approved (seller NULL, без слота продавца)', st[130][0]=='approved', st[130])
        check('120 rejected blocklist (победитель занял слот и срезался)', st[120][1]==['blocklist'], st[120])
        check('121 seller_dup (слот у блоклистного победителя)', st[121][1]==['seller_dup'], st[121])
        check('140 approved', st[140][0]=='approved', st[140])
        check('141 seller_dup (тот же ts, тай-брейк по item_id)', st[141][1]==['seller_dup'], st[141])

        print('=== S2: повторный прогон — всё скипается ===')
        r = await validate_groups(vd, await groups(ed,mapping), cfg, prices)
        check('0 upserts', r['validated']==0, r)
        check(f"{r['skipped']} bump-ов last_checked_at", r['skipped']>=16, r)

        print('=== S3: тот же датасет, dedup ПОСЛЕДНИМ ===')
        cfg2 = write_cfg(['condition','blocklist','whitelist','price','dedup'], BLOCK, WHITE)
        r = await validate_groups(vd, await groups(ed,mapping), cfg2, prices)
        st = await vstate(vd)
        check('121 approved (блоклистный не участвует в соревновании)', st[121][0]=='approved', st[121])
        check('120 rejected blocklist', st[120][1]==['blocklist'], st[120])
        check('107 condition (а не seller_dup: обрыв до дедупа)', st[107][1]==['condition'], st[107])

        print('=== S4: возврат к dedup-первым, смерть победителя 101 ===')
        r = await validate_groups(vd, await groups(ed,mapping), cfg, prices)  # вернуть базовые вердикты
        await ed.execute("update items set is_dead=true, died_at=now() where item_id=101")
        r = await validate_groups(vd, await groups(ed,mapping), cfg, prices)
        st = await vstate(vd)
        check('101 rejected inactive', st[101][1]==['inactive'], st[101])
        check('101 revoked_at установлен', st[101][2] is not None, st[101])
        check('102 approved (слот продавца освободился)', st[102][0]=='approved', st[102])
        check('103 approved (слот тайтла освободился)', st[103][0]=='approved', st[103])
        check('107 всё ещё seller_dup (слот s2 теперь у 103)', st[107][1]==['seller_dup'], st[107])

        print('=== S5: max цена smart_X 100 -> 60 ===')
        prices2 = dict(prices, smart_X=Decimal(60))
        r = await validate_groups(vd, await groups(ed,mapping), cfg, prices2)
        st = await vstate(vd)
        check('103 rejected price (60+5>60), revoked_at установлен', st[103][1]==['price'] and st[103][2] is not None, st[103])
        check('102 approved (55+5=60 не больше 60)', st[102][0]=='approved', st[102])

        print('=== S6: max цена smart_X -> NULL: ценовой чек выключен ===')
        prices3 = dict(prices, smart_X=None)
        r = await validate_groups(vd, await groups(ed,mapping), cfg, prices3)
        st = await vstate(vd)
        check('103 approved снова, revoked_at сброшен', st[103][0]=='approved' and st[103][2] is None, st[103])
        check('106 approved (ценовой чек выключен)', st[106][0]=='approved', st[106])

        print('=== S7: заглушка 108 распаршена -> pending -> вердикт ===')
        await ed.execute("update items set title='Brand new impeller kit unique', condition='new', price_usd=33, seller_id=12 where item_id=108")
        r = await validate_groups(vd, await groups(ed,mapping), cfg, prices3)
        st = await vstate(vd)
        check('108 approved', st[108][0]=='approved', st[108])

        print('=== S8: выключение whitelist (нет в checks) ===')
        cfg3 = write_cfg(['dedup','condition','blocklist','price'], BLOCK, WHITE)
        r = await validate_groups(vd, await groups(ed,mapping), cfg3, prices3)
        st = await vstate(vd)
        check('105 approved (whitelist выключен)', st[105][0]=='approved', st[105])
        check('110 approved (whitelist выключен)', st[110][0]=='approved', st[110])

        print('=== S9: substring-правило блоклиста ===')
        cfg4 = write_cfg(['dedup','condition','blocklist','whitelist','price'],
                         [{'pattern':'rbo','match_type':'substring'}], WHITE)
        r = await validate_groups(vd, await groups(ed,mapping), cfg4, prices3)
        st = await vstate(vd)
        check('109 rejected blocklist (rbo как substring матчит caRBOn)', st[109][1]==['blocklist'], st[109])

        print('=== S10: стабильность отпечатка для Decimal 10000 (1E+4) ===')
        v = await ed.fetchval("select price_usd from items where item_id=111")
        fp1 = fingerprint('t', 1, 'new', v, None, None, True)
        fp2 = fingerprint('t', 1, 'new', Decimal('10000'), None, None, True)
        fp3 = fingerprint('t', 1, 'new', Decimal('1E+4'), None, None, True)
        check('fp(Decimal из БД) == fp(Decimal literal) == fp(1E+4)', fp1==fp2==fp3, (v, f'{v:f}'))

        print('=== S11: норм. тайтла юникод ===')
        check('кириллица не схлопывается в пустоту', norm_title('Опора двигателя!')== 'опора двигателя')
        check('пунктуация/регистр', norm_title('  Suzuki  IMPELLER--kit  ')=='suzuki impeller kit')

        print('=== S12: валидация конфига ===')
        for name, kw in [
            ('неизвестная проверка', dict(checks=['dedup','bogus'])),
            ('дубль проверки', dict(checks=['dedup','dedup'])),
            ('scope != global', dict(checks=['blocklist'], blocklist=[{'pattern':'x','scope':'smart:1'}])),
            ('match_type кривой', dict(checks=['blocklist'], blocklist=[{'pattern':'x','match_type':'fuzzy'}])),
        ]:
            try:
                write_cfg(**kw); check(f'{name}: должен падать', False)
            except ValueError:
                check(f'{name}: ValueError', True)
        inact = write_cfg(['blocklist'], [{'pattern':'x','active':False}])
        check('active:false выкидывает правило', inact.blocklist==())

        finish()

asyncio.run(main())
