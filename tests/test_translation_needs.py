"""Tests for translation.needs_translation() — ADR-0016 user_languages source-of-truth.

Bug fix: previously NATIVE_LANGUAGES was hardcoded {zh, en}, ignoring the
user's actual configured user_languages. Also "unknown" lang detection unilaterally
hid the translate button — now we offer it for substantive text and let the
user decide.
"""
from __future__ import annotations

from compass.services.translation import needs_translation


# ─── user_languages parameter respects search_config ─────


def test_no_user_languages_treated_as_undeclared_offers_button():
    """5-06 22:21 fix: undeclared user_languages → show button, let user decide.

    Previously硬编码 fallback to {zh, en} — that hid the button for users who
    hadn't filled user_languages yet. Decision: offer translation for anything
    detected; user can ignore."""
    en_jd = "We are hiring a Senior Engineer with experience in Python and Java. " * 5
    offer, lang = needs_translation(en_jd)
    assert lang == "en"
    assert offer is True  # undeclared → offer translation


def test_chinese_user_only_offers_translation_for_english():
    """If user only reads zh, English JD should get translate button."""
    en_jd = "We are looking for a Senior Software Engineer with 5+ years experience in distributed systems. " * 3
    offer, lang = needs_translation(en_jd, user_languages=["zh"])
    assert lang == "en"
    assert offer is True  # user can't read English


def test_bilingual_user_no_translate_for_either():
    en_jd = "Looking for a Python engineer with cloud experience. " * 5
    offer, _ = needs_translation(en_jd, user_languages=["zh", "en"])
    assert offer is False


def test_user_languages_includes_italian_no_offer_for_italian():
    it_jd = (
        "Cerchiamo un ingegnere software con esperienza nei sistemi distribuiti. "
        "L'azienda offre un ambiente dinamico e una grande opportunità di crescita."
    ) * 3
    offer, lang = needs_translation(it_jd, user_languages=["en", "it"])
    assert lang == "it"
    assert offer is False  # user reads it, no translation needed


# ─── unknown language heuristic ──────────────────────────


def test_unknown_lang_with_substantive_text_offers_button():
    """The user-reported痛点: detection fails ('unknown') but JD is real → button."""
    weird_jd = (
        "Position requires advanced statistical methodology, Bayesian inference, "
        "and proficiency with R programming. Compensation discussed during interview. "
        "Position open immediately."
    )
    offer, lang = needs_translation(weird_jd, user_languages=["zh"])
    # detection may say 'en' (long English with stopwords) — that's fine for this case.
    # The key invariant: offer=True since the user can't read whatever it is.
    if lang == "unknown":
        assert offer is True  # the bug fix
    # If detected as some non-zh lang, also True
    assert offer is True


def test_unknown_lang_with_short_text_no_button():
    """Don't offer translation for empty / trivial text — that's not a JD."""
    offer, lang = needs_translation("Tiny", user_languages=["zh"])
    assert lang == "unknown"
    assert offer is False


def test_unknown_lang_at_threshold():
    """Around 100 chars boundary."""
    short_unknown = "x y z " * 15  # ~90 chars, detect=unknown
    offer, lang = needs_translation(short_unknown, user_languages=["zh"])
    if lang == "unknown":
        assert offer is False  # below 100 char threshold

    longer_unknown = "x y z " * 25  # ~150 chars, detect=unknown
    offer2, lang2 = needs_translation(longer_unknown, user_languages=["zh"])
    if lang2 == "unknown":
        assert offer2 is True  # above threshold


# ─── empty / None edge cases ─────────────────────────────


def test_empty_text_no_offer():
    offer, lang = needs_translation("", user_languages=["zh"])
    assert lang == "unknown"
    assert offer is False


def test_none_text_no_offer():
    offer, lang = needs_translation(None, user_languages=["zh"])
    assert lang == "unknown"
    assert offer is False


def test_user_languages_empty_list_treated_as_undeclared():
    """5-06 22:21 fix: [] → treated as undeclared → offer button.

    Previously [] was falsy → fell back to NATIVE_LANGUAGES = {zh, en}, hiding
    button for users who never filled user_languages. After fix: empty list is
    NOT silently coerced to default — it's 'I haven't declared, show me the
    button so I can decide'."""
    en_jd = "We are looking for an engineer with Python experience. " * 5
    offer, _ = needs_translation(en_jd, user_languages=[])
    assert offer is True  # undeclared → offer translation
