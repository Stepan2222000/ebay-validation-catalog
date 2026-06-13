"""Read-only shadow-сервис мусор-фильтра.

Мониторит approved в БД валидатора, классифицирует, мусор пишет в отдельный лог.
В БД НИЧЕГО не пишет (только SELECT) — не конфликтует с боевым валидатором.

Запуск:   PYTHONPATH=<repo> python -m junk_filter.service
Нужен env VALIDATOR_DSN (берётся из .env проекта).
"""
import os
import sys
import json
import time
import asyncio
from datetime import datetime, timezone

import asyncpg

from junk_filter import part_scores
from junk_filter.classifier import _THRESHOLD

INTERVAL = int(os.environ.get("JUNK_FILTER_INTERVAL", "30"))
LOG_PATH = os.environ.get("JUNK_FILTER_LOG", "junk_filter/junk_filtered.jsonl")


def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')}  {msg}", flush=True)


async def _classify(rows):
    scores = part_scores([r["title"] for r in rows])
    return [{"item_id": r["item_id"], "p_part": round(p, 4), "title": r["title"],
             "seen": datetime.now(timezone.utc).isoformat()}
            for r, p in zip(rows, scores) if p < _THRESHOLD]


async def main():
    t0 = time.time()
    part_scores(["warmup"])  # прогрев модели
    log(f"модель загружена за {time.time()-t0:.1f}s, порог P(деталь)<{_THRESHOLD} -> мусор")

    vd = await asyncpg.connect(os.environ["VALIDATOR_DSN"])
    log("подключён к БД валидатора (только чтение)")

    rows = await vd.fetch("select item_id, title, validated_at from validated_items "
                          "where status='approved' and title is not null order by validated_at")
    junk = await _classify(rows)
    with open(LOG_PATH, "w") as f:
        for j in junk:
            f.write(json.dumps(j, ensure_ascii=False) + "\n")
    last_ts = max((r["validated_at"] for r in rows), default=None)
    log(f"BASELINE: approved={len(rows)}  мусор={len(junk)} "
        f"({len(junk)/max(len(rows),1):.0%})  -> {LOG_PATH}")
    for j in sorted(junk, key=lambda x: x["p_part"])[:5]:
        log(f"   junk P={j['p_part']:.2f}  {j['title'][:60]}")

    tick = 0
    while True:
        await asyncio.sleep(INTERVAL)
        tick += 1
        q = ("select item_id, title, validated_at from validated_items "
             "where status='approved' and title is not null")
        args = []
        if last_ts is not None:
            q += " and validated_at > $1"
            args = [last_ts]
        rows = await vd.fetch(q + " order by validated_at", *args)
        if not rows:
            log(f"tick {tick}: новых approved нет")
            continue
        junk = await _classify(rows)
        if junk:
            with open(LOG_PATH, "a") as f:
                for j in junk:
                    f.write(json.dumps(j, ensure_ascii=False) + "\n")
        last_ts = max(r["validated_at"] for r in rows)
        log(f"tick {tick}: +{len(rows)} новых approved, мусор {len(junk)}")


if __name__ == "__main__":
    asyncio.run(main())
