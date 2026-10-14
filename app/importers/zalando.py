"""Zalando's GDPR export ("Mon compte › Mes données › Télécharger mes données").

A Zalando export is a FLAT zip of CSVs -- unlike Meta/Google/Amazon there is no
folder tree (only an empty "Communication avec le service client" one holding a
stray `_processed` marker), no manifest, and no published schema. Every file is
named `<uuid>-<name>.csv`, the uuid being the export's own id, and its `<name>`
half is the only stable thing to match on. Everything below was read off a real
French export (10/2026, 10 files, 11 orders, 26 order lines) rather than off a
documentation, which does not exist for this export: field names are taken
verbatim, and a record missing a field is skipped rather than guessed at.

Four points need a word of explanation:

* **The order is split across two files.** `orders-master.csv` holds the order
  itself (when it was placed, what it was invoiced, how it was paid, where it
  went, who carried it), `orders-products.csv` holds its lines (which article,
  which size, what it cost, whether it was kept). The lines only carry the
  order *number*, so the two are joined here on it, and every order-level fact
  is denormalized onto each line -- exactly how Amazon repeats an order's
  columns on every one of its item rows, and what lets the Achats page group
  Zalando's spend by shop, payment method, status or shipping address.

* **`orders-products.csv` is the one file with NO header row.** Read with
  `csv.DictReader` it would silently swallow its first article as a header --
  which is why it is read positionally (`PRODUCT_COLUMNS`) and why the column
  names are exported at module level: app/anonymize.py has to know them too, or
  its own header assumption would write one real order line out verbatim.

* **Product names and partner brands carry HTML.** A name looks like
  `Wide Fit (G) Fit - Elastic Lace Trainers  .<br aria-hidden='true'><strong>
  Vendu par Next</strong>`, and the order's "Marques partenaires" is
  `<b>Next</b>: c/o BDO AG Wirtschaftsprüfungsgesellschaft, Zielstattstr. 40…`.
  The seller is worth keeping, the markup is not, and the two files do not even
  spell their attribute alike (`aria-hidden` in a product name, the typo
  `arial-hidden` in a partner brand) -- so the stripper drops any tag rather
  than matching either spelling, and turns them into separators rather than
  into nothing, so a name and its seller do not run together.

* **An order's own price is not its line's price.** `Montant de la facture` is
  what the whole order was invoiced; each line's `price` is its own share (on
  the real export the lines sum exactly to the invoice, all 11 orders). Only
  the *line* price becomes the item's amount: putting the invoice total on
  every line would multiply an order's spend by its number of articles.

Returns are items of their own (`zalando_return`), as Amazon's and Seazon's are:
a line the export marks `Articles renvoyés` also yields an item carrying what
came back, so the Achats page nets it out of the spend. Two things the export
does *not* state are settled the only honest way available -- the refunded
amount is the line's own price (the single figure the export gives), and the
return is dated by the order's purchase date (it gives no return date). The
returned line itself stays a `zalando_order` with its price, as Amazon's Order
History keeps what was returned to it.

A Zalando line is its own item type rather than Amazon's shared `order_item`:
it holds none of what an Amazon line is made of (no department, no ASIN, no
product condition) and has a size, an article reference and a seller instead.
Keeping them apart is what lets the platform's purchase page offer its own
dimensions instead of Amazon's -- the same reasoning as app/importers/seazon.py.
"""
import csv
import html
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from app.importers.base import BaseImporter, NormalizedItem
from app.importers.registry import register

# Every file is "<uuid>-<name>.csv" -- the uuid is this export's id, so only the
# half after it is a stable name.
_UUID_PREFIX_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}-", re.IGNORECASE)

