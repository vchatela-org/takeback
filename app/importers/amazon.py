"""Amazon's "Request My Data" export (Account > Manage Your Data & Privacy
> Request My Data) has no single stable schema: the reference open source
tool used to originally ground column names here (github.com/holocronweaver/
amazon-orders-to-transactions) expects dot-namespaced, numbered files
("Retail.OrderHistory.1.csv"), but Amazon's own FileDescriptions.csv -- the
file it ships INSIDE the export itself, describing every file that can
appear in it -- names the same categories "Order History.csv" instead, no
dot-prefix or numbering. Both namings are matched where both are known,
since which one a given export uses seems to depend on account
region/export vintage, and guessing wrong means the category silently
returns nothing (the same lesson as Google Takeout's localized filenames).

Most of the categories below (and every exact column name) are grounded in
a real anonymized French export dropped in sample_data/ -- the anonymizer
preserves CSV headers and structural fields (dates, amounts) verbatim, only
scrubbing content, so real column names were read directly rather than
guessed. Two real bugs were caught this way: Order History's total column
is "Total Amount", not "Total Owed" (the reference tool describes a
different naming variant); its quantity column is "Original Quantity", not
"Quantity".

A full export also contains an "Additional Data" folder with 100+
dot-namespaced sub-folders -- confirmed on the same real export to be
overwhelmingly internal engagement/telemetry (UI interaction counters, sync
behavioral metrics, notification delivery logs) with no real "browse your
own history" value, not a uniform pile of untapped categories. The
exceptions worth their own category are covered below (Alexa voice
utterances, the Kindle files -- see the Kindle section); the rest -- notably
"SearchHistory.RufusConversations" (Amazon's AI shopping assistant,
substantial real content but a genuinely different chat-like format,
deserving its own dedicated design rather than a rushed pass) -- are a
documented gap, not an oversight.

Column values are always read defensively (every field via .get(), several
alternate names tried, never a required column), since a category
confirmed on ONE real export is not a guarantee every regional variant uses
the exact same headers.
"""
import csv
import fnmatch
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from app.importers.base import BaseImporter, NormalizedItem
from app.importers.registry import register
from app.importers.utils import iter_files, load_json


def _parse_amazon_date(value: Optional[str]) -> Optional[datetime]:
    """ISO 8601 in every file checked so far (confirmed against the
    reference tool's `pd.to_datetime(..., format="ISO8601", utc=True)` and
    against real Search Queries/Fire TV/Audible/Alexa columns), but
    tolerate a trailing "Z", a plain date with no time component, or (only
    Kindle's start_timestamp/end_timestamp, confirmed on a real export) a
    raw epoch value -- scaled by magnitude rather than assumed to be a
    fixed unit, since Amazon's own files aren't consistent about it."""
    if not value or not value.strip():
        return None
    value = value.strip()
    if value.lstrip("-").isdigit():
        return _parse_epoch_any_unit(int(value))
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _parse_epoch_any_unit(value: int) -> Optional[datetime]:
    """Same magnitude-scaling heuristic as
    google_takeout._parse_any_timestamp (seconds/milliseconds/
    microseconds), duplicated locally rather than imported since the two
    importers have no other shared dependency."""
    if value > 10**14:
        seconds = value / 1_000_000
    elif value > 10**11:
        seconds = value / 1000
    else:
        seconds = value
    try:
        return datetime.fromtimestamp(seconds)
    except (ValueError, OSError, OverflowError):
        return None


def _parse_amount(value: Optional[str]) -> Optional[float]:
    """Amazon uses "Not Available" as a placeholder in numeric columns
    (confirmed by the reference tool coercing it to NaN) -- anything that
    doesn't parse as a plain number is treated as absent, never as 0."""
    if not value or not value.strip():
        return None
    try:
        return float(value.strip().replace(",", ""))
    except ValueError:
        return None


def _first_present(row: Dict[str, str], *keys: str) -> Optional[str]:
    for key in keys:
        value = row.get(key)
        if value and value.strip() and value.strip().lower() != "not available":
            return value.strip()
    return None


