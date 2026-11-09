"""AliExpress's data export (privacy.aliexpress.com › Request a copy of your data).

An AliExpress export is ONE Excel workbook, `aliexpress_user_data_backup_<date>
.xlsx`, inside a folder named after the request's number, next to a
`Member_Attachments/` folder holding the profile picture. Unlike every other
supported export it is neither CSV nor JSON: each kind of record is a sheet of
the workbook (read by app/importers/xlsx.py, no third-party dependency). No
schema is published; everything below was read off a real French account's
export (11/2026, 7 sheets, 399 order lines from 2016 to 2026) and field names
are taken verbatim.

| Sheet | One row is | Becomes |
|---|---|---|
| Order Information | one order line (a "sub-order": one article, one seller) | `aliexpress_order` |
| Issue Information | one EVENT of a dispute (opened, escalated, refunded...) | `aliexpress_dispute`, `aliexpress_refund` |
| Evaluate Information | a review left on an article | `aliexpress_review` |
| QA Information | a question asked on a product page | `aliexpress_question` |
| LogisticsAddress Information | a saved delivery address | `saved_address` |
| Member Information + UGC Information | the account and its public nickname | `profile` |

Points that need a word of explanation:

* **Amounts are integers in minor units**: `unit_price` 650 with currency EUR
  is 6.50 EUR. A dispute's refund on the same export confirms it (a line of
  `payable_amt` 250 USD is refunded "US $2.50"). The currency is per line, and
  an account's history mixes them (USD for checkouts priced in dollars, EUR
  for the others) -- they are never converted, so the purchases page groups
  by currency (see app/stats.py) rather than adding dollars to euros.

* **What an order line cost is `payable_amt`**, not `unit_price`: the line has
  no quantity column, and what was paid differs from the unit price both ways
  (coupons below it, shipping or several units above it). That is the amount
  the spend is made of. It is not always what a refund gives back: two refunds
  of the real export exceed the payable amount of their line (by its shipping
  or taxes, presumably -- the export does not say), and each is taken as
  stated.

* **One checkout, several sub-orders.** `order_id` is one line,
  `parent_orderid` the checkout it was paid in. The checkout is the `context`
  (what the purchases page counts as an order, and what an item page opens as
  "the whole order"); the line's own number stays in its fields, and is what a
  dispute or a review refers to.

* **A line never paid is not a purchase.** Abandoned checkouts (`is_success_pay`
  false: "buyer_pay_timeout", "buyer_cancel_notpay_order") are kept, as their
  own `aliexpress_unpaid_order`, out of the spend and out of the order count.

* **Every dispute event is in the export twice**, byte for byte. Rows are
  de-duplicated before anything is counted. A dispute (one `reverseOrderLineId`)
  becomes one item carrying its timeline; when one of its events is "The refund
  has been approved", it also yields an `aliexpress_refund` dated by it, for
  the amount the event states ("US $2.50", "€ 6,40"). Four approvals of the real
  export state no amount (`[]`): the amount the buyer asked for when opening
  the dispute stands in for it -- on every approval that does state one, the
  two are equal -- and the item says so ("Montant d'après la demande").

* **An order closed before it was received was refunded in full**, without any
  dispute: AliExpress closes and refunds a paid order the seller cancels, the
  warehouse cannot ship, stock runs out for, or the carrier cannot deliver
  (`end_reason`). Such a line yields an `aliexpress_refund` of its own payable
  amount, dated by its closing -- the only figure and date the export gives for
  it -- unless a dispute of the same line already refunded it.
"""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from app.importers import xlsx
from app.importers.base import BaseImporter, NormalizedItem
from app.importers.registry import register
from app.importers.utils import guess_media_type

# "aliexpress_user_data_backup_20261109.xlsx"
_WORKBOOK_RE = re.compile(r"^aliexpress.*\.xlsx$", re.IGNORECASE)

# Sheet names as the export writes them. Matched case- and space-insensitively.
MEMBER_SHEET = "Member Information"
ORDER_SHEET = "Order Information"
ADDRESS_SHEET = "LogisticsAddress Information"
ISSUE_SHEET = "Issue Information"
REVIEW_SHEET = "Evaluate Information"
QUESTION_SHEET = "QA Information"
UGC_SHEET = "UGC Information"

