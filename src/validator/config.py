"""Загрузка и валидация YAML-конфига (SPEC §7). Читается один раз при старте."""
import os
import re
from dataclasses import dataclass

import yaml

VALID_CHECKS = ('dedup', 'condition', 'blocklist', 'whitelist', 'price')
DSN_KEYS = ('EBAY_DATA_DSN', 'PARTS_PRICES_DSN', 'PARTS_RESEARCH_DSN', 'VALIDATOR_DSN')


@dataclass(frozen=True)
class Rule:
    pattern: str
    match_type: str  # word | substring
    regex: re.Pattern


@dataclass(frozen=True)
class Config:
    tick_interval_sec: int
    cursor_overlap_sec: int
    full_reconcile_interval_sec: int
    reparse_done_retention_days: int
    allowed_conditions: frozenset
    checks: tuple
    blocklist: tuple  # кортеж Rule
    whitelist: tuple  # кортеж Rule


def _compile_rule(entry: dict, where: str) -> Rule | None:
    if not isinstance(entry, dict) or 'pattern' not in entry:
        raise ValueError(f'{where}: each entry needs a "pattern": {entry!r}')
    if not entry.get('active', True):
        return None
    scope = entry.get('scope', 'global')
    if scope != 'global':
        raise ValueError(f'{where}: only scope "global" is supported for now, got {scope!r}')
    match_type = entry.get('match_type', 'word')
    pattern = str(entry['pattern']).strip().lower()
    if not pattern:
        raise ValueError(f'{where}: empty pattern')
    escaped = re.escape(pattern)
    if match_type == 'word':
        # целое слово: вокруг паттерна нет буквенно-цифровых символов (юникод);
        # матчим по уже приведённой к нижнему регистру строке — без IGNORECASE
        # и его юникод-сюрпризов
        regex = re.compile(r'(?<!\w)' + escaped + r'(?!\w)')
    elif match_type == 'substring':
        regex = re.compile(escaped)
    else:
        raise ValueError(f'{where}: match_type must be word|substring, got {match_type!r}')
    return Rule(pattern=pattern, match_type=match_type, regex=regex)


def load_config(path: str = 'config.yaml') -> Config:
    raw = yaml.safe_load(open(path))

    checks = tuple(raw.get('checks') or ())
    unknown = set(checks) - set(VALID_CHECKS)
    if unknown:
        raise ValueError(f'checks: unknown names {sorted(unknown)}; valid: {VALID_CHECKS}')
    if len(set(checks)) != len(checks):
        raise ValueError('checks: duplicate entries')

    rules = raw.get('rules') or {}
    blocklist = tuple(r for e in (rules.get('blocklist') or ())
                      if (r := _compile_rule(e, 'rules.blocklist')))
    wl = rules.get('whitelist') or {}
    require = wl.get('require', 'any')
    if require != 'any':
        raise ValueError(f'rules.whitelist.require: only "any" is supported, got {require!r}')
    whitelist = tuple(r for e in (wl.get('words') or ())
                      if (r := _compile_rule(e, 'rules.whitelist.words')))

    return Config(
        tick_interval_sec=int(raw['tick_interval_sec']),
        cursor_overlap_sec=int(raw['cursor_overlap_sec']),
        full_reconcile_interval_sec=int(raw['full_reconcile_interval_sec']),
        reparse_done_retention_days=int(raw['reparse_done_retention_days']),
        allowed_conditions=frozenset(raw.get('allowed_conditions') or ()),
        checks=checks,
        blocklist=blocklist,
        whitelist=whitelist,
    )


def load_dotenv(path: str = '.env') -> None:
    """Простейший загрузчик .env: строки KEY=VALUE; существующие переменные не перетирает."""
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip())


def load_dsns(env=os.environ) -> dict:
    missing = [k for k in DSN_KEYS if not env.get(k)]
    if missing:
        raise ValueError(f'missing env vars: {missing} (see .env.example)')
    return {k: env[k] for k in DSN_KEYS}
