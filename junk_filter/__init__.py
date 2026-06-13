"""junk_filter — самодостаточный пакет мусор-фильтра для валидатора.

Публичный API:
    from junk_filter import part_scores, is_junk, classify_units

Валидатор подключается ОДНОЙ строкой (хук), сам ML внутри пакета.
Модель лежит в junk_filter/model, порог — env JUNK_FILTER_THRESHOLD (по умолчанию 0.15).
"""
from .classifier import part_scores, is_junk

__all__ = ["part_scores", "is_junk", "classify_units"]


def classify_units(units, threshold=None):
    """Юниты валидатора -> множество ключей (.key), помеченных мусором.
    Вердикты НЕ меняет — только классифицирует; что делать с результатом, решает вызывающий."""
    items = [u for u in units if getattr(u, "title", None)]
    if not items:
        return set()
    from .classifier import _THRESHOLD
    thr = _THRESHOLD if threshold is None else threshold
    scores = part_scores([u.title for u in items])
    return {u.key for u, p in zip(items, scores) if p < thr}
