import json
import sys
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import anonymize, dedup, importers, influx_export, platforms, stats  # noqa: E402
from app.importers import zalando  # noqa: E402

# A made-up export in the shape of a real one: a flat zip of `<uuid>-<name>.csv`
# files, one of which (`orders-products.csv`) has NO header row, plus the
# empty `contact-information.csv` and a per-entity file per Zalando shop.
UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

MASTER = "\n".join([
    "Numéro de commande,Acheté le,Montant de la facture,Mode de paiement,Statut de la commande,"
    "Commande sur,Marques partenaires,Envoyé par,Numéro d'expédition,Adresse de livraison,Adresse de facturation",
    # Order-level facts the lines only reference by number. Its partner brand
    # carries the export's OTHER spelling of the attribute (`arial-hidden`,
    # not `aria-hidden`) and a <br> at the START of the cell.
    '1001,11 octobre 2026,"60,00 €",PayPal,Fermé,Zalando,'
    '"<br arial-hidden=\'true\'/><b>ACME</b>: 1 rue de la Marque, 75000, Paris, France",'
    '"Colissimo, 1 rue du Transport, 75000 Paris, FR",TRACK1,'
    '"Camille Durand, 12 rue des Lilas, 75000, Paris, France",'
    '"Camille Durand, 6 avenue des Peupliers, 75000, Paris, France"',
    # Returned in part: one line came back.
    '1002,15 août 2026,"42,50 €",Cartes cadeaux,Retour partiel,Zalando Privé,-,'
    '"Colissimo, 1 rue du Transport, 75000 Paris, FR",TRACK2,'
    '"Camille Durand, 12 rue des Lilas, 75000, Paris, France",'
    '"Camille Durand, 12 rue des Lilas, 75000, Paris, France"',
    # Two tracking numbers on one order.
    '1003,3 janvier 2025,"19,99 €",Carte de crédit / débit,Fermé,Zalando,-,'
    '"Colissimo, 1 rue du Transport, 75000 Paris, FR","TRACK3, TRACK4",'
    '"Camille Durand, 12 rue des Lilas, 75000, Paris, France",'
    '"Camille Durand, 12 rue des Lilas, 75000, Paris, France"',
    # Listed here, but with no line in orders-products.csv: not imported (see
    # test_an_order_without_its_lines_is_not_guessed_at).
    '1006,1er février 2026,"49,90 €",PayPal,Annulée,Zalando,-,'
    '"Colissimo, 1 rue du Transport, 75000 Paris, FR",TRACK6,'
    '"Camille Durand, 12 rue des Lilas, 75000, Paris, France",'
    '"Camille Durand, 12 rue des Lilas, 75000, Paris, France"',
])

# NO header row: the first line is the first article. Read with a DictReader it
# would be swallowed as column names, losing "Plain T-Shirt" and one of 1001's
# two lines (the order's invoice would then no longer match its lines).
PRODUCTS = "\n".join([
    '1001,A1,Plain T-Shirt,EU M,"35,00 €",2026-10-11,Articles gardés',
    '1001,A2,"Wide Fit Trainers  .<br aria-hidden=\'true\'><strong>Vendu par Next</strong>",'
    'EU 42,"25,00 €",2026-10-11,Articles gardés',
    '1002,A3,"Returned Jacket<br aria-hidden=\'true\'><strong>Vendu par ACME</strong>",'
    'EU L,"42,50 €",2026-08-15,Articles renvoyés',
    '1003,A5,Linen Shirt,EU S,"19,99 €",2025-01-03,Articles gardés',
    # An article whose order is missing from orders-master.csv: its ISO date is
    # all there is to date it by.
    '1004,A4,Orphan Socks,EU 40,"9,90 €",2024-05-06,Articles gardés',
])

LOGIN = "\n".join([
    "Adresse e-mail,Date et heure,Adresse IP",
    "owner@example.com,2026-07-08 06:13:36,203.0.113.7",
    "owner@example.com,2025-07-20 12:02:07,198.51.100.42",
])

