"""Shared i18n configuration for Visa Sniper Intl."""
from __future__ import annotations

from flask import request

SUPPORTED_LOCALES = ["en", "de", "fr", "it", "es", "tr"]
DEFAULT_LOCALE = "en"

LANGUAGE_NAMES = {
    "en": "English",
    "de": "Deutsch",
    "fr": "Français",
    "it": "Italiano",
    "es": "Español",
    "tr": "Türkçe",
}

LANGUAGE_FLAGS = {
    "en": "\U0001F1EC\U0001F1E7",
    "de": "\U0001F1E9\U0001F1EA",
    "fr": "\U0001F1EB\U0001F1F7",
    "it": "\U0001F1EE\U0001F1F9",
    "es": "\U0001F1EA\U0001F1F8",
    "tr": "\U0001F1F9\U0001F1F7",
}

COOKIE_NAME = "vs_lang"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


def select_locale() -> str:
    """
    Locale resolution order:
      1. cookie set by user-chosen language
      2. browser Accept-Language header
      3. DEFAULT_LOCALE (en)
    """
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie in SUPPORTED_LOCALES:
        return cookie
    best = request.accept_languages.best_match(SUPPORTED_LOCALES)
    return best or DEFAULT_LOCALE
