"""Лёгкий классификатор «деталь / мусор» (MiniLM, дообучен на синтетике+реальных).
Модель грузится один раз и держится в памяти. Чистый CPU, ~1.4 мс/тайтл."""
import os
import threading

_DIR = os.path.dirname(__file__)
_MODEL_DIR = os.environ.get("JUNK_FILTER_MODEL", os.path.join(_DIR, "model"))
_THRESHOLD = float(os.environ.get("JUNK_FILTER_THRESHOLD", "0.15"))  # P(деталь) ниже -> мусор

_lock = threading.Lock()
_state = {}


def _load():
    with _lock:
        if "model" not in _state:
            import torch
            from transformers import AutoTokenizer, AutoModelForSequenceClassification
            _state["torch"] = torch
            _state["tok"] = AutoTokenizer.from_pretrained(_MODEL_DIR)
            _state["model"] = AutoModelForSequenceClassification.from_pretrained(_MODEL_DIR).eval()
    return _state["tok"], _state["model"], _state["torch"]


def part_scores(titles):
    """P(деталь) для списка тайтлов (батчами)."""
    if not titles:
        return []
    tok, model, torch = _load()
    out = []
    with torch.no_grad():
        for i in range(0, len(titles), 64):
            batch = [t or "" for t in titles[i:i + 64]]
            enc = tok(batch, truncation=True, max_length=64, padding=True, return_tensors="pt")
            p = torch.softmax(model(**enc).logits, dim=-1)[:, 1]
            out.extend(p.tolist())
    return out


def is_junk(title, threshold=None):
    """True, если тайтл — мусор (не запчасть)."""
    thr = _THRESHOLD if threshold is None else threshold
    return part_scores([title])[0] < thr