# A line's state: `order_status` while it runs, then `end_reason` once it is
# FINISHed. The labels are what the purchases page's "Par statut" dimension
# shows, so they stay coarse; the precise closing reason is a field of its own.
_RUNNING_STATES = {
    "PLACE_ORDER_SUCCESS": "En attente de paiement",
    "WAIT_BUYER_PAY": "En attente de paiement",
    "RISK_CONTROL": "Paiement en vérification",
    "FUND_PROCESSING": "Paiement en cours",
    "WAIT_SELLER_SEND_GOODS": "En attente d'expédition",
    "SELLER_PART_SEND_GOODS": "Expédiée en partie",
    "WAIT_BUYER_ACCEPT_GOODS": "En cours de livraison",
    "IN_ISSUE": "Litige en cours",
    "IN_CANCEL": "Annulation en cours",
    "IN_FROZEN": "Gelée",
}
_FINISHED = "FINISH"
# end_reason -> (Statut, Clôture). The ones seen on the real export; any other
# is kept verbatim as its closing reason, under a neutral "Terminée".
_END_REASONS = {
    "buyer_accept_goods": ("Reçue", "Réception confirmée"),
    "buyer_accept_goods_timeout": ("Reçue", "Réception confirmée automatiquement"),
    "cancel_order_close_trade": ("Annulée", "Commande annulée"),
    "warehouse_unable_sendout": ("Annulée", "L'entrepôt n'a pas pu l'expédier"),
    "out_of_stock_overtime_close_order": ("Annulée", "Rupture de stock"),
    "logistics_unReachable_close1": ("Annulée", "Livraison impossible"),
    "in-sale-return-close-order": ("Retournée", "Retour avant réception"),
    "seller_accept_issue_no_goods_return": ("Remboursée", "Remboursée sans retour"),
    "buyer_pay_timeout": ("Non payée", "Délai de paiement dépassé"),
    "buyer_cancel_notpay_order": ("Non payée", "Annulée avant paiement"),
}
# A PAID line closed for one of these never reached the buyer, and AliExpress
# gives its money back (see the module docstring).
_REFUNDED_CLOSINGS = {
    "cancel_order_close_trade", "warehouse_unable_sendout", "out_of_stock_overtime_close_order",
    "logistics_unReachable_close1", "in-sale-return-close-order",
}

_REFUND_APPROVED = "the refund has been approved"
_REQUEST_RECEIVED = "your withdrawal request has been received"
_SOLUTIONS = {"Refund only": "Remboursement seul", "Return refunds": "Retour et remboursement"}

# A dispute's money, as the site displays it: "US $2.50", "€ 6,40", "€ 1.234,56".
_MONEY_SYMBOLS = (
    ("US $", "USD"), ("US$", "USD"), ("C$", "CAD"), ("CA $", "CAD"), ("AU $", "AUD"), ("A$", "AUD"),
    ("€", "EUR"), ("£", "GBP"), ("¥", "JPY"), ("₽", "RUB"), ("R$", "BRL"), ("zł", "PLN"), ("$", "USD"),
)
_NUMBER_RE = re.compile(r"\d[\d\s.,]*\d|\d")

_PRODUCT_URL = "https://www.aliexpress.com/item/{}.html"


def _text(value: Any) -> Optional[str]:
    return value.strip() or None if isinstance(value, str) else None


def _sheet_key(name: str) -> str:
    return re.sub(r"\s+", "", name).lower()


def _parse_date(value: Any) -> Optional[datetime]:
    """"2026-01-10 08:00:00" -- every date of the export has this shape. An
    unparseable value is absent, never "now"."""
    text = _text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _date_fr(value: Any) -> Optional[str]:
    parsed = _parse_date(value)
    return parsed.strftime("%d/%m/%Y") if parsed else None


def _minor_units(value: Any) -> Optional[float]:
    """650 -> 6.50. A cell that is not a whole number is absent, never 0."""
    text = _text(value)
    if not text:
        return None
    try:
        return int(text) / 100
    except ValueError:
        try:
            return int(float(text)) / 100 if float(text).is_integer() else None
        except ValueError:
            return None


