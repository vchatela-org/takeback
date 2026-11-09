import json
import sys
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import anonymize, dedup, importers, influx_export, platforms, stats  # noqa: E402
from app.importers import aliexpress, xlsx  # noqa: E402

# A made-up export in the shape of a real one: one workbook in a folder named
# after the request, and the profile picture next to it. Every value is a
# placeholder.
ORDER_HEADER = [
    "order_id", "parent_orderid", "receiver_mobileno", "receiver_mobileprefixcode", "receiver_contractname",
    "receiveaddress_countrycode", "receiveaddress_countryname", "receiveaddress_postcode", "receiveaddress_city",
    "receiveaddress_state", "receiveaddress_locationtreeaddressname", "receiveaddress_detailaddress",
    "receiveaddress_languagecode", "receiveaddressaddress2", "gmt_create_order_time", "order_status",
    "frozen_status", "item_name", "unit_price", "unit_price_currency_code", "payable_amt",
    "payable_amt_currency_code", "buyer_admin_id", "gmt_pay_order_time", "is_success_pay", "is_send_goods",
    "is_buyer_accept", "gmt_trade_end_time", "end_reason",
]


def order_row(order_id, parent, name, unit, payable, currency, created, status="FINISH",
              end_reason="buyer_accept_goods", paid="true", ended="2026-02-01 10:00:00"):
    values = {
        "order_id": order_id, "parent_orderid": parent, "receiver_mobileno": "0600000000",
        "receiver_mobileprefixcode": "+33", "receiver_contractname": "Jane Doe", "receiveaddress_countrycode": "FR",
        "receiveaddress_countryname": "France", "receiveaddress_postcode": "12345",
        "receiveaddress_city": "Springfield", "receiveaddress_state": "Region",
        "receiveaddress_locationtreeaddressname": "Region,Springfield",
        "receiveaddress_detailaddress": "1 Example Street", "receiveaddress_languagecode": "en",
        "receiveaddressaddress2": "", "gmt_create_order_time": created, "order_status": status,
        "frozen_status": "0", "item_name": name, "unit_price": unit, "unit_price_currency_code": currency,
        "payable_amt": payable, "payable_amt_currency_code": currency, "buyer_admin_id": "1000",
        "gmt_pay_order_time": created if paid == "true" else "", "is_success_pay": paid,
        "is_send_goods": "true", "is_buyer_accept": "true" if end_reason == "buyer_accept_goods" else "false",
        "gmt_trade_end_time": ended if status == "FINISH" else "", "end_reason": end_reason,
    }
    return [values[column] for column in ORDER_HEADER]


ORDERS = [ORDER_HEADER,
          # One checkout (900) holding two lines from two sellers.
          order_row("101", "900", "USB Cable", "650", "1020", "EUR", "2026-01-10 08:00:00"),
          order_row("102", "900", "Level Shifter", "300", "880", "EUR", "2026-01-10 08:00:00"),
          # Paid in dollars, a coupon below the unit price; disputed below.
          order_row("103", "901", "Soldering Tip", "210", "175", "USD", "2026-01-15 09:30:00"),
          # Still on its way.
          order_row("104", "902", "Dev Board", "2500", "2950", "EUR", "2026-11-03 14:20:00",
                    status="WAIT_BUYER_ACCEPT_GOODS", end_reason=""),
          # Never paid: an abandoned checkout.
          order_row("105", "903", "Unpaid Gadget", "1500", "1500", "EUR", "2025-07-01 12:00:00",
                    end_reason="buyer_pay_timeout", paid="false"),
          # Paid, then the warehouse could not ship it: refunded in full.
          order_row("106", "904", "Stuck Parcel", "820", "820", "USD", "2024-08-30 10:00:00",
                    end_reason="warehouse_unable_sendout", ended="2024-09-02 11:00:00"),
          # Disputed with a refund that states no amount.
          order_row("107", "905", "Broken Lamp", "640", "640", "EUR", "2024-03-01 10:00:00")]