# The files an export can carry, by their uuid-stripped name. The export read
# here holds all of them, but one is enough to recognize it: Zalando does not
# say whether its download page lets you pick which sets to include (Seazon's
# does), and an importer that demanded the whole set would return nothing if it
# did.
_KNOWN_FILES = {
    "account", "addresses", "contact-information", "login",
    "orders-master", "orders-products", "wishlist",
}
# ...except that half of those names are generic enough for any CSV zip to
# carry one, where Seazon's `seazon_*.json` cannot be anything else. Those only
# count on a file wearing the export's own `<uuid>-` prefix (or a per-entity
# name, which is Zalando's alone); the rest are taken on their name alone.
_DISTINCTIVE_FILES = {"orders-master", "orders-products", "contact-information"}
_GENERIC_FILES = _KNOWN_FILES - _DISTINCTIVE_FILES
# Two families ship one file PER ZALANDO ENTITY (`newsletter-lounge`,
# `size_fit-measurement_lounge`), so they are globbed rather than listed.
_ENTITY_FILES = ("newsletter", "size_fit-measurement")

# `orders-products.csv` has no header row: its columns are only known
# positionally. Module-level (not private) because app/anonymize.py reads it.
PRODUCT_COLUMNS = ("order_id", "article_number", "product_name", "size", "price", "date", "state")

# The one line state that means the article came back. The other seen on the
# real export is "Articles gardés".
_RETURNED_STATE = "articles renvoyés"

# An amount is written "35,00 €" -- a French decimal comma, a (narrow) no-break
# space and the currency symbol, which is not a space a plain split() would cut
# on. The WHOLE run of digits and separators is taken (the regex only finds where
# the number starts and ends): stopping at the first decimal mark would read a
# read a thousands group as the whole number, "1 234,56 €" as 1.00.
_AMOUNT_RE = re.compile(r"-?\d[\d   .,]*\d|-?\d")

# French long dates ("11 octobre 2026"): strptime's %B is C-locale, i.e. English
# month names, and this export is French whatever the account's language. The
# unaccented spellings are accepted too, and the day's ordinal ("1er janvier")
# tolerated -- neither has been seen in the export, and getting either wrong
# would drop a date silently rather than loudly.
_MONTHS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
    "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}
_FR_LONG_DATE_RE = re.compile(r"^(\d{1,2})(?:er|e)?\s+([A-Za-zéûôàè]+)\s+(\d{4})$", re.IGNORECASE)

# The markup the export embeds in a product name and in a partner brand. Any
# tag is stripped, whatever the attribute it carries is spelled like.
_TAG_RE = re.compile(r"<[^>]*>")
# The seller, appended to a product name as its own marked-up chunk.
_SELLER_RE = re.compile(r"^Vendu par\s+(?P<seller>.+)$", re.IGNORECASE)


def _text(value: Any) -> Optional[str]:
    return value.strip() or None if isinstance(value, str) else None


def _logical_name(path: Path) -> str:
    """`268294db-…-orders-master.csv` -> `orders-master`."""
    name = _UUID_PREFIX_RE.sub("", path.name)
    return name[:-4] if name.lower().endswith(".csv") else name


def _entity_of(stem: str) -> Optional[Tuple[str, str]]:
    """(family, entity) for a per-entity file name, or None. The two families do
    not even agree on their separator -- `newsletter-lounge` but
    `size_fit-measurement_lounge` -- so both are accepted."""
    lowered = stem.lower()
    for family in _ENTITY_FILES:
        if lowered.startswith(family) and len(lowered) > len(family) and lowered[len(family)] in "-_":
            return family, stem[len(family) + 1:]
    return None


def _entity_label(entity: str) -> str:
    """`lounge` / `zalando_se` -- the Zalando entity a per-entity file belongs
    to, taken verbatim from the file name and de-slugified for display."""
    return " ".join(part.upper() if len(part) <= 2 else part.capitalize()
                    for part in entity.replace("_", " ").split())


def csv_header_for(path: Path) -> Optional[Tuple[str, ...]]:
    """The column names of a Zalando CSV that ships WITHOUT a header row, or
    None for the ones that carry one. app/anonymize.py anonymizes every row of
    such a file as data: its first line is an order line, not column names, and
    treating it as a header would write it out verbatim."""
    return PRODUCT_COLUMNS if _logical_name(path).lower() == "orders-products" else None