def _money(amount: Optional[float], currency: Optional[str]) -> Optional[str]:
    """Stored the way every amount in the app is ("6.50 EUR"), so
    stats.extract_leading_number / extract_currency read it back."""
    return None if amount is None else f"{amount:.2f} {currency or ''}".strip()


def _json_list(value: Any) -> List[str]:
    """`solutionType` / `formatMoney` / `actionFileAttachments` are JSON lists
    written as text ('["Refund only"]'); empty, "[]" or unparseable -> []."""
    text = _text(value)
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        return [text]
    return [str(entry).strip() for entry in parsed if str(entry).strip()] if isinstance(parsed, list) else []


def parse_money(value: str, fallback_currency: Optional[str] = None) -> Optional[Tuple[float, Optional[str]]]:
    """"US $2.50" -> (2.5, "USD"); "€ 1.234,56" -> (1234.56, "EUR"). Whichever
    of "," and "." comes last is the decimal mark, the other groups thousands.
    A symbol not listed takes the order's own currency."""
    match = _NUMBER_RE.search(value)
    if not match:
        return None
    digits = re.sub(r"\s", "", match.group(0))
    if digits.rfind(",") > digits.rfind("."):
        digits = digits.replace(".", "").replace(",", ".")
    else:
        digits = digits.replace(",", "")
    try:
        amount = float(digits)
    except ValueError:
        return None
    currency = next((code for symbol, code in _MONEY_SYMBOLS if symbol in value), None)
    return amount, currency or fallback_currency


def _product_link(product_id: Optional[str]) -> Optional[str]:
    return _PRODUCT_URL.format(product_id) if product_id and product_id.isdigit() else None


def _workbooks(root: Path) -> List[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file() and _WORKBOOK_RE.match(path.name))