ISSUE_HEADER = ["actionTime", "buyerId", "reverseOrderLineId", "order_id", "issue_reason",
                "actionFileAttachments", "action", "operatorType", "solutionType", "formatMoney", "actionMemo"]


def issue(time, line, order, action, operator, solution="", money="", memo="", attachments=""):
    return [time, "1000", line, order, "Item defective or doesn't work", attachments, action, operator,
            solution, money, memo]


_DISPUTE_103 = [
    issue("2026-01-20 10:00:01", "R1", "103", "Your request has been submitted", "Buyer", memo="It does not heat."),
    issue("2026-01-20 10:00:02", "R1", "103", "Your withdrawal request has been received", "Buyer",
          '["Return refunds"]', '["US $1.75"]', attachments='["https://example.com/photo.jpg"]'),
    issue("2026-01-20 11:00:00", "R1", "103", "The refund has been approved", "Customer Service",
          '["Refund only"]', '["US $1.75"]'),
    issue("2026-01-20 11:00:00", "R1", "103", "A solution has been reached for your request", "Customer Service"),
]
ISSUES = [ISSUE_HEADER,
          # The export writes every event twice: so does this fixture.
          *_DISPUTE_103, *_DISPUTE_103,
          issue("2024-03-05 10:00:00", "R2", "107", "Your withdrawal request has been received", "Buyer",
                '["Return refunds"]', '["€ 6,40"]'),
          issue("2024-03-05 10:05:00", "R2", "107", "The refund has been approved", "Customer Service", "[]", "[]")]

SHEETS = {
    "Member Information": [
        ["accountId", "aeRewardLevel", "country", "email", "firstName", "lastName", "loginMobile", "memberId",
         "newAeRewardLevel", "registerTime", "totalScore", "vipLevel"],
        ["1000", "A1", "FR", "jane@example.com", "Jane", "Doe", "+15550100", "fr1000abcd", "Silver Member",
         "2015-04-02 10:00:00", "120", "0"],
    ],
    "Order Information": ORDERS,
    "LogisticsAddress Information": [
        ["country", "buyerAddressType", "ownerSeq", "province", "zip", "district", "countryName", "city",
         "address", "address2", "contactPerson", "platform", "phoneCountry", "mobileNo", "gmtCreate", "gmtModified"],
        ["FR", "residential", "1000", "Region", "12345", "", "France", "Springfield", "1 Example Street", "",
         "Jane Doe", "PC", "+33", "0600000000", "2022-10-01 09:00:00", "2026-08-01 12:00:00"],
    ],
    "Issue Information": ISSUES,
    "Evaluate Information": [
        ["buyerEvaluationDate", "buyerEvaluationText", "customerService", "itemAsDescribed", "shippingService",
         "evaluationId", "orderId", "productId"],
        ["2026-01-12 10:00:00", "Works as described.", "5", "4", "5", "5000", "101", "1005000000000001"],
    ],
    "QA Information": [
        ["createTime", "interlocutionId", "interlouctionType", "productId", "content"],
        ["2021-02-01 15:00:00", "800", "QUESTION", "32000000001", "Does it fit a 2 mm wire?"],
    ],
    "UGC Information": [
        ["nickName", "memberSeq", "contactName", "avatar"],
        ["Jane_D", "1000", "Jane Doe", "https://example.com/avatar.jpg"],
    ],
}

WORKBOOK = "12345678/aliexpress_user_data_backup_20261109.xlsx"
AVATAR = "12345678/Member_Attachments/avatar.jpg_120x120.jpg"


def write_export(root: Path) -> Path:
    (root / WORKBOOK).parent.mkdir(parents=True, exist_ok=True)
    (root / WORKBOOK).write_bytes(xlsx.write_workbook(SHEETS))
    (root / AVATAR).parent.mkdir(parents=True, exist_ok=True)
    (root / AVATAR).write_bytes(b"\xff\xd8\xff")
    return root


@pytest.fixture()
def export(tmp_path):
    return write_export(tmp_path)


