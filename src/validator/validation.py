"""Validation core (SPEC §3, §4): a batch of part groups -> verdicts in validated_items.

The caller supplies FULL groups: for every touched smart part, every catalog
membership row of that part. Dedup is a competition over the whole group, so
partial groups would produce wrong slot assignments.
"""
from dataclasses import dataclass, field

from .fingerprint import norm_title, fingerprint

SINGLE_CHECKS = ('condition', 'blocklist', 'whitelist', 'price')


@dataclass
class Unit:
    """One validation unit (SPEC §2): item within a smart part within a context."""
    item_id: int
    part_id: str
    context_id: int
    articles: list           # sorted, all current catalog memberships in this part
    first_seen_at: object
    alive: bool              # catalog is_active AND NOT items.is_dead
    title: str | None
    condition: str | None
    price_usd: object
    seller_id: int | None
    shipping_cost: object
    # filled during validation:
    fp: bytes | None = None
    status: str = 'pending'
    reasons: list = field(default_factory=list)

    @property
    def key(self):
        return (self.item_id, self.part_id, self.context_id)


def build_units(rows, mapping) -> dict:
    """Aggregate detailed catalog membership rows into units grouped by part.

    rows: records with article, context_id, item_id, first_seen_at (items),
          catalog_active, title, condition, price_usd, seller_id, is_dead,
          shipping_cost.
    mapping: article -> part_id.
    Returns {part_id: [Unit, ...]}.
    """
    units: dict[tuple, Unit] = {}
    for r in rows:
        part_id = mapping.get(r['article'])
        if part_id is None:
            continue
        key = (r['item_id'], part_id, r['context_id'])
        u = units.get(key)
        if u is None:
            u = Unit(
                item_id=r['item_id'], part_id=part_id, context_id=r['context_id'],
                articles=[r['article']], first_seen_at=r['first_seen_at'],
                alive=bool(r['catalog_active']) and not r['is_dead'],
                title=r['title'], condition=r['condition'],
                price_usd=r['price_usd'], seller_id=r['seller_id'],
                shipping_cost=r['shipping_cost'],
            )
            units[key] = u
        else:
            if r['article'] not in u.articles:
                u.articles.append(r['article'])
            # any active membership keeps the unit in the catalog
            u.alive = u.alive or (bool(r['catalog_active']) and not r['is_dead'])
    by_part: dict[str, list] = {}
    for u in units.values():
        u.articles.sort()
        by_part.setdefault(u.part_id, []).append(u)
    return by_part


def _single_check(name: str, u: Unit, cfg, max_price) -> str | None:
    """Run one single-listing check; return reason on failure, None on pass."""
    if name == 'condition':
        if u.condition not in cfg.allowed_conditions:
            return 'condition'
    elif name == 'blocklist':
        low = (u.title or '').lower()
        if any(r.regex.search(low) for r in cfg.blocklist):
            return 'blocklist'
    elif name == 'whitelist':
        if cfg.whitelist:
            low = (u.title or '').lower()
            if not any(r.regex.search(low) for r in cfg.whitelist):
                return 'whitelist'
    elif name == 'price':
        if max_price is not None and u.price_usd is not None:
            if u.price_usd + (u.shipping_cost or 0) > max_price:
                return 'price'
    return None


