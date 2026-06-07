"""Нормализация названия и отпечаток вердикта (SPEC §3, §5.1)."""
import hashlib
import re
from decimal import Decimal

_NORM_RE = re.compile(r'[\W_]+')


def norm_title(title: str | None) -> str:
    """Нижний регистр; последовательности не-буквенно-цифровых символов (юникод) — в один пробел."""
    return _NORM_RE.sub(' ', (title or '').lower()).strip()


def _s(v) -> str:
    if v is None:
        return ''
    if isinstance(v, Decimal):
        # каноничная форма: не зависит от масштаба ('10000.00' == '10000') и без научной записи
        return f'{v.normalize():f}'
    if isinstance(v, bool):
        return 't' if v else 'f'
    return str(v)


def fingerprint(title, seller_id, condition, price_usd, shipping_cost,
                max_buy_price_usd, alive) -> bytes:
    """md5 по всем входам, от которых зависит вердикт (SPEC §5.1)."""
    parts = (title, seller_id, condition, price_usd, shipping_cost,
             max_buy_price_usd, alive)
    return hashlib.md5('|'.join(map(_s, parts)).encode()).digest()