ACCOUNT = "\n".join([
    "Numéro de client,Compte créé le,Nom,Adresse e-mail,Numéro de sécurité sociale,Numéro de téléphone,"
    "Préférences en matière de mode,Date de naissance,Catégorie client,Abonnement Plus,Comptes liés,"
    "Abonnements connectés",
    "98765432101,26 novembre 2021,Camille Durand,owner@example.com,-,0600000000,"
    "Pas de préférence,27 octobre 1994,A commandé,Oui,-,-",
])

ADDRESSES = "\n".join([
    "Nom,Adresses sauvegardées",
    'Camille Durand,"12 rue des Lilas, 75000 Paris FR"',
])

WISHLIST = "\n".join([
    "Numéro de l'article,Description de l'article,Taille sauvegardée",
    "A9,Acme - Corduroy Trousers,EU 32x32",
])

NEWSLETTER_HEADER = "Consentement de réception des e-mails,Préférences de newsletter enregistrées"

FILES = {
    f"{UUID}-orders-master.csv": MASTER,
    f"{UUID}-orders-products.csv": PRODUCTS,
    f"{UUID}-login.csv": LOGIN,
    f"{UUID}-account.csv": ACCOUNT,
    f"{UUID}-addresses.csv": ADDRESSES,
    f"{UUID}-wishlist.csv": WISHLIST,
    # Ships empty: a bare line break, not even a header row.
    f"{UUID}-contact-information.csv": "\r\n",
    # One file per Zalando shop.
    f"{UUID}-newsletter-lounge.csv": NEWSLETTER_HEADER + '\nNon,"Weekly deals, Top offers"',
    f"{UUID}-newsletter-zalando_se.csv": NEWSLETTER_HEADER + '\nOui,"New arrivals, Stories"',
    # Header row, no data: measurements never entered.
    f"{UUID}-size_fit-measurement_lounge.csv": "Trousers,Tops,Dresses",
}


@pytest.fixture()
def export(tmp_path):
    for name, content in FILES.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def items(export):
    return list(zalando.ZalandoImporter().parse(export))


def by_type(items, item_type):
    return [item for item in items if item.item_type == item_type]


def order(items, number):
    return [item for item in by_type(items, "zalando_order") if item.context == number]


def test_detected_as_zalando_only(export, tmp_path_factory):
    assert importers.detect(export).key == "zalando"
    elsewhere = tmp_path_factory.mktemp("other")
    (elsewhere / "orders.json").write_text("[]", encoding="utf-8")
    assert importers.detect(elsewhere) is None


def test_detected_from_one_file_of_a_partial_export(tmp_path):
    (tmp_path / "orders-master.csv").write_text(MASTER, encoding="utf-8")
    assert importers.detect(tmp_path).key == "zalando"


def test_a_generic_file_name_alone_does_not_claim_the_export(tmp_path):
    """`account.csv` / `login.csv` / `addresses.csv` belong to no platform in
    particular -- a zip of any CSVs could carry one. They only count wearing the
    export's own uuid prefix, which no other export has."""
    (tmp_path / "account.csv").write_text(ACCOUNT, encoding="utf-8")
    assert importers.detect(tmp_path) is None
    (tmp_path / f"{UUID}-account.csv").write_text(ACCOUNT, encoding="utf-8")
    assert importers.detect(tmp_path).key == "zalando"


def test_a_thousands_separator_does_not_truncate_an_amount(tmp_path):
    """Reading the number as ending at the first separator turned "1 234,56 €"
    into "1.00 EUR" -- a four-figure invoice reported as one euro."""
    (tmp_path / f"{UUID}-orders-products.csv").write_text(
        '7,A1,Wool Coat,EU M,"1 234,56 €",2026-01-02,Articles gardés', encoding="utf-8")
    line, = zalando.ZalandoImporter().parse(tmp_path)
    assert line.fields["Montant"] == "1234.56 EUR"
    assert stats.purchase_amount(line.fields) == 1234.56


def test_every_file_of_the_export_is_read(items):
    assert Counter(item.item_type for item in items) == {
        "zalando_order": 5, "zalando_return": 1, "login": 2, "wishlist_item": 1,
        "newsletter_preference": 2, "profile": 1,
    }


# ── Orders: `orders-products.csv` against `orders-master.csv` ──────────────

