"""English -> Thai translation of short headlines, best effort.

Uses free public endpoints (MyMemory first, then Google's unofficial endpoint). Every
result is cached on disk so a headline is translated once per day at most. When nothing
works the caller keeps the English text.
"""
from __future__ import annotations

import html
import json
import logging
import threading

import requests

from . import config

log = logging.getLogger(__name__)

MYMEMORY_URL = "https://api.mymemory.translated.net/get"
GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
CACHE_FILE = config.DATA_DIR / "translations_th.json"
TIMEOUT = 15

_lock = threading.Lock()
_cache: dict[str, str] | None = None


def _load_cache() -> dict[str, str]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(_cache or {}, ensure_ascii=False, indent=0), encoding="utf-8")
    except OSError as exc:
        log.debug("could not write translation cache: %s", exc)


def _mymemory(text: str) -> str | None:
    resp = requests.get(
        MYMEMORY_URL,
        params={"q": text, "langpair": "en|th"},
        headers={"User-Agent": config.USER_AGENT},
        timeout=TIMEOUT,
    )
    payload = resp.json()
    if payload.get("responseStatus") != 200:
        return None
    out = html.unescape(str((payload.get("responseData") or {}).get("translatedText") or "")).strip()
    if not out or "MYMEMORY WARNING" in out.upper() or out.lower() == text.lower():
        return None
    return out


def _google(text: str) -> str | None:
    resp = requests.get(
        GOOGLE_URL,
        params={"client": "gtx", "sl": "en", "tl": "th", "dt": "t", "q": text},
        headers={"User-Agent": config.USER_AGENT},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    out = "".join(seg[0] for seg in (data[0] or []) if seg and seg[0]).strip()
    return out or None


PROVIDERS = (_mymemory, _google)


def to_thai(text: str | None) -> str | None:
    """Thai translation of ``text``, or None when no provider could translate it."""
    if not text:
        return None
    cache = _load_cache()
    with _lock:
        if text in cache:
            return cache[text] or None
    for provider in PROVIDERS:
        try:
            out = provider(text)
        except Exception as exc:  # noqa: BLE001
            log.debug("%s failed: %s", provider.__name__, exc)
            continue
        if out:
            with _lock:
                cache[text] = out
                _save_cache()
            return out
    log.warning("could not translate headline; keeping English")
    return None