def judge_group(units: list, cfg, max_price) -> None:
    """Decide status/reasons for every unit of one part group (SPEC §3)."""
    if 'dedup' in cfg.checks:
        i = cfg.checks.index('dedup')
        pre, post = cfg.checks[:i], cfg.checks[i + 1:]
    else:
        pre, post = cfg.checks, ()

    judgeable = []
    for u in units:
        if u.title is None:           # parser stub: no data yet
            u.status, u.reasons, u.fp = 'pending', [], None
            continue
        u.fp = fingerprint(u.title, u.seller_id, u.condition, u.price_usd,
                           u.shipping_cost, max_price, u.alive)
        if not u.alive:               # built-in, not configurable (SPEC §3)
            u.status, u.reasons = 'rejected', ['inactive']
            continue
        judgeable.append(u)

    failed: dict[int, str] = {}       # item_id -> reason from pre-dedup checks
    for u in judgeable:
        for name in pre:
            if reason := _single_check(name, u, cfg, max_price):
                failed[u.item_id] = reason
                break

    # dedup competition among pre-pass units (SPEC §3). Seller and title slots
    # are claimed independently: the earliest contender holds a slot even if it
    # loses the other dimension ("the seller's first listing", literally).
    if 'dedup' in cfg.checks:
        contenders = sorted((u for u in judgeable if u.item_id not in failed),
                            key=lambda x: (x.first_seen_at, x.item_id))
        seller_winner: dict = {}
        title_winner: dict = {}
        for u in contenders:
            if u.seller_id is not None:
                seller_winner.setdefault(u.seller_id, u.item_id)
            title_winner.setdefault(norm_title(u.title), u.item_id)
        for u in contenders:
            if u.seller_id is not None and seller_winner[u.seller_id] != u.item_id:
                failed[u.item_id] = 'seller_dup'
            elif title_winner[norm_title(u.title)] != u.item_id:
                failed[u.item_id] = 'title_dup'

    for u in judgeable:
        reason = failed.get(u.item_id)
        if reason is None:
            for name in post:
                if reason := _single_check(name, u, cfg, max_price):
                    break
        if reason is None:
            u.status, u.reasons = 'approved', []
        else:
            u.status, u.reasons = 'rejected', [reason]


_UPSERT_SQL = """
insert into validated_items(item_id, part_id, context_id, articles, seller_id,
    title, title_norm, condition, price_usd, shipping_cost, max_buy_price_usd,
    status, reject_reasons, fingerprint)
values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
on conflict (item_id, part_id, context_id) do update set
    articles          = excluded.articles,
    seller_id         = excluded.seller_id,
    title             = excluded.title,
    title_norm        = excluded.title_norm,
    condition         = excluded.condition,
    price_usd         = excluded.price_usd,
    shipping_cost     = excluded.shipping_cost,
    max_buy_price_usd = excluded.max_buy_price_usd,
    status            = excluded.status,
    reject_reasons    = excluded.reject_reasons,
    fingerprint       = excluded.fingerprint,
    revoked_at        = case
                          when excluded.status = 'approved' then null
                          when validated_items.status = 'approved' then now()
                          else validated_items.revoked_at
                        end,
    validated_at      = now(),
    last_checked_at   = now()
"""

_BUMP_SQL = """
update validated_items set last_checked_at = now()
where item_id = $1 and part_id = $2 and context_id = $3
"""


async def validate_groups(vd, by_part: dict, cfg, prices: dict) -> dict:
    """Validate full part groups; write verdicts. Returns stats + transitions."""
    if not by_part:
        return {'validated': 0, 'skipped': 0, 'transitions': []}

    existing = {
        (r['item_id'], r['part_id'], r['context_id']): r
        for r in await vd.fetch(
            'select * from validated_items where part_id = any($1::text[])',
            list(by_part),
        )
    }

    upserts, bumps, transitions = [], [], []
    for part_id, units in by_part.items():
        judge_group(units, cfg, prices.get(part_id))
        for u in units:
            old = existing.get(u.key)
            unchanged = (
                old is not None
                and old['fingerprint'] == u.fp
                and old['status'] == u.status
                and list(old['reject_reasons']) == u.reasons
                and list(old['articles']) == u.articles
            )
            if unchanged:
                bumps.append(u.key)
                continue
            upserts.append((
                u.item_id, u.part_id, u.context_id, u.articles, u.seller_id,
                u.title, norm_title(u.title) if u.title is not None else None,
                u.condition, u.price_usd, u.shipping_cost, prices.get(part_id),
                u.status, u.reasons, u.fp,
            ))
            transitions.append(
                (u.item_id, u.part_id, old['status'] if old else '-', u.status, u.reasons)
            )

    if upserts:
        await vd.executemany(_UPSERT_SQL, upserts)
    if bumps:
        await vd.executemany(_BUMP_SQL, bumps)
    return {'validated': len(upserts), 'skipped': len(bumps), 'transitions': transitions}