def test_the_header_less_products_file_keeps_its_first_line_as_data(items):
    """orders-products.csv is the one file with no header row: read as if it had
    one, its first article would be taken for column names -- losing it, and one
    of order 1001's two lines."""
    assert len(order(items, "1001")) == 2
    assert "Plain T-Shirt" in {item.content for item in order(items, "1001")}


def test_a_line_carries_its_own_price_not_the_orders_invoice(items):
    lines = order(items, "1001")
    assert {line.fields["Montant"] for line in lines} == {"35.00 EUR", "25.00 EUR"}
    # ...which together are the order's 60,00 € invoice (the export states both).
    assert sum(stats.purchase_amount(line.fields) for line in lines) == 60.0


def test_the_order_level_facts_are_joined_onto_each_of_its_lines(items):
    line, = [item for item in order(items, "1001") if item.content == "Plain T-Shirt"]
    assert line.fields["Boutique"] == "Zalando"
    assert line.fields["Statut"] == "Fermé"
    assert line.fields["Mode de paiement"] == "PayPal"
    assert line.fields["Expédié par"].startswith("Colissimo")
    assert line.fields["Suivi"] == "TRACK1"
    # The partner brand is marked up in <b>, spelled `arial-hidden` (the export's
    # typo) and followed by the brand's postal address.
    assert line.fields["Marque partenaire"] == "ACME"


def test_html_and_the_seller_are_split_out_of_a_product_name(items):
    line, = [item for item in order(items, "1001") if "Wide Fit" in (item.content or "")]
    # The markup, and the stray "  ." the export leaves before it, are gone.
    assert line.content == "Wide Fit Trainers"
    assert line.fields["Vendu par"] == "Next"
    assert line.fields["Taille"] == "EU 42"
    assert line.fields["Référence"] == "A2"
    assert line.fields["État de l'article"] == "Articles gardés"


def test_the_article_name_lands_in_content_where_the_purchases_page_reads_it(items):
    # stats.build_purchase_list takes a line's product from `content`.
    line, = [item for item in order(items, "1001") if item.content == "Plain T-Shirt"]
    assert line.title is None


def test_an_order_without_its_lines_is_not_guessed_at(items):
    """A master row with no article line is a shape the export has not been seen
    to produce. Standing in the invoice total for it would make the same order
    two unrelated records the day a later export brings the lines, and
    cross-import deduplication would then count both."""
    assert order(items, "1006") == []


def test_two_tracking_numbers_on_one_order_are_kept(items):
    line, = order(items, "1003")
    assert line.fields["Suivi"] == "TRACK3, TRACK4"
    assert line.fields["Montant"] == "19.99 EUR"


def test_a_line_whose_order_is_missing_keeps_its_iso_date(items):
    line, = order(items, "1004")
    assert line.timestamp == datetime(2024, 5, 6)
    assert "Boutique" not in line.fields  # no master row to take it from


# ── Returns, dated and priced as the export allows ────────────────────────

def test_a_returned_line_becomes_its_own_item(items):
    returned, = by_type(items, "zalando_return")
    assert returned.context == "1002"
    assert returned.content == "Returned Jacket"
    assert returned.fields["Montant remboursé"] == "42.50 EUR"
    assert returned.fields["Vendu par"] == "ACME"
    assert returned.category == "Retours"


def test_a_returned_line_is_still_an_order_line(items):
    """As Amazon's Order History keeps what was returned to it: the return is
    netted out of the spend, not removed from what was ordered."""
    line, = order(items, "1002")
    assert line.fields["État de l'article"] == "Articles renvoyés"
    assert line.fields["Statut"] == "Retour partiel"


def test_the_three_date_shapes_are_read(items):
    assert order(items, "1001")[0].timestamp == datetime(2026, 10, 11)   # French long form
    assert order(items, "1002")[0].timestamp == datetime(2026, 8, 15)    # and its août
    assert by_type(items, "login")[0].timestamp == datetime(2026, 7, 8, 6, 13, 36)  # ISO timestamp
    assert by_type(items, "profile")[0].timestamp == datetime(2021, 11, 26)


# ── The account: wishlist, newsletters, measurements, profile ─────────────