def _unique(rows: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    """Rows of one sheet minus exact repeats -- every Issue Information event
    is written twice, and two workbooks of one batch overlap entirely."""
    seen, out = set(), []
    for row in rows:
        key = tuple(sorted(row.items()))
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


@register
class AliExpressImporter(BaseImporter):
    key = "aliexpress"
    label = "AliExpress"
    type_labels = {
        "aliexpress_order": "Article commandé",
        "aliexpress_unpaid_order": "Commande non payée",
        "aliexpress_refund": "Remboursement",
        "aliexpress_dispute": "Litige",
        "aliexpress_review": "Avis",
        "aliexpress_question": "Question sur un produit",
        "saved_address": "Adresse",
    }

    def detect(self, root: Path) -> bool:
        return bool(_workbooks(root))

    def parse(self, root: Path) -> Iterator[NormalizedItem]:
        sheets = self._load(root)
        orders = _unique(sheets.get(_sheet_key(ORDER_SHEET), []))
        by_line = {_text(row.get("order_id")): row for row in orders if _text(row.get("order_id"))}
        disputes = self._disputes(_unique(sheets.get(_sheet_key(ISSUE_SHEET), [])))

        yield from self._parse_orders(orders)
        refunded_lines = set()
        for events in disputes.values():
            try:
                order = by_line.get(_text(events[0].get("order_id"))) or {}
                yield self._dispute_item(events, order)
                refund = self._dispute_refund(events, order)
                if refund:
                    refunded_lines.add(_text(events[0].get("order_id")))
                    yield refund
            except Exception:
                continue
        yield from self._closing_refunds(orders, refunded_lines)
        yield from self._parse_reviews(_unique(sheets.get(_sheet_key(REVIEW_SHEET), [])), by_line)
        yield from self._parse_questions(_unique(sheets.get(_sheet_key(QUESTION_SHEET), [])))
        yield from self._parse_addresses(_unique(sheets.get(_sheet_key(ADDRESS_SHEET), [])))
        yield from self._parse_profile(root, sheets)

    def _load(self, root: Path) -> Dict[str, List[Dict[str, str]]]:
        """{normalized sheet name: rows}, every workbook of the batch merged."""
        merged: Dict[str, List[Dict[str, str]]] = {}
        for path in _workbooks(root):
            try:
                workbook = xlsx.read_sheets(path)
            except xlsx.WorkbookError:
                continue
            for name, rows in workbook.items():
                merged.setdefault(_sheet_key(name), []).extend(xlsx.records(rows))
        return merged

    # -- Orders ----------------------------------------------------------------

    def _parse_orders(self, orders: List[Dict[str, str]]) -> Iterator[NormalizedItem]:
        for row in orders:
            try:
                item = self._order_item(row)
            except Exception:
                continue
            if item:
                yield item

    @staticmethod
    def _state(row: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
        """(Statut, Clôture) of one line."""
        status = _text(row.get("order_status"))
        reason = _text(row.get("end_reason"))
        if status and status.upper() != _FINISHED:
            return _RUNNING_STATES.get(status.upper(), status), None
        if reason:
            return _END_REASONS.get(reason, ("Terminée", reason))
        return ("Terminée" if status else None), None

    @staticmethod
    def _shipping_address(row: Dict[str, str]) -> Optional[str]:
        street = ", ".join(part for part in (_text(row.get("receiveaddress_detailaddress")),
                                             _text(row.get("receiveaddressaddress2"))) if part)
        locality = " ".join(part for part in (_text(row.get("receiveaddress_postcode")),
                                              _text(row.get("receiveaddress_city"))) if part)
        country = _text(row.get("receiveaddress_countryname")) or _text(row.get("receiveaddress_countrycode"))
        return ", ".join(part for part in (street, locality, country) if part) or None

    def _order_item(self, row: Dict[str, str]) -> Optional[NormalizedItem]:
        line = _text(row.get("order_id"))
        name = _text(row.get("item_name"))
        if not line and not name:
            return None
        currency = _text(row.get("payable_amt_currency_code")) or _text(row.get("unit_price_currency_code"))
        paid = (_text(row.get("is_success_pay")) or "").lower() == "true"
        status, closing = self._state(row)

        fields: Dict[str, Any] = {}
        payable = _minor_units(row.get("payable_amt"))
        if paid and payable is not None:
            fields["Montant"] = _money(payable, currency)
        elif payable is not None:
            fields["Montant dû"] = _money(payable, currency)
        for label, value in (
            ("Prix unitaire", _money(_minor_units(row.get("unit_price")),
                                     _text(row.get("unit_price_currency_code")) or currency)),
            ("Statut", status),
            ("Clôture", closing),
            ("N° de commande", line),
            ("Payée le", _date_fr(row.get("gmt_pay_order_time")) if paid else None),
            ("Clôturée le", _date_fr(row.get("gmt_trade_end_time"))),
            ("Destinataire", _text(row.get("receiver_contractname"))),
            ("Adresse de livraison", self._shipping_address(row)),
        ):
            if value:
                fields[label] = value

        return NormalizedItem(
            item_type="aliexpress_order" if paid else "aliexpress_unpaid_order",
            category="Commandes" if paid else "Commandes non payées",
            timestamp=_parse_date(row.get("gmt_create_order_time")) or _parse_date(row.get("gmt_pay_order_time")),
            # The article's name in `content`, where the purchases page reads a
            # line's product from (stats.build_purchase_list), as Amazon's.
            content=name,
            # The checkout: every line paid together shares it.
            context=_text(row.get("parent_orderid")) or line,
            fields=fields or None,
            raw=row,
        )

    # -- Disputes and refunds --------------------------------------------------

    @staticmethod
    def _disputes(events: List[Dict[str, str]]) -> Dict[str, List[Dict[str, str]]]:
        """{reverseOrderLineId: its events, oldest first}."""
        disputes: Dict[str, List[Dict[str, str]]] = {}
        for row in events:
            key = _text(row.get("reverseOrderLineId")) or _text(row.get("order_id"))
            if key:
                disputes.setdefault(key, []).append(row)
        for rows in disputes.values():
            rows.sort(key=lambda row: _text(row.get("actionTime")) or "")
        return disputes

    @staticmethod
    def _amounts(events: List[Dict[str, str]], action: str, currency: Optional[str]) -> List[Tuple[float, Optional[str]]]:
        found = []
        for row in events:
            if (_text(row.get("action")) or "").lower() != action:
                continue
            for entry in _json_list(row.get("formatMoney")):
                parsed = parse_money(entry, currency)
                if parsed:
                    found.append(parsed)
        return found

    def _dispute_item(self, events: List[Dict[str, str]], order: Dict[str, str]) -> NormalizedItem:
        first, last = events[0], events[-1]
        currency = _text(order.get("payable_amt_currency_code"))
        line = _text(first.get("order_id"))
        fields: Dict[str, Any] = {}
        solutions = [s for row in events for s in _json_list(row.get("solutionType"))]
        requested = self._amounts(events, _REQUEST_RECEIVED, currency)
        refunded = self._amounts(events, _REFUND_APPROVED, currency)
        memos = [_text(row.get("actionMemo")) for row in events if _text(row.get("actionMemo"))]
        attachments = sorted({url for row in events for url in _json_list(row.get("actionFileAttachments"))})
        for label, value in (
            ("Motif", _text(first.get("issue_reason"))),
            ("Solution demandée", _SOLUTIONS.get(solutions[0], solutions[0]) if solutions else None),
            ("Montant demandé", _money(*requested[0]) if requested else None),
            ("Montant remboursé", _money(*refunded[-1]) if refunded else None),
            ("Dernière étape", _text(last.get("action"))),
            ("Dernière étape le", _date_fr(last.get("actionTime"))),
            ("N° de commande", line),
            ("Messages", "\n\n".join(dict.fromkeys(memos)) if memos else None),
            ("Pièces jointes", "\n".join(attachments) if attachments else None),
            ("Historique", "\n".join(
                f"{_text(row.get('actionTime')) or '?'} · {_text(row.get('operatorType')) or '?'} · "
                f"{_text(row.get('action')) or '?'}" for row in events
            )),
        ):
            if value:
                fields[label] = value
        return NormalizedItem(
            item_type="aliexpress_dispute",
            category="Litiges",
            timestamp=_parse_date(first.get("actionTime")),
            title=_text(first.get("issue_reason")),
            content=_text(order.get("item_name")),
            context=_text(order.get("parent_orderid")) or line,
            fields=fields or None,
            raw={"events": events, "order": order or None},
        )

    def _dispute_refund(self, events: List[Dict[str, str]], order: Dict[str, str]) -> Optional[NormalizedItem]:
        approvals = [row for row in events if (_text(row.get("action")) or "").lower() == _REFUND_APPROVED]
        if not approvals:
            return None
        currency = _text(order.get("payable_amt_currency_code"))
        refunded = self._amounts(approvals, _REFUND_APPROVED, currency)
        fields: Dict[str, Any] = {}
        if refunded:
            fields["Montant remboursé"] = _money(*refunded[-1])
        else:
            requested = self._amounts(events, _REQUEST_RECEIVED, currency)
            if requested:
                fields["Montant remboursé"] = _money(*requested[0])
                fields["Origine du montant"] = "Montant d'après la demande"
        solutions = [s for row in approvals for s in _json_list(row.get("solutionType"))]
        line = _text(events[0].get("order_id"))
        for label, value in (
            ("Motif", _text(events[0].get("issue_reason"))),
            ("Solution", _SOLUTIONS.get(solutions[-1], solutions[-1]) if solutions else None),
            ("Origine", "Litige"),
            ("N° de commande", line),
        ):
            if value:
                fields[label] = value
        return NormalizedItem(
            item_type="aliexpress_refund",
            category="Remboursements",
            timestamp=_parse_date(approvals[-1].get("actionTime")),
            content=_text(order.get("item_name")),
            context=_text(order.get("parent_orderid")) or line,
            fields=fields,
            raw={"events": approvals, "order": order or None},
        )

    def _closing_refunds(self, orders: List[Dict[str, str]], refunded_lines: set) -> Iterator[NormalizedItem]:
        for row in orders:
            try:
                line = _text(row.get("order_id"))
                reason = _text(row.get("end_reason"))
                paid = (_text(row.get("is_success_pay")) or "").lower() == "true"
                amount = _minor_units(row.get("payable_amt"))
                if not paid or reason not in _REFUNDED_CLOSINGS or not amount or line in refunded_lines:
                    continue
                currency = _text(row.get("payable_amt_currency_code"))
                yield NormalizedItem(
                    item_type="aliexpress_refund",
                    category="Remboursements",
                    timestamp=_parse_date(row.get("gmt_trade_end_time")) or _parse_date(row.get("gmt_create_order_time")),
                    content=_text(row.get("item_name")),
                    context=_text(row.get("parent_orderid")) or line,
                    fields={
                        "Montant remboursé": _money(amount, currency),
                        "Origine": _END_REASONS.get(reason, ("", reason))[1],
                        **({"N° de commande": line} if line else {}),
                    },
                    raw=row,
                )
            except Exception:
                continue

    # -- Reviews, questions, addresses, the account ----------------------------

    def _parse_reviews(self, rows: List[Dict[str, str]], by_line: Dict[str, Dict[str, str]]) -> Iterator[NormalizedItem]:
        for row in rows:
            try:
                line = _text(row.get("orderId"))
                order = by_line.get(line) or {}
                fields: Dict[str, Any] = {}
                for label, key in (("Note : conforme à la description", "itemAsDescribed"),
                                   ("Note : communication", "customerService"),
                                   ("Note : livraison", "shippingService")):
                    value = _text(row.get(key))
                    if value:
                        fields[label] = f"{value}/5"
                for label, value in (("N° de commande", line),
                                     ("Lien", _product_link(_text(row.get("productId"))))):
                    if value:
                        fields[label] = value
                yield NormalizedItem(
                    item_type="aliexpress_review",
                    category="Avis",
                    timestamp=_parse_date(row.get("buyerEvaluationDate")),
                    title=_text(order.get("item_name")),
                    content=_text(row.get("buyerEvaluationText")),
                    context=_text(order.get("parent_orderid")) or line,
                    fields=fields or None,
                    raw=row,
                )
            except Exception:
                continue

    def _parse_questions(self, rows: List[Dict[str, str]]) -> Iterator[NormalizedItem]:
        for row in rows:
            try:
                content = _text(row.get("content"))
                if not content:
                    continue
                product = _text(row.get("productId"))
                fields: Dict[str, Any] = {}
                kind = _text(row.get("interlouctionType")) or _text(row.get("interlocutionType"))  # sic
                if kind:
                    fields["Type"] = {"QUESTION": "Question", "ANSWER": "Réponse"}.get(kind.upper(), kind)
                link = _product_link(product)
                if link:
                    fields["Lien"] = link
                yield NormalizedItem(
                    item_type="aliexpress_question",
                    category="Questions",
                    timestamp=_parse_date(row.get("createTime")),
                    content=content,
                    context=product,
                    fields=fields or None,
                    raw=row,
                )
            except Exception:
                continue

    def _parse_addresses(self, rows: List[Dict[str, str]]) -> Iterator[NormalizedItem]:
        """The address book, in the shape of Amazon's saved addresses (the same
        item type): an order carries its own delivery address besides."""
        for row in rows:
            try:
                street = ", ".join(part for part in (_text(row.get("address")), _text(row.get("address2"))) if part)
                postal = _text(row.get("zip"))
                if not street and not postal:
                    continue
                fields: Dict[str, Any] = {}
                locality = " ".join(part for part in (postal, _text(row.get("city"))) if part)
                for label, value in (
                    ("Ville", locality),
                    ("Région", _text(row.get("province"))),
                    ("Pays", _text(row.get("countryName")) or _text(row.get("country"))),
                    ("Type", _text(row.get("buyerAddressType"))),
                    ("Modifiée le", _date_fr(row.get("gmtModified"))),
                ):
                    if value:
                        fields[label] = value
                yield NormalizedItem(
                    item_type="saved_address",
                    category="Adresses",
                    timestamp=_parse_date(row.get("gmtCreate")),
                    content=_text(row.get("contactPerson")) or street,
                    context=street or None,
                    fields=fields or None,
                    raw=row,
                )
            except Exception:
                continue

    def _parse_profile(self, root: Path, sheets: Dict[str, List[Dict[str, str]]]) -> Iterator[NormalizedItem]:
        member = next(iter(sheets.get(_sheet_key(MEMBER_SHEET), [])), None)
        if not member:
            return
        ugc = next(iter(sheets.get(_sheet_key(UGC_SHEET), [])), {})
        try:
            fields: Dict[str, Any] = {}
            for label, value in (
                ("Identifiant", _text(member.get("accountId"))),
                ("N° de membre", _text(member.get("memberId"))),
                ("E-mail", _text(member.get("email"))),
                ("Téléphone", _text(member.get("loginMobile"))),
                ("Pays", _text(member.get("country"))),
                ("Pseudo", _text(ugc.get("nickName"))),
                ("Niveau", _text(member.get("newAeRewardLevel")) or _text(member.get("aeRewardLevel"))),
                ("Points", _text(member.get("totalScore"))),
                ("Niveau VIP", _text(member.get("vipLevel"))),
                ("Inscrit le", _date_fr(member.get("registerTime"))),
            ):
                if value:
                    fields[label] = value
            name = " ".join(dict.fromkeys(part for part in (_text(member.get("firstName")),
                                                            _text(member.get("lastName"))) if part))
            # The profile picture ships as a file of its own, next to the
            # workbook; the sheet only holds its public URL.
            media = [
                (path.relative_to(root).as_posix(), guess_media_type(path.name))
                for path in sorted(root.rglob("*"))
                if path.is_file() and path.parent.name.lower() == "member_attachments"
            ]
            yield NormalizedItem(
                item_type="profile",
                category="Compte",
                timestamp=_parse_date(member.get("registerTime")),
                title=_text(ugc.get("contactName")) or name or None,
                content="Compte AliExpress",
                context=_text(member.get("accountId")),
                fields=fields or None,
                media=media,
                raw={"member": member, "ugc": ugc or None},
            )
        except Exception:
            return


# -- How app/anonymize.py treats each column -----------------------------------
# The workbook is anonymized column by column, like a CSV, but with what each
# column IS spelled out here rather than guessed from its name: the generic
# CSV rule would turn `order_status` and `is_success_pay` into sentences (the
# anonymized copy would then import nothing), and would give the same order a
# different number in the order sheet and in the dispute sheet. Any column not
# listed is free text and replaced -- the default stays deny.

# Structural values: states, flags, currencies, reasons, ratings. Never personal.
_KEEP_COLUMNS = {
    "order_status", "frozen_status", "unit_price_currency_code", "payable_amt_currency_code",
    "is_success_pay", "is_send_goods", "is_buyer_accept", "end_reason", "receiveaddress_countrycode",
    "receiveaddress_countryname", "receiveaddress_languagecode", "issue_reason", "action", "operatortype",
    "solutiontype", "interlouctiontype", "interlocutiontype", "buyeraddresstype", "platform", "country",
    "countryname", "aerewardlevel", "newaerewardlevel", "viplevel", "itemasdescribed", "customerservice",
    "shippingservice",
}
# Identifiers, replaced by one consistent fake each, so a dispute still points
# at its order line and a line at its checkout.
_ID_COLUMNS = {
    "order_id", "parent_orderid", "buyer_admin_id", "reverseorderlineid", "buyerid", "evaluationid",
    "orderid", "productid", "interlocutionid", "accountid", "memberid", "ownerseq", "memberseq",
}
_DATE_COLUMNS = {
    "gmt_create_order_time", "gmt_pay_order_time", "gmt_trade_end_time", "actiontime", "buyerevaluationdate",
    "createtime", "registertime", "gmtcreate", "gmtmodified",
}
_AMOUNT_COLUMNS = {"unit_price", "payable_amt", "totalscore"}
# '["US $2.50"]': the number is replaced, the currency symbol kept.
_MONEY_COLUMNS = {"formatmoney"}


def anonymization_kind(column: str) -> Optional[str]:
    """"keep", "id", "date", "amount" or "money" for a column app/anonymize.py
    must not treat as free text; None for every other one."""
    name = column.strip().lower()
    for kind, columns in (("keep", _KEEP_COLUMNS), ("id", _ID_COLUMNS), ("date", _DATE_COLUMNS),
                          ("amount", _AMOUNT_COLUMNS), ("money", _MONEY_COLUMNS)):
        if name in columns:
            return kind
    return None


def is_workbook(path: Path) -> bool:
    return bool(_WORKBOOK_RE.match(path.name))