@pytest.fixture()
def items(export):
    return list(aliexpress.AliExpressImporter().parse(export))


def by_type(items, item_type):
    return [item for item in items if item.item_type == item_type]


def line(items, number):
    return next(item for item in items
                if item.item_type.startswith("aliexpress_") and item.item_type.endswith("order")
                and (item.fields or {}).get("N° de commande") == number)


# ── The workbook ───────────────────────────────────────────────────────────

def test_a_workbook_with_shared_strings_and_gaps_is_read(tmp_path):
    """What a spreadsheet app writes, not what write_workbook does: shared
    strings (one split into rich-text runs), an empty cell left out entirely,
    a number cell, a boolean, and a sheet target given from the package root."""
    path = tmp_path / "book.xlsx"
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/workbook.xml", (
            f'<workbook xmlns="{main}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Order Information" sheetId="1" r:id="rId7"/></sheets></workbook>'))
        zf.writestr("xl/_rels/workbook.xml.rels", (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId7" Type="worksheet" Target="/xl/worksheets/data.xml"/></Relationships>'))
        zf.writestr("xl/sharedStrings.xml", (
            f'<sst xmlns="{main}"><si><t>order_id</t></si><si><t>item_name</t></si><si><t>flag</t></si>'
            '<si><r><t>Rich </t></r><r><t>text</t></r></si></sst>'))
        zf.writestr("xl/worksheets/data.xml", (
            f'<worksheet xmlns="{main}"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="D1" t="s"><v>2</v></c></row>'
            '<row r="2"><c r="A2"><v>101</v></c><c r="B2" t="s"><v>3</v></c><c r="D2" t="b"><v>1</v></c></row>'
            '</sheetData></worksheet>'))
    sheets = xlsx.read_sheets(path)
    assert sheets["Order Information"] == [["order_id", "item_name", "", "flag"], ["101", "Rich text", "", "true"]]
    assert xlsx.records(sheets["Order Information"]) == [{"order_id": "101", "item_name": "Rich text", "flag": "true"}]


def test_what_write_workbook_writes_reads_back(tmp_path):
    rows = [["a", "b"], ["x & <y>", ""], ["", "z\x01"]]
    path = tmp_path / "book.xlsx"
    path.write_bytes(xlsx.write_workbook({"Sheet": rows}))
    assert xlsx.read_sheets(path) == {"Sheet": [["a", "b"], ["x & <y>"], ["", "z"]]}


def test_a_part_bigger_than_its_budget_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "book.xlsx"
    path.write_bytes(xlsx.write_workbook({"Sheet": [["a" * 5000]]}))
    monkeypatch.setattr(xlsx, "MAX_PART_BYTES", 1000)
    with pytest.raises(xlsx.WorkbookError):
        xlsx.read_sheets(path)


def test_a_file_that_is_not_a_workbook_is_skipped(tmp_path):
    (tmp_path / "aliexpress_user_data_backup_1.xlsx").write_text("not a zip", encoding="utf-8")
    assert list(aliexpress.AliExpressImporter().parse(tmp_path)) == []


# ── Detection ──────────────────────────────────────────────────────────────

def test_detected_as_aliexpress_only(export, tmp_path_factory):
    assert importers.detect(export).key == "aliexpress"
    elsewhere = tmp_path_factory.mktemp("other")
    (elsewhere / "budget.xlsx").write_bytes(xlsx.write_workbook({"Sheet": [["a"]]}))
    assert importers.detect(elsewhere) is None


def test_aliexpress_is_no_longer_only_planned():
    assert platforms.is_supported("aliexpress")
    assert "aliexpress" not in [platform.key for platform in platforms.planned()]
    assert "aliexpress" in [platform.key for platform in platforms.supported()]


# ── Orders ─────────────────────────────────────────────────────────────────

def test_every_sheet_is_read(items):
    assert Counter(item.item_type for item in items) == {
        "aliexpress_order": 6, "aliexpress_unpaid_order": 1, "aliexpress_dispute": 2, "aliexpress_refund": 3,
        "aliexpress_review": 1, "aliexpress_question": 1, "saved_address": 1, "profile": 1,
    }


def test_amounts_are_minor_units_and_the_line_costs_what_was_paid(items):
    cable = line(items, "101")
    assert cable.fields["Montant"] == "10.20 EUR"
    assert cable.fields["Prix unitaire"] == "6.50 EUR"
    # A coupon brought it below its unit price: the spend is what was paid.
    assert line(items, "103").fields["Montant"] == "1.75 USD"


def test_the_checkout_is_the_order_and_the_line_keeps_its_number(items):
    checkout = [item for item in by_type(items, "aliexpress_order") if item.context == "900"]
    assert sorted(item.content for item in checkout) == ["Level Shifter", "USB Cable"]
    assert line(items, "102").fields["N° de commande"] == "102"


def test_an_order_line_carries_its_state_date_and_address(items):
    cable = line(items, "101")
    assert cable.timestamp == datetime(2026, 1, 10, 8, 0)
    assert cable.fields["Statut"] == "Reçue"
    assert cable.fields["Clôture"] == "Réception confirmée"
    assert cable.fields["Adresse de livraison"] == "1 Example Street, 12345 Springfield, France"
    assert line(items, "104").fields["Statut"] == "En cours de livraison"
    assert line(items, "106").fields["Statut"] == "Annulée"


def test_an_unpaid_line_is_not_a_purchase(items):
    unpaid = line(items, "105")
    assert unpaid.item_type == "aliexpress_unpaid_order"
    assert "Montant" not in unpaid.fields
    assert unpaid.fields["Montant dû"] == "15.00 EUR"
    assert unpaid.fields["Statut"] == "Non payée"
    assert "aliexpress_unpaid_order" not in stats.PURCHASE_ITEM_TYPES


# ── Disputes and refunds ───────────────────────────────────────────────────

def test_a_dispute_written_twice_is_one_dispute_with_its_timeline_once(items):
    dispute = next(item for item in by_type(items, "aliexpress_dispute") if item.fields["N° de commande"] == "103")
    assert dispute.context == "901"
    assert dispute.content == "Soldering Tip"
    assert dispute.title == "Item defective or doesn't work"
    assert dispute.timestamp == datetime(2026, 1, 20, 10, 0, 1)
    assert len(dispute.fields["Historique"].splitlines()) == 4
    assert dispute.fields["Montant demandé"] == "1.75 USD"
    assert dispute.fields["Montant remboursé"] == "1.75 USD"
    assert dispute.fields["Solution demandée"] == "Retour et remboursement"
    assert dispute.fields["Messages"] == "It does not heat."
    assert dispute.fields["Pièces jointes"] == "https://example.com/photo.jpg"


def test_an_approved_refund_becomes_a_refund_once(items):
    refunds = [item for item in by_type(items, "aliexpress_refund") if item.fields["N° de commande"] == "103"]
    assert len(refunds) == 1
    assert refunds[0].fields["Montant remboursé"] == "1.75 USD"
    assert refunds[0].fields["Solution"] == "Remboursement seul"
    assert refunds[0].timestamp == datetime(2026, 1, 20, 11, 0)
    assert refunds[0].context == "901"


def test_a_refund_stating_no_amount_takes_the_requested_one(items):
    refund = next(item for item in by_type(items, "aliexpress_refund") if item.fields["N° de commande"] == "107")
    assert refund.fields["Montant remboursé"] == "6.40 EUR"
    assert refund.fields["Origine du montant"] == "Montant d'après la demande"


def test_a_paid_order_closed_before_delivery_is_refunded_in_full(items):
    refund = next(item for item in by_type(items, "aliexpress_refund") if item.fields["N° de commande"] == "106")
    assert refund.fields["Montant remboursé"] == "8.20 USD"
    assert refund.fields["Origine"] == "L'entrepôt n'a pas pu l'expédier"
    assert refund.timestamp == datetime(2024, 9, 2, 11, 0)
    # The unpaid one gives nothing back: nothing was paid.
    assert not [item for item in by_type(items, "aliexpress_refund") if item.fields["N° de commande"] == "105"]


def test_a_closing_refund_does_not_repeat_a_dispute_refund():
    rows = [ORDER_HEADER, order_row("201", "950", "Lamp", "500", "500", "EUR", "2024-01-01 10:00:00",
                                    end_reason="in-sale-return-close-order")]
    importer = aliexpress.AliExpressImporter()
    assert len(list(importer._closing_refunds(xlsx.records(rows), {"201"}))) == 0
    assert len(list(importer._closing_refunds(xlsx.records(rows), set()))) == 1


def test_the_site_money_formats_are_read():
    assert aliexpress.parse_money("US $120.40") == (120.40, "USD")
    assert aliexpress.parse_money("€ 1.234,56") == (1234.56, "EUR")
    assert aliexpress.parse_money("12.00", "EUR") == (12.0, "EUR")
    assert aliexpress.parse_money("[]") is None


# ── The rest of the account ────────────────────────────────────────────────

def test_a_review_names_its_article_and_links_its_product(items):
    review = by_type(items, "aliexpress_review")[0]
    assert review.content == "Works as described."
    assert review.title == "USB Cable"
    assert review.context == "900"
    assert review.fields["Note : conforme à la description"] == "4/5"
    assert review.fields["Lien"] == "https://www.aliexpress.com/item/1005000000000001.html"


def test_a_question_links_its_product(items):
    question = by_type(items, "aliexpress_question")[0]
    assert question.content == "Does it fit a 2 mm wire?"
    assert question.fields == {"Type": "Question", "Lien": "https://www.aliexpress.com/item/32000000001.html"}


def test_a_saved_address_has_the_shape_of_amazons(items):
    address = by_type(items, "saved_address")[0]
    assert address.content == "Jane Doe"
    assert address.context == "1 Example Street"
    assert address.fields["Ville"] == "12345 Springfield"
    assert address.timestamp == datetime(2022, 10, 1, 9, 0)


def test_the_profile_keeps_the_account_and_its_picture(items):
    profile = by_type(items, "profile")[0]
    assert profile.title == "Jane Doe"
    assert profile.fields["E-mail"] == "jane@example.com"
    assert profile.fields["Pseudo"] == "Jane_D"
    assert profile.fields["Niveau"] == "Silver Member"
    assert profile.fields["Inscrit le"] == "02/04/2015"
    assert profile.media == [(AVATAR, "photo")]


# ── How the purchases page reads them ──────────────────────────────────────

def test_the_purchases_page_is_told_about_aliexpress(items):
    assert stats.ALIEXPRESS_ORDER_TYPE in stats.PURCHASE_LINE_ITEM_TYPES
    assert stats.ALIEXPRESS_REFUND_TYPE in stats.RETURN_ITEM_TYPES
    assert stats.purchase_line_type("aliexpress") == stats.ALIEXPRESS_ORDER_TYPE
    view_keys = [view.key for view in platforms.available_views({item.item_type for item in items})]
    assert "purchases" in view_keys


def test_the_category_dimension_is_the_currency():
    """Dollars are never added to euros within one group."""
    assert stats.purchase_group_dimensions("aliexpress") == {
        "category": "Devise", "none": "Aucun (total)", "shipping_address": "Adresse de livraison", "status": "Statut",
    }
    assert stats.purchase_category("aliexpress_order", {"Montant": "1.75 USD"}) == "USD"
    assert stats.purchase_category("aliexpress_order", {}) == "Sans devise"


def test_a_refund_is_netted_out_of_the_spend():
    rows = [
        (0, datetime(2026, 1, 15), "aliexpress_order", "901", "Tip", json.dumps({"Montant": "10.00 USD"})),
        (1, datetime(2026, 1, 20), "aliexpress_refund", "901", "Tip", json.dumps({"Montant remboursé": "4.00 USD"})),
    ]
    dashboard = stats.build_purchase_dashboard(rows, "month", "category", ["USD"])
    assert dashboard["total_spend"] == 10.0
    assert dashboard["total_refunded"] == 4.0
    assert dashboard["cumulative_net_series"] == [6.0]


def test_aliexpress_spend_is_exported_under_its_own_kind():
    assert influx_export._KIND_BY_PURCHASE["aliexpress_order"] == "marketplace"


def test_dedup_forgets_the_state_but_not_the_amount():
    def signature(item_type, fields):
        return dedup.item_signature("aliexpress", item_type, "Commandes", datetime(2026, 11, 6), None,
                                    "Dev Board", None, "902", json.dumps(fields))

    base = {"Montant": "29.50 EUR", "Statut": "En cours de livraison"}
    later = {**base, "Statut": "Reçue", "Clôture": "Réception confirmée", "Clôturée le": "20/11/2026"}
    assert signature("aliexpress_order", base) == signature("aliexpress_order", later)
    assert signature("aliexpress_order", base) != signature("aliexpress_order", {**base, "Montant": "1.00 EUR"})
    # A dispute's timeline grows until it is settled.
    assert signature("aliexpress_dispute", {"Historique": "a"}) == signature("aliexpress_dispute", {"Historique": "a\nb"})


# ── The anonymized copy ────────────────────────────────────────────────────

def test_the_anonymized_workbook_still_imports_without_the_real_values(export, tmp_path_factory):
    out = tmp_path_factory.mktemp("anon")
    assert anonymize.anonymize_export(export, out / "anon.zip", source_key="aliexpress") == []
    with zipfile.ZipFile(out / "anon.zip") as zf:
        zf.extractall(out / "files")
    workbook = out / "files" / WORKBOOK
    cells = "\n".join(cell for rows in xlsx.read_sheets(workbook).values() for row in rows for cell in row)
    for leaked in ("Jane", "Doe", "Example Street", "Springfield", "jane@example.com", "0600000000",
                   "USB Cable", "It does not heat", "Works as described", "https://example.com",
                   "1005000000000001"):
        assert leaked not in cells
    # The states, flags and currencies the importer reads survive...
    assert "WAIT_BUYER_ACCEPT_GOODS" in cells and "warehouse_unable_sendout" in cells and "USD" in cells

    items = list(importers.detect(out / "files").parse(out / "files"))
    assert Counter(item.item_type for item in items) == Counter(
        item.item_type for item in aliexpress.AliExpressImporter().parse(export))
    # ...and an identifier is replaced the same way on every sheet: the dispute
    # still finds its order line, so it still knows its checkout and article.
    orders = {item.context for item in by_type(items, "aliexpress_order")}
    disputes = by_type(items, "aliexpress_dispute")
    assert all(dispute.context in orders and dispute.content for dispute in disputes)


# ── The pages themselves ───────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from fastapi.testclient import TestClient

    from app import processing
    from app.database import Base, SessionLocal, engine
    from app.main import app
    from app.models import ImportBatch, ImportStatus

    root = write_export(tmp_path_factory.mktemp("aliexpress-export"))
    Base.metadata.create_all(engine)
    db = SessionLocal()
    batch = ImportBatch(original_filename="37899022.zip", status=ImportStatus.parsing)
    db.add(batch)
    db.commit()
    processing._detect_and_parse(db, batch, root)
    db.close()
    return TestClient(app)


def test_the_platform_page_offers_the_purchases_tab(client):
    html = client.get("/p/aliexpress").text
    assert 'href="/p/aliexpress/purchases"' in html


def test_the_purchases_page_groups_by_currency(client):
    html = client.get("/p/aliexpress/purchases").text
    assert "Par devise" in html
    # Euros and dollars are shown side by side, the page converting nothing.
    assert "EUR" in html and "USD" in html


def test_a_checkout_opens_as_all_its_lines(client):
    html = client.get("/browse", params={"source": "aliexpress", "item_type": "aliexpress_order",
                                         "context": "900"}).text
    assert "USB Cable" in html and "Level Shifter" in html
    assert "Soldering Tip" not in html