def test_the_wishlist_carries_no_invented_date(items):
    saved, = by_type(items, "wishlist_item")
    assert saved.title == "Acme - Corduroy Trousers"
    assert saved.timestamp is None  # the export dates nothing in it
    assert saved.fields == {"Référence": "A9", "Taille": "EU 32x32"}


def test_one_newsletter_item_per_zalando_shop(items):
    newsletters = by_type(items, "newsletter_preference")
    assert {item.context for item in newsletters} == {"lounge", "zalando_se"}
    assert {item.title for item in newsletters} == {"Newsletter Lounge", "Newsletter Zalando SE"}
    assert [item.fields["Consentement e-mail"] for item in newsletters] == ["Non", "Oui"]


def test_the_size_fit_file_yields_nothing_until_measurements_exist(items):
    # Its header row is there, but the real export has not one data row.
    assert by_type(items, "size_fit") == []


def test_the_profile_keeps_the_account_and_its_address_book(items):
    profile, = by_type(items, "profile")
    assert profile.title == "Camille Durand"
    assert profile.context == "98765432101"
    assert profile.fields["Compte créé le"] == "26/11/2021"
    assert profile.fields["Date de naissance"] == "27/10/1994"
    assert profile.fields["Abonnement Plus"] == "Oui"
    assert profile.fields["Adresses"] == "12 rue des Lilas, 75000 Paris FR"
    # "-" is how the export writes "not set": not a value.
    assert "Numéro de sécurité sociale" not in profile.fields
    assert "Comptes liés" not in profile.fields


def test_a_login_keeps_the_ip_the_export_states(items):
    logins = by_type(items, "login")
    assert [item.fields["Adresse IP"] for item in logins] == ["203.0.113.7", "198.51.100.42"]
    assert logins[0].fields["E-mail"] == "owner@example.com"


# ── How the purchases page reads them ──────────────────────────────────────

def test_the_purchases_page_is_told_about_zalando(items):
    assert stats.ZALANDO_ORDER_TYPE in stats.PURCHASE_LINE_ITEM_TYPES
    assert stats.ZALANDO_RETURN_TYPE in stats.RETURN_ITEM_TYPES
    assert stats.purchase_line_type("zalando") == stats.ZALANDO_ORDER_TYPE
    view_keys = [view.key for view in platforms.available_views({item.item_type for item in items})]
    assert "purchases" in view_keys


def test_the_category_dimension_is_the_shop_not_a_digital_purchase(items):
    """purchase_category falls through to "Numérique" for an item type it does
    not know -- which would mislabel every Zalando line, InfluxDB's `department`
    tag included."""
    assert stats.purchase_category("zalando_order", {"Boutique": "Zalando Privé"}) == "Zalando Privé"
    assert stats.purchase_category("zalando_order", {}) == "Sans boutique"
    assert stats.purchase_category("zalando_order", {"Boutique": "Zalando"}) != "Numérique"


def test_the_dimensions_are_the_ones_zalando_can_fill():
    assert set(stats.purchase_group_dimensions("zalando")) == {
        "category", "none", "shipping_address", "billing_address", "payment_method", "status",
    }
    assert stats.purchase_group_dimensions("zalando")["category"] == "Boutique"
    assert stats.purchase_group("zalando_order", {"Boutique": "Zalando"}, "category") == "Zalando"
    # A dimension Zalando has no column for is not offered to it.
    assert "product_condition" not in stats.purchase_group_dimensions("zalando")


def test_a_returned_line_is_netted_out_of_the_spend():
    rows = [
        (0, datetime(2026, 10, 1), "zalando_order", "1001", "Plain T-Shirt", json.dumps({"Montant": "60.00 EUR", "Boutique": "Zalando"})),
        (1, datetime(2026, 10, 5), "zalando_return", "1001", "Plain T-Shirt", json.dumps({"Montant remboursé": "25.00 EUR"})),
    ]
    dashboard = stats.build_purchase_dashboard(rows, "month", "category", ["Zalando"])
    assert dashboard["total_spend"] == 60.0
    assert dashboard["total_refunded"] == 25.0
    assert dashboard["total_returns"] == 1
    assert dashboard["returns_amount_series"] == [-25.0]
    assert dashboard["cumulative_net_series"] == [35.0]