def _parse_date(value: Any) -> Optional[datetime]:
    """A Zalando date, in whichever of its three shapes: the French long form
    of `account.csv` and `orders-master.csv` ("26 novembre 2021"), the ISO date
    of an order line, or the ISO timestamp of a login. An unparseable value is
    treated as absent, never as "now"."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    match = _FR_LONG_DATE_RE.match(text)
    if match:
        month = _MONTHS_FR.get(match.group(2).lower())
        if not month:
            return None
        try:
            return datetime(int(match.group(3)), month, int(match.group(1)))
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _date_fr(value: Any) -> Optional[str]:
    parsed = _parse_date(value)
    return parsed.strftime("%d/%m/%Y") if parsed else None


def _amount(value: Any) -> Optional[float]:
    """"1 234,56 €" -> 1234.56. Anything with no number in it is absent, never 0."""
    if not isinstance(value, str):
        return None
    match = _AMOUNT_RE.search(value)
    if not match:
        return None
    # The separators are not distinguishable by the regex: whichever of "," and
    # "." comes LAST is the decimal mark (French groups the thousands with a
    # space or a dot -- "1.234,56" -- and only ever writes one decimal mark), the
    # other one groups thousands and goes.
    digits = match.group(0).translate({ord(c): None for c in "   "})
    if digits.rfind(",") > digits.rfind("."):
        digits = digits.replace(".", "").replace(",", ".")
    else:
        digits = digits.replace(",", "")
    try:
        return float(digits)
    except ValueError:
        return None


def _euros(value: Optional[float]) -> Optional[str]:
    """Stored the way every other amount in the app is ("35.00 EUR", not
    "35,00 €"), so stats.extract_leading_number/extract_currency keep working."""
    return None if value is None else f"{value:.2f} EUR"


def _chunks(raw: Any) -> List[str]:
    """A cell's text, one entry per marked-up piece. Tags become separators
    rather than vanishing, so "Name<br><strong>Vendu par X</strong>" does not
    run the article and its seller together into one word."""
    if not isinstance(raw, str):
        return []
    text = html.unescape(_TAG_RE.sub("\n", raw))
    return [line.strip() for line in text.splitlines() if line.strip()]


def _clean(raw: Any) -> Optional[str]:
    text = " ".join(_chunks(raw)).strip()
    return text or None


def _split_seller(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    """(article name, seller) out of a product-name cell. The export appends
    the seller as a marked-up tail, after a stray "  ." that is not part of the
    name."""
    seller = None
    name_parts: List[str] = []
    for chunk in _chunks(raw):
        match = _SELLER_RE.match(chunk)
        if match:
            seller = match.group("seller").strip() or None
        else:
            name_parts.append(chunk)
    name = " ".join(name_parts).strip().rstrip(" .-,").strip() or None
    return name, seller


def _partner_brand(raw: Any) -> Optional[str]:
    """"Marques partenaires": the partner brand, marked up in <b> and followed
    by its postal address. "-" means the order had no partner brand."""
    chunks = _chunks(raw)
    if not chunks or chunks[0] in ("-", "–", "—"):
        return None
    return chunks[0].rstrip(":").strip() or None


def _find(root: Path, stem: str) -> List[Path]:
    """Every CSV whose uuid-stripped name is `stem` (the bare name is matched
    too, defensively, in case a file is ever written without the uuid)."""
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.name.lower().endswith(".csv")
        and _logical_name(path).lower() == stem.lower()
    )


def _find_entity(root: Path, family: str) -> List[Tuple[str, Path]]:
    """[(entity, path)] for one per-entity family of files."""
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or not path.name.lower().endswith(".csv"):
            continue
        parsed = _entity_of(_logical_name(path))
        if parsed and parsed[0] == family:
            found.append((parsed[1], path))
    return found


def _read_rows(path: Path) -> List[Dict[str, str]]:
    if not path.is_file():
        return []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            return [row for row in csv.DictReader(handle)]
    except (OSError, csv.Error):
        return []


def _read_products(path: Path) -> List[Dict[str, str]]:
    """Positional read of the header-less `orders-products.csv` -- its first
    line is an article, and a DictReader would eat it as column names."""
    if not path.is_file():
        return []
    rows: List[Dict[str, str]] = []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            for cells in csv.reader(handle):
                if not any(cell.strip() for cell in cells):
                    continue
                rows.append({column: cells[i] for i, column in enumerate(PRODUCT_COLUMNS) if i < len(cells)})
    except (OSError, csv.Error):
        return []
    return rows


@register
class ZalandoImporter(BaseImporter):
    key = "zalando"
    label = "Zalando"
    type_labels = {
        "zalando_order": "Article commandé",
        "zalando_return": "Article renvoyé",
        "wishlist_item": "Article souhaité",
        "newsletter_preference": "Abonnement newsletter",
        "size_fit": "Mensurations",
    }

    def detect(self, root: Path) -> bool:
        # One known file is enough -- see _KNOWN_FILES and _GENERIC_FILES.
        for path in root.rglob("*"):
            if not path.is_file() or not path.name.lower().endswith(".csv"):
                continue
            logical = _logical_name(path)
            if logical.lower() in _DISTINCTIVE_FILES or _entity_of(logical):
                return True
            if logical.lower() in _GENERIC_FILES and _UUID_PREFIX_RE.match(path.name):
                return True
        return False

    def parse(self, root: Path) -> Iterator[NormalizedItem]:
        orders = self._orders(root)
        yield from self._parse_lines(root, orders)
        yield from self._parse_logins(root)
        yield from self._parse_wishlist(root)
        yield from self._parse_newsletters(root)
        yield from self._parse_size_fit(root)
        yield from self._parse_profile(root)

    # -- Orders: the lines of `orders-products.csv`, against `orders-master` ---

    def _orders(self, root: Path) -> Dict[str, Dict[str, str]]:
        """{order number: the order's row of `orders-master.csv`} -- an order
        line only knows the number, every order-level fact living there."""
        orders: Dict[str, Dict[str, str]] = {}
        for path in _find(root, "orders-master"):
            for row in _read_rows(path):
                number = _text(row.get("Numéro de commande"))
                if number and number not in orders:
                    orders[number] = row
        return orders

    def _parse_lines(self, root: Path, orders: Dict[str, Dict[str, str]]) -> Iterator[NormalizedItem]:
        for path in _find(root, "orders-products"):
            for line in _read_products(path):
                try:
                    number = _text(line.get("order_id"))
                    if not number:
                        continue
                    yield from self._line_items(orders.get(number) or {}, line)
                except Exception:
                    continue
        # An order listed in `orders-master.csv` with no line in
        # `orders-products.csv` is deliberately NOT turned into one item
        # carrying its invoice total. That stand-in would be a shape the export
        # has not been seen to produce, and it would break the one invariant
        # cross-import deduplication rests on: the same order would then be two
        # unrelated records (the stand-in, and the lines of a later export that
        # does carry them), so both copies would count and an order's spend be
        # doubled. An export missing its article list yields no orders, visibly.

    def _order_fields(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """The order-level facts, repeated on each of its lines. Which labels
        the purchases page's dimensions read back by name -- "Statut", "Mode de
        paiement", the two addresses through app/stats.py `_PURCHASE_GROUP_FIELDS`,
        "Boutique" as the category one -- is fixed there, so these are not
        free-form wording."""
        fields: Dict[str, Any] = {}
        for label, key in (
            ("Boutique", "Commande sur"),            # Zalando / Zalando Privé
            ("Statut", "Statut de la commande"),
            ("Mode de paiement", "Mode de paiement"),
            ("Adresse de livraison", "Adresse de livraison"),
            ("Adresse de facturation", "Adresse de facturation"),
            ("Expédié par", "Envoyé par"),
            ("Suivi", "Numéro d'expédition"),        # one number, or two
        ):
            value = _clean(order.get(key))
            if value:
                fields[label] = value
        brand = _partner_brand(order.get("Marques partenaires"))
        if brand:
            fields["Marque partenaire"] = brand
        return fields

    def _line_items(self, order: Dict[str, Any], line: Dict[str, str]) -> Iterator[NormalizedItem]:
        name, seller = _split_seller(line.get("product_name"))
        number = _text(line.get("order_id"))
        # The order was placed on the master's date; the line repeats it in ISO
        # form, and stands in for it if the master half is missing.
        timestamp = _parse_date(order.get("Acheté le")) or _parse_date(line.get("date"))
        price = _amount(line.get("price"))
        state = _text(line.get("state"))

        fields = self._order_fields(order)
        if price is not None:
            fields["Montant"] = _euros(price)
        for label, value in (
            ("Taille", _text(line.get("size"))),
            ("Référence", _text(line.get("article_number"))),
            ("Vendu par", seller),
            ("État de l'article", state),
        ):
            if value:
                fields[label] = value

        yield NormalizedItem(
            item_type="zalando_order",
            category="Commandes",
            timestamp=timestamp,
            # The article's name goes in `content`, not `title`: that is where
            # the Achats page reads a purchase line's product from (see
            # stats.build_purchase_list), as Amazon's own lines do.
            content=name,
            context=number,  # the order: every line of it shares this
            fields=fields or None,
            raw={"line": line, "order": order or None},
        )

        if (state or "").lower() == _RETURNED_STATE:
            yield self._return_item(order, line, name, seller, price, timestamp, number)

    def _return_item(self, order: Dict[str, Any], line: Dict[str, str], name: Optional[str],
                     seller: Optional[str], price: Optional[float], timestamp: Optional[datetime],
                     number: Optional[str]) -> NormalizedItem:
        """What came back on an order line, as its own item so the Achats page
        can net it out of the spend (see the module docstring: the amount is
        the line's price and the date the order's, the export stating neither
        a refunded amount nor a return date)."""
        fields: Dict[str, Any] = {}
        if price is not None:
            fields["Montant remboursé"] = _euros(price)
        for label, value in (
            ("Taille", _text(line.get("size"))),
            ("Référence", _text(line.get("article_number"))),
            ("Vendu par", seller),
        ):
            if value:
                fields[label] = value
        return NormalizedItem(
            item_type="zalando_return",
            category="Retours",
            timestamp=timestamp,
            content=name,
            context=number,
            fields=fields or None,
            raw={"line": line, "order": order or None},
        )

    # -- The account: sign-ins, wishlist, newsletters, measurements, profile ---

    def _parse_logins(self, root: Path) -> Iterator[NormalizedItem]:
        for path in _find(root, "login"):
            for row in _read_rows(path):
                try:
                    fields: Dict[str, Any] = {}
                    for label, key in (("E-mail", "Adresse e-mail"), ("Adresse IP", "Adresse IP")):
                        value = _text(row.get(key))
                        if value:
                            fields[label] = value
                    yield NormalizedItem(
                        item_type="login",
                        category="Connexions",
                        timestamp=_parse_date(row.get("Date et heure")),
                        title="Connexion",
                        fields=fields or None,
                        raw=row,
                    )
                except Exception:
                    continue

    def _parse_wishlist(self, root: Path) -> Iterator[NormalizedItem]:
        for path in _find(root, "wishlist"):
            for row in _read_rows(path):
                try:
                    fields: Dict[str, Any] = {}
                    reference = _text(row.get("Numéro de l'article"))
                    if reference:
                        fields["Référence"] = reference
                    size = _text(row.get("Taille sauvegardée"))
                    if size:
                        fields["Taille"] = size
                    yield NormalizedItem(
                        item_type="wishlist_item",
                        category="Liste d'envies",
                        # The wishlist carries no date of its own: an article
                        # saved is not dated by the export, and dating it by the
                        # import would invent one.
                        timestamp=None,
                        title=_clean(row.get("Description de l'article")),
                        fields=fields or None,
                        raw=row,
                    )
                except Exception:
                    continue

    def _parse_newsletters(self, root: Path) -> Iterator[NormalizedItem]:
        """One file per Zalando entity, each holding whether the customer
        consented to its e-mails and which topics they chose."""
        for entity, path in _find_entity(root, "newsletter"):
            for row in _read_rows(path):
                try:
                    fields: Dict[str, Any] = {}
                    consent = _text(row.get("Consentement de réception des e-mails"))
                    if consent:
                        fields["Consentement e-mail"] = consent
                    yield NormalizedItem(
                        item_type="newsletter_preference",
                        category="Newsletters",
                        timestamp=None,
                        title=f"Newsletter {_entity_label(entity)}",
                        content=_text(row.get("Préférences de newsletter enregistrées")),
                        context=entity,
                        fields=fields or None,
                        raw=row,
                    )
                except Exception:
                    continue

    def _parse_size_fit(self, root: Path) -> Iterator[NormalizedItem]:
        """One item per entity whose measurements were entered, one field per
        garment category. The real export ships these files with their header
        and not a single row, so this yields nothing until measurements exist."""
        for entity, path in _find_entity(root, "size_fit-measurement"):
            for row in _read_rows(path):
                try:
                    fields = {label.strip(): value.strip() for label, value in row.items()
                              if _text(label) and _text(value)}
                    if not fields:
                        continue
                    yield NormalizedItem(
                        item_type="size_fit",
                        category="Mensurations",
                        timestamp=None,
                        title=_entity_label(entity),
                        context=entity,
                        fields=fields,
                        raw=row,
                    )
                except Exception:
                    continue

    def _parse_profile(self, root: Path) -> Iterator[NormalizedItem]:
        account = next((row for path in _find(root, "account") for row in _read_rows(path)), None)
        if not account:
            return
        try:
            fields: Dict[str, Any] = {}
            for label, key in (
                ("Numéro de client", "Numéro de client"),
                ("E-mail", "Adresse e-mail"),
                ("Téléphone", "Numéro de téléphone"),
                ("Numéro de sécurité sociale", "Numéro de sécurité sociale"),
                ("Préférences en matière de mode", "Préférences en matière de mode"),
                ("Catégorie client", "Catégorie client"),
                ("Abonnement Plus", "Abonnement Plus"),
                ("Comptes liés", "Comptes liés"),
                ("Abonnements connectés", "Abonnements connectés"),
            ):
                # "-" is how the export says "not set" (the real one holds no
                # social security number, no linked account): not a value.
                value = _text(account.get(key))
                if value and value != "-":
                    fields[label] = value
            for label, key in (("Date de naissance", "Date de naissance"),
                               ("Compte créé le", "Compte créé le")):
                formatted = _date_fr(account.get(key))
                if formatted:
                    fields[label] = formatted
            addresses = self._saved_addresses(root)
            if addresses:
                fields["Adresses"] = " · ".join(addresses)
            # `contact-information.csv` ships EMPTY in the export read here (a
            # bare line break, not even a header row). Whatever it one day
            # holds is folded in verbatim rather than dropped on the floor.
            contact = self._contact_information(root)
            if contact:
                fields["Coordonnées"] = " · ".join(f"{key} : {value}" for key, value in contact.items())

            yield NormalizedItem(
                item_type="profile",
                category="Compte",
                timestamp=_parse_date(account.get("Compte créé le")),
                title=_text(account.get("Nom")),
                content="Compte Zalando",
                context=_text(account.get("Numéro de client")),
                fields=fields or None,
                raw=account,
            )
        except Exception:
            return

    def _saved_addresses(self, root: Path) -> List[str]:
        """The account's address book (`addresses.csv`): an order carries its own
        delivery and billing addresses, so this is a separate, shorter list."""
        addresses: List[str] = []
        for path in _find(root, "addresses"):
            for row in _read_rows(path):
                for key, value in row.items():
                    if _text(key) and "adresse" in key.lower() and _text(value):
                        addresses.append(value.strip())
        return addresses

    def _contact_information(self, root: Path) -> Dict[str, str]:
        contact: Dict[str, str] = {}
        for path in _find(root, "contact-information"):
            for row in _read_rows(path):
                for key, value in row.items():
                    if _text(key) and _text(value):
                        contact[key.strip()] = value.strip()
        return contact
