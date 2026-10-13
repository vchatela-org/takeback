"""The translation layer: key lookup, the fallback chain, locale negotiation,
and the two helpers the templates and the item-type labels go through."""
import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="tb-test-")
os.environ.setdefault("TAKEBACK_DATABASE_URL", f"sqlite:///{_TMP}/test.db")
os.environ.setdefault("TAKEBACK_DATA_DIR", _TMP)

from app import i18n, platforms, templating  # noqa: E402


def test_translate_returns_the_key_when_it_is_unknown():
    # Louder than a blank: a missing translation must be visible in the UI.
    assert i18n.translate("no.such.key.at.all") == "no.such.key.at.all"


def test_every_key_exists_in_every_catalogue():
    """A key present in one catalogue and missing from another is the one bug
    that silently degrades to the key itself, so it is worth failing on."""
    english = set(i18n.CATALOGS["en"])
    french = set(i18n.CATALOGS["fr"])
    assert english == french


def test_fills_placeholders():
    assert i18n.translate("nav.soon_title", locale="en", platform="Amazon") == \
        "Importing Amazon is coming soon"
    assert i18n.translate("nav.soon_title", locale="fr", platform="Amazon") == \
        "L'import Amazon arrive bientôt"


def test_a_message_with_unmatched_placeholders_renders_rather_than_raises():
    # A catalogue entry edited without its {placeholder} must not 500 a page.
    assert i18n.translate("nav.soon_title", locale="en") == "Importing {platform} is coming soon"


def test_unknown_locale_falls_back_to_the_default():
    assert i18n.translate("nav.home", locale="de") == "Accueil"


def test_get_locale_defaults_to_french():
    i18n._current_locale.set(None)
    assert i18n.get_locale() == "fr"


def test_negotiate_prefers_an_explicit_choice():
    assert i18n.negotiate(accept_language="fr-FR,fr;q=0.9", explicit="en") == "en"


def test_negotiate_reads_the_accept_language_header_in_its_own_order():
    assert i18n.negotiate(accept_language="en-GB,en;q=0.9,fr;q=0.8") == "en"
    assert i18n.negotiate(accept_language="fr-CH,fr;q=0.9,en;q=0.8") == "fr"


def test_negotiate_skips_a_locale_we_have_no_catalogue_for():
    assert i18n.negotiate(accept_language="de-DE,de;q=0.9,en;q=0.8") == "en"


def test_negotiate_honours_a_refusal():
    # "fr;q=0" means "not acceptable"; the header is otherwise ordered already.
    assert i18n.negotiate(accept_language="fr;q=0,en") == "en"


def test_negotiate_falls_back_to_the_configured_default():
    assert i18n.negotiate(accept_language=None, default="en") == "en"
    assert i18n.negotiate(accept_language=None, default="nonsense") == "fr"


def test_display_label_uses_the_importers_label_by_default():
    # A type an importer names keeps that name when nothing overrides it.
    assert templating.display_label("app_used") == "Application ouverte"


def test_display_label_uses_the_catalogue_for_another_locale():
    i18n.set_locale("en")
    try:
        assert templating.display_label("app_used") == "App opened"
    finally:
        i18n.set_locale("fr")


def test_display_label_is_unchanged_for_a_type_the_catalogue_does_not_name():
    # An untranslated type falls through to the importer's label, then humanize.
    i18n.set_locale("en")
    try:
        assert templating.display_label("app_used") == "App opened"
    finally:
        i18n.set_locale("fr")
    assert templating.display_label("some_unknown_type") == "Some unknown type"


def test_view_label_translates_the_dashboard_tabs():
    i18n.set_locale("en")
    try:
        assert templating.view_label(platforms.PURCHASES) == "Purchases"
        assert templating.view_label(platforms.READING) == "Reading & listening"
    finally:
        i18n.set_locale("fr")
    assert templating.view_label(platforms.PURCHASES) == "Achats"


def test_export_hint_translates_where_to_ask_for_an_export():
    i18n.set_locale("en")
    try:
        assert templating.export_hint(platforms.get("amazon")) == \
            "Account › Privacy settings › Request your data"
    finally:
        i18n.set_locale("fr")
    assert templating.export_hint(platforms.get("amazon")) == \
        "Compte › Paramètres de confidentialité › Demander vos données"


def test_export_hint_without_a_key_keeps_the_platform_string():
    # Google's hint is a URL, the same in every language.
    assert templating.export_hint(platforms.get("google")) == "takeout.google.com"
