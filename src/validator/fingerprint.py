"""Title normalization and verdict fingerprint (SPEC §3, §5.1)."""
import hashlib
import re
from decimal import Decimal

_NORM_RE = re.compile(r'[\W_]+')


def norm_title(title: str | None) -> str:
    """Lowercase, collapse runs of non-alphanumerics (unicode-aware) to one space."""
    return _NORM_RE.sub(' ', (title or '').lower()).strip()


def _s(v) -> str:
    if v is None:
        return ''
    if isinstance(v, Decimal):
        # canonical form: scale-independent ('10000.00' == '10000'), never scientific
        return f'{v.normalize():f}'
    if isinstance(v, bool):
        return 't' if v else 'f'
    return str(v)


def fingerprint(title, seller_id, condition, price_usd, shipping_cost,
                max_buy_price_usd, alive) -> bytes:
    """md5 over every input the verdict depends on (SPEC §5.1)."""
    parts = (title, seller_id, condition, price_usd, shipping_cost,
             max_buy_price_usd, alive)
    return hashlib.md5('|'.join(map(_s, parts)).encode()).digest()