def test_zalando_spend_is_exported_under_its_own_kind():
    assert influx_export._KIND_BY_PURCHASE["zalando_order"] == "fashion"


def test_dedup_forgets_the_status_but_keeps_the_size():
    """An order turns "Fermé" into "Retour partiel" between two exports; two
    same-named lines of one order differing by size must stay distinct."""
    def signature(fields):
        return dedup.item_signature(
            "zalando", "zalando_order", "Commandes", datetime(2026, 10, 11), None,
            "Plain T-Shirt", None, "1001", json.dumps(fields),
        )

    base = {"Montant": "35.00 EUR", "Taille": "EU M", "Statut": "Fermé", "État de l'article": "Articles gardés"}
    assert signature(base) == signature({**base, "Statut": "Retour partiel",
                                          "État de l'article": "Articles renvoyés"})
    assert signature(base) != signature({**base, "Taille": "EU L"})
    assert signature(base) != signature({**base, "Montant": "25.00 EUR"})


# ── The anonymized copy ────────────────────────────────────────────────────

def test_the_anonymizer_scrubs_the_line_the_header_less_file_starts_with(export, tmp_path_factory):
    """The header-less file has no header to keep: reading its first line as one
    would write a real order id, article number and product name out verbatim.

    This asserts only what the fix is responsible for -- that nothing of the
    export's content survives, the first line included. It does NOT assert the
    anonymized copy re-imports to the same items: `_pseudo_amount` replaces a
    numeric order number without memoizing it, so `orders-master.csv` and
    `orders-products.csv` no longer agree on it and the join, the returns and
    the amounts are lost. That is how the anonymizer already treats every
    multi-file export (Amazon's Order History vs Refund Details included), not
    something Zalando introduced."""
    out = tmp_path_factory.mktemp("anon")
    assert anonymize.anonymize_export(export, out / "anon.zip", source_key="zalando") == []
    with zipfile.ZipFile(out / "anon.zip") as zf:
        names = zf.namelist()
        zf.extractall(out / "files")
    # Filenames are never renamed, so the export is still recognized as Zalando.
    assert f"{UUID}-orders-products.csv" in names
    assert importers.detect(out / "files").key == "zalando"

    products = (out / "files" / f"{UUID}-orders-products.csv").read_text(encoding="utf-8")
    # Not one of the four articles leaked -- the FIRST line included.
    for leaked in ("Plain T-Shirt", "Wide Fit Trainers", "Returned Jacket", "Orphan Socks"):
        assert leaked not in products
    # Still five data rows: no header row was added in front of them.
    assert len([line for line in products.splitlines() if line.strip()]) == 5


# ── The pages themselves ───────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from fastapi.testclient import TestClient

    from app import processing
    from app.database import Base, SessionLocal, engine
    from app.main import app
    from app.models import ImportBatch, ImportStatus

    root = tmp_path_factory.mktemp("zalando-export")
    for name, content in FILES.items():
        (root / name).write_text(content, encoding="utf-8")
    Base.metadata.create_all(engine)
    db = SessionLocal()
    batch = ImportBatch(original_filename="zalando_export.zip", status=ImportStatus.parsing)
    db.add(batch)
    db.commit()
    processing._detect_and_parse(db, batch, root)
    db.close()
    return TestClient(app)


def test_the_platform_page_offers_the_purchases_tab(client):
    html = client.get("/p/zalando").text
    assert 'href="/p/zalando/purchases"' in html
    # No Seazon tab: those read item types Zalando never produces.
    assert 'href="/p/zalando/formulas"' not in html


def test_the_purchases_page_groups_by_shop(client):
    html = client.get("/p/zalando/purchases").text
    assert "Par boutique" in html
    # 60,00 + 42,50 + 19,99 + 9,90 spent (the four orders), 42,50 of it back.
    assert "132,39" in html
    assert "42,50" in html


def test_an_order_opens_as_all_its_lines(client):
    html = client.get("/browse", params={"source": "zalando", "item_type": "zalando_order", "context": "1001"}).text
    assert "Plain T-Shirt" in html and "Wide Fit Trainers" in html
    assert "Returned Jacket" not in html  # another order's line