def _read_csv_rows(path: Path) -> Iterator[Dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        yield from csv.DictReader(f)


# Amazon delivers one zip per category requested ("Your Orders.zip",
# "Kindle.zip"...), so an export can hold the Kindle files alone, without
# any order history. Matched on file names, in one walk of the tree.
_DETECT_PATTERNS = (
    "Retail.OrderHistory*", "Retail.OrdersReturned*", "Order History*.csv", "Refund Details*.csv",
    "Kindle.Devices.ReadingSession*.csv", "Kindle.ReadingActivity*.csv", "Kindle.KindleDocs.*.csv",
    "Digital.Content.Ownership*.json", "whispersync*.csv",
)


@register
class AmazonImporter(BaseImporter):
    key = "amazon"
    label = "Amazon"
    type_labels = {
        "reading_session": "Session de lecture",
        "kindle_finished": "Livre terminé",
        "kindle_highlight": "Surlignage",
        "kindle_note": "Note",
        "kindle_bookmark": "Signet",
        "kindle_book": "Livre Kindle",
        "kindle_document": "Document personnel",
        "kindle_badge": "Badge de lecture",
    }

    # Populated by parse() before returns are normalized; kept as a class-level
    # default so a normalizer called directly (e.g. in a test) doesn't crash.
    _order_dates: Dict[str, datetime] = {}
    _kindle_titles: Dict[str, str] = {}
    _kindle_authors: Dict[str, str] = {}
    _kindle_families: Dict[str, str] = {}

    def detect(self, root: Path) -> bool:
        return any(
            any(fnmatch.fnmatchcase(path.name, pattern) for pattern in _DETECT_PATTERNS)
            for path in root.rglob("*")
        )

    def parse(self, root: Path) -> Iterator[NormalizedItem]:
        # Refund/return files don't carry the original purchase date -- only
        # Order History does. Built once up front so _normalize_return(_status)
        # can look it up by Order ID without a second pass over the exports.
        self._order_dates = self._build_order_date_map(root)
        # Same for Kindle: sessions and highlights only name the book by id.
        self._kindle_titles, self._kindle_authors = self._build_kindle_titles(root)
        self._kindle_families = self._build_kindle_families(root)
        yield from self._parse_order_history(root)
        yield from self._parse_returns(root)
        yield from self._parse_digital_content_orders(root)
        yield from self._parse_cart_history(root)
        yield from self._parse_returns_status(root)
        yield from self._parse_search_queries(root)
        yield from self._parse_device_usage(root, "Your Fire TV Apps & Usage/Total Usage*.csv", "Fire TV")
        yield from self._parse_device_usage(root, "Your Fire Tablet Apps & Usage/Total Usage.csv", "Fire Tablet")
        yield from self._parse_app_engagement(root)
        yield from self._parse_audible_listening(root)
        yield from self._parse_audible_purchases(root)
        yield from self._parse_alexa_utterances(root, "questionUnderstandings*.csv", "voice_command", "Alexa")
        yield from self._parse_alexa_utterances(root, "questionAnswering*.csv", "voice_question", "Alexa")
        yield from self._parse_kindle_reading_session(root)
        yield from self._parse_kindle_finished(root)
        yield from self._parse_kindle_annotations(root)
        yield from self._parse_kindle_library(root)
        yield from self._parse_kindle_documents(root)
        yield from self._parse_kindle_badges(root)

    # -- Shared CSV-category runner ------------------------------------------
    # Every category below is "glob a filename pattern, read each CSV row,
    # normalize it into a NormalizedItem, skip anything that doesn't parse
    # cleanly" -- factored out once both to cut the repetition and because
    # a bug in the shared shell (e.g. try/except placement) used to need
    # fixing in N places instead of one.

    def _parse_csv_category(
        self, root: Path, pattern: str, normalize: Callable[[Dict[str, str]], Optional[NormalizedItem]],
    ) -> Iterator[NormalizedItem]:
        seen_paths = set()
        seen_rows = set()
        for path in iter_files(root, pattern):
            if path in seen_paths:
                continue
            seen_paths.add(path)
            try:
                rows = list(_read_csv_rows(path))
            except Exception:
                continue
            for row in rows:
                # Amazon's multi-part "Request My Data" download has been
                # observed shipping the same small category file at more
                # than one path across parts (each part gets its own
                # top-level wrapper folder, and some files aren't actually
                # split, just repeated) -- a byte-for-byte identical row
                # is essentially certain to be that, not two genuinely
                # separate real-world events, so it's skipped rather than
                # double-counted in spend/session totals. A key over ALL
                # columns (not just Order ID) since a real order can
                # legitimately span several distinct item rows that must
                # NOT be collapsed into one.
                row_key = tuple(sorted(row.items()))
                if row_key in seen_rows:
                    continue
                seen_rows.add(row_key)
                try:
                    item = normalize(row)
                except Exception:
                    continue
                if item is not None:
                    yield item

    # -- Order history: one row per item within an order --------------------
    # Either "Retail.OrderHistory.<n>/Retail.OrderHistory.<n>.csv" (dot-
    # namespaced naming) or "Order History.csv" (confirmed via Amazon's own
    # FileDescriptions.csv AND a real export, no dot-prefix/numbering) --
    # possibly split across several numbered files for a long history
    # either way. Confirmed real header for "Order History.csv": ASIN,
    # Billing Address, Carrier Name & Tracking Number, Currency, Department,
    # Gift Message/Recipient/Sender, Item Serial Number, Order Date, Order
    # ID, Order Status, Original Quantity, Payment Method Type, Product
    # Condition, Product Name, Ship Date, Shipment Item Subtotal(+Tax),
    # Shipping Address/Charge/Option, Total Amount, Total Discounts, Unit
    # Price(+Tax), Website.

    def _parse_order_history(self, root: Path) -> Iterator[NormalizedItem]:
        yield from self._parse_csv_category(root, "Retail.OrderHistory*.csv", self._normalize_order_item)
        yield from self._parse_csv_category(root, "Order History*.csv", self._normalize_order_item)

    def _build_order_date_map(self, root: Path) -> Dict[str, datetime]:
        dates: Dict[str, datetime] = {}
        for pattern in ("Retail.OrderHistory*.csv", "Order History*.csv"):
            for path in iter_files(root, pattern):
                try:
                    rows = list(_read_csv_rows(path))
                except Exception:
                    continue
                for row in rows:
                    order_id = _first_present(row, "Order ID", "Website Order ID")
                    if not order_id:
                        continue
                    order_date = _parse_amazon_date(_first_present(row, "Order Date"))
                    if not order_date:
                        continue
                    if order_id not in dates or order_date < dates[order_id]:
                        dates[order_id] = order_date
        return dates

    def _normalize_order_item(self, row: Dict[str, str]) -> Optional[NormalizedItem]:
        order_id = _first_present(row, "Order ID", "Website Order ID")
        product_name = _first_present(row, "Product Name")
        if not order_id and not product_name:
            return None  # not a recognizable order-item row

        order_date = _parse_amazon_date(_first_present(row, "Order Date"))
        ship_date = _parse_amazon_date(_first_present(row, "Ship Date"))
        currency = _first_present(row, "Currency") or ""
        total = _parse_amount(
            _first_present(row, "Total Amount", "Total Owed", "Total", "Shipment Item Subtotal")
        )
        quantity = _first_present(row, "Original Quantity", "Quantity", "Quantity Ordered")
        status = _first_present(row, "Order Status", "Shipment Status")
        asin = _first_present(row, "ASIN")
        department = _first_present(row, "Department")
        shipping_address = _first_present(row, "Shipping Address")
        billing_address = _first_present(row, "Billing Address")
        payment_method = _first_present(row, "Payment Method Type")
        website = _first_present(row, "Website")
        condition = _first_present(row, "Product Condition")

        fields: Dict[str, Any] = {}
        if total is not None:
            fields["Montant"] = f"{total:.2f} {currency}".strip()
        if quantity:
            fields["Quantité"] = quantity
        if status:
            fields["Statut"] = status
        if department:
            fields["Département"] = department
        if ship_date:
            fields["Expédié le"] = ship_date.strftime("%d/%m/%Y")
        if shipping_address:
            fields["Adresse de livraison"] = shipping_address
        if billing_address:
            fields["Adresse de facturation"] = billing_address
        if payment_method:
            fields["Mode de paiement"] = payment_method
        if website:
            fields["Site"] = website
        if condition:
            fields["État du produit"] = condition
        if asin:
            fields["ASIN"] = asin
            fields["Lien"] = f"https://www.amazon.fr/dp/{asin}"

        return NormalizedItem(
            item_type="order_item",
            category="Commandes",
            timestamp=order_date or ship_date,
            title=None,
            content=product_name,
            author=None,
            context=order_id,  # groups every item of the same order together
            fields=fields or None,
            media=[],
            raw=row,
        )

    # -- Returns/refunds ------------------------------------------------------
    # Either "Retail.OrdersReturned.Payments.<n>/....csv" -> columns
    # OrderID, RefundCompletionDate, AmountRefunded, or "Refund Details.csv"
    # -- confirmed real header: "Creation Date", Currency, Direct Debit
    # Refund Amount, Disbursement Type, Order ID, Payment Status, Quantity,
    # Refund Amount, Refund Date, Reversal Amount State/Reason/Status,
    # Website.

    def _parse_returns(self, root: Path) -> Iterator[NormalizedItem]:
        yield from self._parse_csv_category(
            root, "Retail.OrdersReturned.Payments*.csv", self._normalize_return
        )
        yield from self._parse_csv_category(root, "Refund Details*.csv", self._normalize_return)

    def _normalize_return(self, row: Dict[str, str]) -> Optional[NormalizedItem]:
        order_id = _first_present(row, "OrderID", "Order ID")
        refund_date = _parse_amazon_date(
            _first_present(row, "RefundCompletionDate", "Refund Completion Date", "Refund Date")
        )
        amount = _parse_amount(_first_present(row, "AmountRefunded", "Amount Refunded", "Refund Amount"))
        if not order_id and amount is None:
            return None

        fields: Dict[str, Any] = {}
        if amount is not None:
            fields["Montant remboursé"] = f"{amount:.2f}"
        purchase_date = self._order_dates.get(order_id) if order_id else None
        if purchase_date:
            fields["Date d'achat"] = purchase_date.strftime("%d/%m/%Y")
        if refund_date:
            fields["Date de remboursement"] = refund_date.strftime("%d/%m/%Y")

        return NormalizedItem(
            item_type="return",
            category="Retours",
            timestamp=refund_date,
            title=None,
            content=f"Remboursement commande {order_id}" if order_id else "Remboursement",
            author=None,
            context=order_id,
            fields=fields or None,
            media=[],
            raw=row,
        )

    # -- Return status (richer than Refund Details.csv: carries a reason) ---
    # "Returns Status.csv" -- confirmed real header: "Carrier Package ID",
    # Carrier Tracking ID, Charge Status, Contract Creation Date/ID/Unit ID,
    # Date of Return, Gift Card, Order ID, Payment Method Service Type,
    # Replacement Order, Retrocharge Resolution, Return Amount(+Currency),
    # Return Authorization ID, Return Creation Date, Return Reason, Return
    # Receivable Creation Reason/Initiation Date/State/Tracking ID, Return
    # Resolution, Return Ship Option. Kept as a separate item_type from
    # "return" (Refund Details-based) since the two files aren't confirmed
    # to cover the same real-world return 1:1 -- merging risks double-
    # counting until that's verified against more real data.

    def _parse_returns_status(self, root: Path) -> Iterator[NormalizedItem]:
        yield from self._parse_csv_category(root, "Returns Status*.csv", self._normalize_return_status)

    def _normalize_return_status(self, row: Dict[str, str]) -> Optional[NormalizedItem]:
        order_id = _first_present(row, "Order ID")
        return_date = _parse_amazon_date(_first_present(row, "Date of Return", "Return Creation Date"))
        amount = _parse_amount(_first_present(row, "Return Amount"))
        reason = _first_present(row, "Return Reason")
        resolution = _first_present(row, "Return Resolution")
        currency = _first_present(row, "Return Amount Currency") or ""
        if not order_id and amount is None and not reason:
            return None

        fields: Dict[str, Any] = {}
        if amount is not None:
            fields["Montant"] = f"{amount:.2f} {currency}".strip()
        if resolution:
            fields["Résolution"] = resolution
        purchase_date = self._order_dates.get(order_id) if order_id else None
        if purchase_date:
            fields["Date d'achat"] = purchase_date.strftime("%d/%m/%Y")
        if return_date:
            fields["Date de remboursement"] = return_date.strftime("%d/%m/%Y")

        return NormalizedItem(
            item_type="return_status",
            category="Retours",
            timestamp=return_date,
            title=None,
            content=f"Retour {order_id}: {reason}" if (order_id and reason) else (reason or f"Retour {order_id}"),
            author=None,
            context=order_id,
            fields=fields or None,
            media=[],
            raw=row,
        )

    # -- Digital content orders (ebooks, Audible, digital purchases...) -----
    # "Digital Content Orders.csv" -- confirmed real header (72 columns;
    # only a handful used here). Same shape as retail Order History, kept
    # as a separate category/item_type since a digital purchase has no
    # shipping/carrier data and its price/quantity columns are named
    # differently ("Price"/"Quantity Ordered" here vs. "Unit Price"/
    # "Original Quantity" in the retail file) -- confirmed real headers,
    # not guessed from the retail file's naming.

    def _parse_digital_content_orders(self, root: Path) -> Iterator[NormalizedItem]:
        yield from self._parse_csv_category(
            root, "Digital Content Orders*.csv", self._normalize_digital_order
        )

    def _normalize_digital_order(self, row: Dict[str, str]) -> Optional[NormalizedItem]:
        order_id = _first_present(row, "Order ID")
        product_name = _first_present(row, "Product Name")
        if not order_id and not product_name:
            return None

        order_date = _parse_amazon_date(_first_present(row, "Order Date", "Fulfilled Date", "Delivery Date"))
        currency = _first_present(row, "Price Currency Code", "Base Currency Code") or ""
        price = _parse_amount(_first_present(row, "Price", "Transaction Amount"))
        quantity = _first_present(row, "Quantity Ordered", "Original Quantity")
        status = _first_present(row, "Order Status", "Fullfilment Status")  # sic, Amazon's own typo
        publisher = _first_present(row, "Publisher")
        asin = _first_present(row, "ASIN")

        fields: Dict[str, Any] = {}
        if price is not None:
            fields["Montant"] = f"{price:.2f} {currency}".strip()
        if quantity:
            fields["Quantité"] = quantity
        if status:
            fields["Statut"] = status
        if publisher:
            fields["Éditeur"] = publisher
        if asin:
            fields["ASIN"] = asin
            fields["Lien"] = f"https://www.amazon.fr/dp/{asin}"

        return NormalizedItem(
            item_type="digital_order",
            category="Commandes numériques",
            timestamp=order_date,
            title=None,
            content=product_name,
            author=None,
            context=order_id,
            fields=fields or None,
            media=[],
            raw=row,
        )

    # -- Cart history (added/removed, not necessarily purchased) ------------
    # "Cart History.csv" -- confirmed real header: ASIN, Add-on Item, Cart
    # Domain, Cart List, Cart Source, Date Added to Cart, Gift Wrapped,
    # One-Click Enabled, Order Quantity, Pantry Item, Prime Subscription,
    # Product Name. Two DIFFERENT "Cart History.csv" exist in a full export
    # (retail and Audible) -- both share this shape closely enough to reuse
    # the same normalizer; iter_files() finds both regardless of which
    # top-level folder they're under.

    def _parse_cart_history(self, root: Path) -> Iterator[NormalizedItem]:
        yield from self._parse_csv_category(root, "Cart History*.csv", self._normalize_cart_entry)

    def _normalize_cart_entry(self, row: Dict[str, str]) -> Optional[NormalizedItem]:
        product_name = _first_present(row, "Product Name")
        if not product_name:
            return None
        added_date = _parse_amazon_date(_first_present(row, "Date Added to Cart"))

        quantity = _first_present(row, "Order Quantity")
        asin = _first_present(row, "ASIN")

        fields: Dict[str, Any] = {}
        if quantity:
            fields["Quantité"] = quantity
        if asin:
            fields["ASIN"] = asin
            fields["Lien"] = f"https://www.amazon.fr/dp/{asin}"

        return NormalizedItem(
            item_type="cart_item",
            category="Panier",
            timestamp=added_date,
            title=None,
            content=product_name,
            author=None,
            context=None,
            fields=fields or None,
            media=[],
            raw=row,
        )

    # -- Shopping search history ----------------------------------------------
    # "Your Shopping Search/Search Queries.csv" -- confirmed real header
    # (60 columns; only a handful used here). "First Search Query String"
    # is NOT the typed search text despite the name -- on real exports it's
    # an internal click-tracking parameter blob (e.g. "at&v=0.36...&id=...");
    # the actual human-readable text is in "Keywords" (e.g. "secteur 12v 3a
    # blanc"), confirmed against the user's own real anonymized export.
    # Columns used: Keywords, First Search Time (GMT), Department, Query
    # Abandoned (Y/N), Purchased Any Item (Y/N), Number of Clicked Items,
    # Number of Items Added to Cart, Number of Items Ordered, Maximum
    # Purchase Price, Application / Browser Name.
    # 1533 real rows on the sample export -- one of the richest single
    # files for time-activity stats (real per-search timestamps).

    def _parse_search_queries(self, root: Path) -> Iterator[NormalizedItem]:
        yield from self._parse_csv_category(root, "Search Queries*.csv", self._normalize_search_query)

    def _normalize_search_query(self, row: Dict[str, str]) -> Optional[NormalizedItem]:
        query = _first_present(row, "Keywords", "First Search Query String")
        if not query:
            return None
        search_date = _parse_amazon_date(_first_present(row, "First Search Time (GMT)", "Last search Time (GMT)"))
        department = _first_present(row, "Department", "All Department (APS) or Category")
        abandoned = _first_present(row, "Query Abandoned (Y/N)")
        purchased = _first_present(row, "Purchased Any Item (Y/N)")
        clicked = _first_present(row, "Number of Clicked Items")
        added_to_cart = _first_present(row, "Number of Items Added to Cart")
        items_ordered = _first_present(row, "Number of Items Ordered")
        max_price = _first_present(row, "Maximum Purchase Price")
        device = _first_present(row, "Application / Browser Name", "Device Category")

        fields: Dict[str, Any] = {}
        # "aps" is Amazon's internal code for "no specific department" (All
        # Product Search), not a real category -- showing it as-is just
        # adds noise, so it's only kept when it names an actual department.
        if department and department.strip().lower() != "aps":
            fields["Département"] = department
        if clicked:
            fields["Résultats cliqués"] = clicked
        if added_to_cart and added_to_cart != "0":
            fields["Ajouté au panier"] = added_to_cart
        if purchased:
            fields["Achat suite à la recherche"] = purchased
        if items_ordered and items_ordered != "0":
            fields["Articles commandés"] = items_ordered
        if max_price:
            fields["Prix max consulté"] = max_price
        if abandoned:
            fields["Recherche abandonnée"] = abandoned
        if device and device != "Not Available":
            fields["Appareil"] = device

        return NormalizedItem(
            item_type="search",
            category="Recherches",
            timestamp=search_date,
            title=None,
            content=query,
            author=None,
            context=None,
            fields=fields or None,
            media=[],
            raw=row,
        )

    # -- Device session duration (Fire TV / Fire Tablet) ---------------------
    # "Total Usage.csv" -- confirmed real header: Amazon Marketplace,
    # Country of Residence, Device Record Time, Device Serial Number,
    # Device Type, Duration Ms, End Time, Hdmi Flicker Ms, OS Software
    # Version, Start Time, Trigger Type. Real, exact session start/end/
    # duration -- unlike Facebook/Instagram's stats.py, no estimation
    # needed here, Amazon already records the session boundaries directly.
    # Same file name/shape confirmed under both "Fire TV" and "Fire Tablet"
    # folders -- device_label distinguishes which, since the file itself
    # carries no explicit device-family column.

    def _parse_device_usage(self, root: Path, pattern: str, device_label: str) -> Iterator[NormalizedItem]:
        yield from self._parse_csv_category(
            root, pattern, lambda row: self._normalize_device_session(row, device_label)
        )

    def _normalize_device_session(self, row: Dict[str, str], device_label: str) -> Optional[NormalizedItem]:
        start = _parse_amazon_date(_first_present(row, "Start Time"))
        end = _parse_amazon_date(_first_present(row, "End Time"))
        duration_ms = _parse_amount(_first_present(row, "Duration Ms"))
        if start is None and duration_ms is None:
            return None

        fields: Dict[str, Any] = {}
        if duration_ms is not None:
            fields["Durée"] = f"{duration_ms / 60000:.1f} min"
        if end:
            fields["Fin"] = end.strftime("%d/%m/%Y %H:%M")
        trigger = _first_present(row, "Trigger Type")
        if trigger:
            fields["Déclencheur"] = trigger

        return NormalizedItem(
            item_type="device_session",
            category=device_label,
            timestamp=start,
            title=None,
            content=f"Session {device_label}",
            author=None,
            context=device_label,
            fields=fields or None,
            media=[],
            raw=row,
        )

    # -- Per-app engagement (Fire TV) -----------------------------------------
    # "Your Fire TV Apps & Usage/App Engagement.csv" -- confirmed real
    # header: Amazon Marketplace, App Package Name, App Profile Type, App
    # Version Code/Name, Country of Residence, Device Profile Type, Device
    # Record Time, Device Serial Number, Device Type, Duration
    # Milliseconds, Launch Count, Metric Level, Metric Source, Product
    # Mode, Reason Code, Software Version, Start Time. 3587 real rows --
    # per-launch app usage, more granular than the daily/session totals.

    def _parse_app_engagement(self, root: Path) -> Iterator[NormalizedItem]:
        yield from self._parse_csv_category(
            root, "Your Fire TV Apps & Usage/App Engagement*.csv", self._normalize_app_engagement
        )

    def _normalize_app_engagement(self, row: Dict[str, str]) -> Optional[NormalizedItem]:
        app_name = _first_present(row, "App Package Name")
        start = _parse_amazon_date(_first_present(row, "Start Time", "Device Record Time"))
        if not app_name:
            return None

        duration_ms = _parse_amount(_first_present(row, "Duration Milliseconds"))
        launches = _first_present(row, "Launch Count")

        fields: Dict[str, Any] = {}
        if duration_ms is not None:
            fields["Durée"] = f"{duration_ms / 60000:.1f} min"
        if launches:
            fields["Lancements"] = launches

        return NormalizedItem(
            item_type="app_engagement",
            category="Fire TV",
            timestamp=start,
            title=None,
            content=app_name,
            author=None,
            context=app_name,
            fields=fields or None,
            media=[],
            raw=row,
        )

    # -- Audible listening sessions -------------------------------------------
    # "Your Audible Library & Listening/Listening History.csv" -- confirmed
    # real header: ASIN, App Version, Asin Owned, Audio Type, Book Length
    # Milliseconds, Bookmark, Delivery Type, End Date, End Position
    # Milliseconds, Event Duration Milliseconds, Listening Mode, Local
    # Timezone, Narration Speed, Product Name, Start Date, Start Position
    # Milliseconds, Store. 2290 real rows.

    def _parse_audible_listening(self, root: Path) -> Iterator[NormalizedItem]:
        yield from self._parse_csv_category(
            root, "Your Audible Library & Listening/Listening History*.csv", self._normalize_audible_session
        )

    def _normalize_audible_session(self, row: Dict[str, str]) -> Optional[NormalizedItem]:
        product_name = _first_present(row, "Product Name")
        start = _parse_amazon_date(_first_present(row, "Start Date"))
        if not product_name:
            return None

        duration_ms = _parse_amount(_first_present(row, "Event Duration Milliseconds"))
        speed = _first_present(row, "Narration Speed")

        fields: Dict[str, Any] = {}
        if duration_ms is not None:
            fields["Durée d'écoute"] = f"{duration_ms / 60000:.1f} min"
        if speed:
            fields["Vitesse"] = speed
        asin = _first_present(row, "ASIN")
        if asin:
            fields["ASIN"] = asin
            fields["Lien"] = f"https://www.amazon.fr/dp/{asin}"

        return NormalizedItem(
            item_type="listening_session",
            category="Audible",
            timestamp=start,
            title=None,
            content=product_name,
            author=None,
            context=product_name,
            fields=fields or None,
            media=[],
            raw=row,
        )

    # -- Audible purchases -----------------------------------------------------
    # "Your Audible Account & Membership/Purchase History.csv" -- confirmed
    # real header: ASIN, Action, Consumed Credit, Currency, Discount, Net
    # Price, Net Sales, Order Fulfill/Place/Settle Date, Order ID, Partner
    # Revenue, Preorder Desc, Price Paid Member, Product Name, Regular
    # Price, Royalty Sale Type, Sale Type Desc, Status, Tax(+Rate), Type,
    # Units, Whispersync.

    def _parse_audible_purchases(self, root: Path) -> Iterator[NormalizedItem]:
        yield from self._parse_csv_category(
            root, "Your Audible Account & Membership/Purchase History*.csv", self._normalize_audible_purchase
        )

    def _normalize_audible_purchase(self, row: Dict[str, str]) -> Optional[NormalizedItem]:
        product_name = _first_present(row, "Product Name")
        order_id = _first_present(row, "Order ID")
        if not product_name and not order_id:
            return None

        order_date = _parse_amazon_date(_first_present(row, "Order Place Date", "Order Fulfill Date"))
        currency = _first_present(row, "Currency") or ""
        price = _parse_amount(_first_present(row, "Price Paid Member", "Net Price", "Regular Price"))
        status = _first_present(row, "Status")

        fields: Dict[str, Any] = {}
        if price is not None:
            fields["Montant"] = f"{price:.2f} {currency}".strip()
        if status:
            fields["Statut"] = status
        asin = _first_present(row, "ASIN")
        if asin:
            fields["ASIN"] = asin
            fields["Lien"] = f"https://www.amazon.fr/dp/{asin}"

        return NormalizedItem(
            item_type="audible_order",
            category="Commandes numériques",
            timestamp=order_date,
            title=None,
            content=product_name,
            author=None,
            context=order_id,
            fields=fields or None,
            media=[],
            raw=row,
        )

    # -- Alexa voice utterances (what was said + what Alexa understood) -----
    # "Additional Data/Alexa/Alexa/Understanding/questionUnderstandings*.csv"
    # and ".../Answers/questionAnswering*.csv" -- confirmed real headers,
    # both carrying "Utterance text" + "Utterance Creation Date". Two
    # separate pipeline stages (interpretation vs. answer), not confirmed
    # to be 1:1 with each other, so kept as distinct item_types
    # ("voice_command"/"voice_question") rather than merged.

    def _parse_alexa_utterances(
        self, root: Path, pattern: str, item_type: str, category: str,
    ) -> Iterator[NormalizedItem]:
        yield from self._parse_csv_category(
            root, pattern, lambda row: self._normalize_alexa_utterance(row, item_type, category)
        )

    def _normalize_alexa_utterance(
        self, row: Dict[str, str], item_type: str, category: str,
    ) -> Optional[NormalizedItem]:
        text = _first_present(row, "Utterance text")
        if not text:
            return None
        when = _parse_amazon_date(_first_present(row, "Utterance Creation Date"))
        device_type = _first_present(row, "Device type")

        fields: Dict[str, Any] = {}
        if device_type:
            fields["Appareil"] = device_type

        return NormalizedItem(
            item_type=item_type,
            category=category,
            timestamp=when,
            title=None,
            content=text,
            author=None,
            context=None,
            fields=fields or None,
            media=[],
            raw=row,
        )

    # -- Kindle -------------------------------------------------------------------
    # Grounded in a real French Kindle-only export ("Kindle.zip", 75 files:
    # the dot-namespaced folders a full export keeps under "Additional
    # Data"). Most of it is device telemetry (power states, toolbar taps,
    # wifi dialogs, home-screen impressions); read here is what a reader
    # looks for: sessions, finished books, highlights, the books owned, the
    # documents sent to the Kindle, reading badges. Kindle.ReadingInsights'
    # ReadingActivity (pages and minutes per day) and its
    # "sessions_with_adjustments" file add up to the very same reading time
    # as Kindle.Devices.ReadingSession (606.5 h each on the real export):
    # they only lend their titles, importing them as items would count
    # every minute twice. Ids are an ASIN for a book from the store, a
    # 32-character id for a personal document -- 2 728 of the 2 823
    # session rows of the real export.

    def _build_kindle_titles(self, root: Path) -> tuple:
        """(book id -> title, book id -> author), from every Kindle file
        naming a book. The first source to name one wins: a few files cut
        titles short ("La patience, une générosité intérieure" in
        ContentUpdates, with its subtitle everywhere else), and every item
        of one book must get the same title -- the dashboards group by it."""
        titles: Dict[str, str] = {}
        authors: Dict[str, List[str]] = {}

        def rows(pattern: str) -> Iterator[Dict[str, str]]:
            for path in iter_files(root, pattern):
                try:
                    yield from list(_read_csv_rows(path))
                except Exception:
                    continue

        def name(content_id: Optional[str], title: Optional[str]) -> None:
            if content_id and title:
                titles.setdefault(content_id, title)

        def credit(content_id: Optional[str], author: Optional[str]) -> None:
            if content_id and author and author not in authors.setdefault(content_id, []):
                authors[content_id].append(author)

        for row in rows("Kindle.KindleDocs.DocumentMetadata*.csv"):
            doc_id = _kindle_value(row, "DocumentId")
            name(doc_id, _kindle_value(row, "Title"))
            credit(doc_id, _author_name(_kindle_value(row, "DocumentProvider")))
        for row in rows("Kindle.reading-insights-sessions*.csv"):
            name(_kindle_value(row, "ASIN", "personal_document_id"), _kindle_value(row, "product_name"))
        for row in rows("Kindle.UserUniqueTitlesCompleted*.csv"):
            name(_finished_id(row), _kindle_value(row, "product_name"))
        for row in rows("whispersync*.csv"):
            name(_kindle_value(row, "ASIN", "Non ASIN"), _kindle_value(row, "Product Name"))
        for entry in _ownership_entries(root):
            resource = entry.get("resource") or {}
            name(_kindle_value(resource, "ASIN"), _kindle_value(resource, "Product Name"))
        for pattern in ("Kindle.UnifiedLibraryIndex.*.csv", "Kindle.KindleContentUpdate.*.csv",
                        "BookRelation*.csv", "Digital Content Orders*.csv"):
            for row in rows(pattern):
                name(_kindle_value(row, "ASIN"), _kindle_value(row, "Product Name"))
        for row in rows("Kindle.UnifiedLibraryIndex.CustomerAuthorNameRelationship*.csv"):
            credit(_kindle_value(row, "ASIN"), _author_name(_kindle_value(row, "Author Name")))
        return titles, {content_id: ", ".join(names) for content_id, names in authors.items()}

    def _build_kindle_families(self, root: Path) -> Dict[str, str]:
        """Device serial number -> device family ("Kindle E-reader"), from the
        reading sessions: the only file that names both. Annotations carry
        a serial alone."""
        families: Dict[str, str] = {}
        for path in iter_files(root, "Kindle.Devices.ReadingSession*.csv"):
            try:
                for row in _read_csv_rows(path):
                    serial, family = _kindle_value(row, "device_serial_number"), _kindle_value(row, "device_family")
                    if serial and family:
                        families.setdefault(serial, family)
            except Exception:
                continue
        return families

    def _kindle_device_fields(self, serial: Optional[str]) -> Dict[str, str]:
        """The device a Kindle item comes from: "Appareil" (its family and
        the end of its serial number, which tells two e-readers of the same
        family apart -- the label dashboards filter and colour by) and the
        full serial number."""
        if not serial:
            return {}
        family = self._kindle_families.get(serial) or "Appareil inconnu"
        return {"Appareil": f"{family} …{serial[-4:]}", "Numéro de série": serial}

    def _kindle_book(self, content_id: Optional[str], own_title: Optional[str] = None) -> tuple:
        """(title, author, context) of a Kindle item: the context every item
        of one book shares is its title, or its id when no file names it
        (a re-sent document gets a new id but keeps its title, and is the
        same book to its reader)."""
        title = self._kindle_titles.get(content_id) if content_id else None
        title = title or own_title
        author = self._kindle_authors.get(content_id) if content_id else None
        return title, author, title or content_id

    # "Kindle.Devices.ReadingSession.csv" -- confirmed real header
    # (snake_case, unlike every other Amazon file): start_timestamp,
    # end_timestamp, ASIN, purchased_marketplace, preferred_marketplace,
    # device_family, device_serial_number, device_software_version,
    # content_type, total_reading_millis, number_of_page_flips. Real
    # per-session reading duration. 1 133 of the 2 823 real rows have no
    # start and no duration (only an end_timestamp): dropped.

    def _parse_kindle_reading_session(self, root: Path) -> Iterator[NormalizedItem]:
        yield from self._parse_csv_category(
            root, "Kindle.Devices.ReadingSession*.csv", self._normalize_kindle_session
        )

    def _normalize_kindle_session(self, row: Dict[str, str]) -> Optional[NormalizedItem]:
        start = _parse_amazon_date(_first_present(row, "start_timestamp"))
        duration_ms = _parse_amount(_first_present(row, "total_reading_millis"))
        if start is None and duration_ms is None:
            return None

        content_id = _kindle_value(row, "ASIN")
        title, author, context = self._kindle_book(content_id)
        fields: Dict[str, Any] = {}
        if duration_ms is not None:
            fields["Durée de lecture"] = f"{duration_ms / 60000:.1f} min"
        pages = _first_present(row, "number_of_page_flips")
        if pages:
            fields["Pages tournées"] = pages
        fields.update(self._kindle_device_fields(_kindle_value(row, "device_serial_number")))
        kind = _KINDLE_CONTENT_TYPES.get(_kindle_value(row, "content_type") or "")
        if kind:
            fields["Type"] = kind
        fields.update(_kindle_id_fields(content_id))

        return NormalizedItem(
            item_type="reading_session",
            category="Kindle",
            timestamp=start,
            title=None,
            content=title or (f"Session de lecture ({content_id})" if content_id else "Session de lecture"),
            author=author,
            context=context,
            fields=fields or None,
            media=[],
            raw=row,
        )

    # "Kindle.ReadingInsights/.../Kindle.UserUniqueTitlesCompleted.csv" --
    # confirmed real header: asin_date_and_content_type, personal_document_id,
    # product_name. The first column packs three values:
    # "B00KZ0L5FE_2024-02-20_AUTOMATIC" (ASIN, day finished, how), with
    # "Not Applicable" for the ASIN of a personal document. 68 real rows,
    # 59 AUTOMATIC (the end of the book was reached), 9 MANUAL.

    def _parse_kindle_finished(self, root: Path) -> Iterator[NormalizedItem]:
        yield from self._parse_csv_category(
            root, "Kindle.UserUniqueTitlesCompleted*.csv", self._normalize_kindle_finished
        )

    def _normalize_kindle_finished(self, row: Dict[str, str]) -> Optional[NormalizedItem]:
        match = _FINISHED_KEY_RE.match((row.get("asin_date_and_content_type") or "").strip())
        if not match:
            return None
        content_id = _finished_id(row)
        title, author, context = self._kindle_book(content_id, _kindle_value(row, "product_name"))
        if not context:
            return None
        try:
            finished_on = datetime.strptime(match["date"], "%Y-%m-%d")
        except ValueError:
            return None

        fields: Dict[str, Any] = {}
        how = _FINISHED_HOW.get(match["how"].upper())
        if how:
            fields["Marqué terminé"] = how
        fields.update(_kindle_id_fields(content_id))

        return NormalizedItem(
            item_type="kindle_finished",
            category="Kindle",
            timestamp=finished_on,
            title=None,
            content=title or content_id,
            author=author,
            context=context,
            fields=fields or None,
            media=[],
            raw=row,
        )

    # "Digital.Content.Whispersync/whispersync.csv" -- confirmed real
    # header: ASIN, Non ASIN (a personal document's id), Product Name,
    # Annotation Type, Customer modified date on device, Device Serial
    # Number, Third Party Device, ContentType, Format, Creation Date,
    # LastUpdatedDate, Highlight, Version, Selection Type, Note, Is Deleted.
    # The text of a highlight is NOT exported: on the real export, "Highlight"
    # only holds its colour ("{mchl_color:yellow}") or "Not Available" -- an
    # annotation is a dated mark in a book, nothing more. 274 of the 967
    # real highlight rows repeat another one to the second (up to 55
    # times): sync copies, collapsed by _parse_csv_category. The reading
    # positions it also syncs (kindle.last_read, continuous_read,
    # most_recent_read, audible.last_heard) are left out: they duplicate
    # the sessions.

    def _parse_kindle_annotations(self, root: Path) -> Iterator[NormalizedItem]:
        yield from self._parse_csv_category(root, "whispersync*.csv", self._normalize_kindle_annotation)

    def _normalize_kindle_annotation(self, row: Dict[str, str]) -> Optional[NormalizedItem]:
        item_type = _KINDLE_ANNOTATION_TYPES.get((row.get("Annotation Type") or "").strip())
        if item_type is None or (_kindle_value(row, "Is Deleted") or "").lower() == "yes":
            return None
        content_id = _kindle_value(row, "ASIN", "Non ASIN")
        title, author, context = self._kindle_book(content_id, _kindle_value(row, "Product Name"))
        when = _parse_amazon_date(_kindle_value(row, "Customer modified date on device", "Creation Date"))

        fields: Dict[str, Any] = {}
        text = None
        for column in ("Note", "Highlight"):
            value = _kindle_value(row, column)
            color = _HIGHLIGHT_COLOR_RE.match(value) if value else None
            if color:
                fields["Couleur"] = _HIGHLIGHT_COLORS.get(color[1], color[1].replace("_", " "))
            elif value and text is None:
                text = value
        if title and text:
            fields["Livre"] = title
        fields.update(self._kindle_device_fields(_kindle_value(row, "Device Serial Number")))
        fields.update(_kindle_id_fields(content_id))

        return NormalizedItem(
            item_type=item_type,
            category="Kindle",
            timestamp=when,
            title=None,
            content=text or title or content_id,
            author=author,
            context=context,
            fields=fields or None,
            media=[],
            raw=row,
        )

    # "Digital.Content.Ownership/Digital.Content.Ownership.<n>.json" -- a
    # list of {resource: {resourceType, catalog, ASIN, "Product Name"},
    # rights: [{rightType, origin: {originType}, rightStatus, acquiredDate,
    # consumptions}], lastUpdatedDate, entity}. On the real export, 1 232
    # entries: 772 personal documents (no ASIN, no name -- the documents
    # below cover them), 356 dictionaries installed by the device, and 26
    # books bought, each listed up to 4 times. One item per ASIN, dated by
    # its first acquisition still active.

    def _parse_kindle_library(self, root: Path) -> Iterator[NormalizedItem]:
        books: Dict[str, Dict[str, Any]] = {}
        for entry in _ownership_entries(root):
            resource = entry.get("resource") or {}
            asin = _kindle_value(resource, "ASIN")
            rights = [r for r in entry.get("rights") or [] if isinstance(r, dict)]
            origins = {_kindle_value(r.get("origin") or {}, "originType") for r in rights} - {None}
            if not asin or not origins or origins <= _IGNORED_ORIGINS:
                continue
            book = books.setdefault(asin, {"origins": set(), "rights": [], "entries": [], "name": None})
            book["origins"] |= origins - _IGNORED_ORIGINS
            book["rights"].extend(rights)
            book["entries"].append(entry)
            book["name"] = book["name"] or _kindle_value(resource, "Product Name")

        for asin, book in books.items():
            try:
                item = self._normalize_kindle_book(asin, book)
            except Exception:
                continue
            if item is not None:
                yield item

    def _normalize_kindle_book(self, asin: str, book: Dict[str, Any]) -> Optional[NormalizedItem]:
        title, author, context = self._kindle_book(asin, book["name"])
        active = [r for r in book["rights"] if (r.get("rightStatus") or "").lower() == "active"]
        dates = [d for d in (_parse_amazon_date(r.get("acquiredDate")) for r in active or book["rights"]) if d]

        fields: Dict[str, Any] = {
            "Origine": ", ".join(sorted(_BOOK_ORIGINS.get(o, o) for o in book["origins"])),
            "Statut": "Actif" if active else "Révoqué",
        }
        fields.update(_kindle_id_fields(asin))

        return NormalizedItem(
            item_type="kindle_book",
            category="Bibliothèque Kindle",
            timestamp=min(dates) if dates else None,
            title=None,
            content=title or asin,
            author=author,
            context=context,
            fields=fields,
            media=[],
            raw=book["entries"],
        )

    # "Kindle.KindleDocs/.../Kindle.KindleDocs.DocumentMetadata.csv" --
    # confirmed real header: DocumentId, Title, DocumentProvider (the
    # author on every real row), Filename, DocumentOriginalType (a MIME
    # type), DocumentSizeInBytes, DocumentTypeAndConvertionCompletionStatus
    # 1..5, HasBeenDeleted, EntryCreationDate. One row per document sent to
    # the Kindle (Send to Kindle, e-mail): 193 real rows.

    def _parse_kindle_documents(self, root: Path) -> Iterator[NormalizedItem]:
        yield from self._parse_csv_category(
            root, "Kindle.KindleDocs.DocumentMetadata*.csv", self._normalize_kindle_document
        )

    def _normalize_kindle_document(self, row: Dict[str, str]) -> Optional[NormalizedItem]:
        doc_id = _kindle_value(row, "DocumentId")
        title, author, context = self._kindle_book(doc_id, _kindle_value(row, "Title"))
        if not context:
            return None

        fields: Dict[str, Any] = {}
        filename = _kindle_value(row, "Filename")
        if filename:
            fields["Fichier"] = filename
        mime = _kindle_value(row, "DocumentOriginalType")
        if mime:
            fields["Format d'origine"] = _DOCUMENT_FORMATS.get(mime, mime)
        size = _parse_amount(_kindle_value(row, "DocumentSizeInBytes"))
        if size:
            fields["Taille"] = f"{size / 1_000_000:.1f} Mo"
        deleted = _kindle_value(row, "HasBeenDeleted")
        if deleted:
            fields["Supprimé"] = "Oui" if deleted.lower() == "yes" else "Non"
        fields.update(_kindle_id_fields(doc_id))

        return NormalizedItem(
            item_type="kindle_document",
            category="Bibliothèque Kindle",
            timestamp=_parse_amazon_date(_kindle_value(row, "EntryCreationDate")),
            title=None,
            content=title or doc_id,
            author=author,
            context=context,
            fields=fields or None,
            media=[],
            raw=row,
        )

    # "Kindle.BookRewards/.../Kindle.BookRewards.Achievements.2.csv" --
    # confirmed real header: AchievementGroupName ("2024 Kindle Spring
    # Challenge"), AchievementName ("SilverReader"), EarnDate, Marketplace,
    # Quantity.

    def _parse_kindle_badges(self, root: Path) -> Iterator[NormalizedItem]:
        yield from self._parse_csv_category(
            root, "Kindle.BookRewards.Achievements*.csv", self._normalize_kindle_badge
        )

    def _normalize_kindle_badge(self, row: Dict[str, str]) -> Optional[NormalizedItem]:
        badge = _kindle_value(row, "AchievementName")
        if not badge:
            return None
        challenge = _kindle_value(row, "AchievementGroupName")
        fields: Dict[str, Any] = {}
        if challenge:
            fields["Défi"] = challenge
        return NormalizedItem(
            item_type="kindle_badge",
            category="Kindle",
            timestamp=_parse_amazon_date(_kindle_value(row, "EarnDate")),
            title=None,
            # "SilverReader" -> "Silver Reader"
            content=re.sub(r"(?<=[a-z])(?=[A-Z])", " ", badge),
            author=None,
            context=challenge,
            fields=fields or None,
            media=[],
            raw=row,
        )


# Kindle files write a missing value several ways, in any column.
_KINDLE_PLACEHOLDERS = {"not available", "not applicable", "no data"}
_KINDLE_CONTENT_TYPES = {"E-Book": "Livre", "Personal Document": "Document personnel"}
_FINISHED_KEY_RE = re.compile(r"^(?P<id>.*)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<how>[A-Za-z]+)$")
_FINISHED_HOW = {"AUTOMATIC": "Automatiquement (fin du livre atteinte)", "MANUAL": "Manuellement"}
_KINDLE_ANNOTATION_TYPES = {
    "kindle.highlight": "kindle_highlight", "kindle.note": "kindle_note", "kindle.bookmark": "kindle_bookmark",
}
_HIGHLIGHT_COLOR_RE = re.compile(r"^\{mchl_color:(\w+)\}$")
_HIGHLIGHT_COLORS = {
    "yellow": "Jaune", "blue": "Bleu", "dark_blue": "Bleu foncé", "pink": "Rose", "orange": "Orange",
    "purple": "Violet", "green": "Vert",
}
# Ownership origins that are no book of the reader's: dictionaries come
# with the device, personal documents are read from DocumentMetadata.
_IGNORED_ORIGINS = {"KindleDictionary", "PDocs"}
_BOOK_ORIGINS = {
    "Purchase": "Achat", "KindleUnlimited": "Kindle Unlimited", "PrimeReading": "Prime Reading",
    "Sample": "Extrait", "Loan": "Prêt", "Lending": "Prêt", "Free": "Gratuit",
}
_DOCUMENT_FORMATS = {
    "application/pdf": "PDF", "application/epub+zip": "EPUB", "application/x-mobipocket-ebook": "MOBI",
    "application/x-mobi8-ebook": "AZW3 (KF8)", "application/x-kfx-ebook": "KFX",
    "application/msword": "Word", "text/plain": "Texte",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word",
}


def _kindle_value(row: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip() and value.strip().lower() not in _KINDLE_PLACEHOLDERS:
            return value.strip()
    return None


def _kindle_id_fields(content_id: Optional[str]) -> Dict[str, str]:
    """A book from the store is named by its 10-character ASIN (and has a
    store page), a personal document by an id of its own."""
    if not content_id:
        return {}
    if len(content_id) == 10:
        return {"ASIN": content_id, "Lien": f"https://www.amazon.fr/dp/{content_id}"}
    return {"Document": content_id}


def _finished_id(row: Dict[str, str]) -> Optional[str]:
    match = _FINISHED_KEY_RE.match((row.get("asin_date_and_content_type") or "").strip())
    asin = _kindle_value({"id": match["id"]}, "id") if match else None
    return asin or _kindle_value(row, "personal_document_id")


def _author_name(value: Optional[str]) -> Optional[str]:
    """"Sullivan, Dan" or "Safier,David" (how the library index and many
    documents write authors) -> "Dan Sullivan"; "Unknown" -> no author."""
    if not value or value.lower() == "unknown":
        return None
    if value.count(",") == 1:
        last, first = (part.strip() for part in value.split(","))
        return f"{first} {last}" if first and last else value
    return value


def _ownership_entries(root: Path) -> Iterator[Dict[str, Any]]:
    for path in iter_files(root, "Digital.Content.Ownership*.json"):
        try:
            data = load_json(path)
        except Exception:
            continue
        if isinstance(data, list):
            yield from (entry for entry in data if isinstance(entry, dict))
