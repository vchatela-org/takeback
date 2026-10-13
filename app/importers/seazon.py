"""Seazon (the meal-kit brand of FoodCheri) GDPR export.

Requested from the account page ("Mes données" / RGPD), a Seazon export is a
FLAT zip of `seazon_*.json` files -- no folders, unlike Meta/Google/Amazon --
plus `foodcheri_user.json`, which kept the parent company's name: Seazon was
launched by FoodCheri, and that one file still holds the meal-taste
preferences under the old brand.

One file per domain: `seazon_order.json` (every shipping slot the plan ever
created), `seazon_opinion.json` (the rating left after a delivery),
`seazon_plan.json` (the subscription's whole history: one record per plan
change, not just the current one), `seazon_notification.json` (what Seazon
sent the customer), `seazon_log.json` (the back office's audit trail),
`seazon_session.json` (logins), `seazon_user.json` (profile, addresses,
coupons, loyalty) and `seazon_gorgias_ticket.json` (support tickets -- Gorgias
is the helpdesk Seazon runs).

Everything below was read off a real French export (10/2026, 9 files, ~1 800
items) rather than a documented schema, which doesn't exist for this export:
field names are taken verbatim, and a record missing a field is skipped
rather than guessed at.

Three points need a word of explanation:

* **What counts as an order.** A plan creates a shipping slot every week
  (`seazon_order.json` holds 180 of them for ~40 real orders); only 40 of them
  were ever charged (`prices` is present, `state` is `paid`/`shipped`, and
  Seazon's own profile says `order_count: 40` / `total_revenue: 3056.73` for
  exactly those). The other 132 were cancelled before any payment, and 52 of
  those still hold the meals the customer had picked. So a charged slot
  becomes a `seazon_order` (what the Achats tab spends), a cancelled one
  becomes an `order_cancelled` -- never counted as a purchase, since no money
  moved -- and the meals of both are exported separately as `meal` items: the
  export states ONE price per order (never per dish), so pricing a meal line
  would mean inventing a split. A Seazon order is its own item type rather
  than Amazon's shared `order_item`: it holds none of what an Amazon order
  line is made of (no department, no ASIN, no product condition), and keeping
  them apart is what lets the platform's purchase page offer its own
  dimensions instead of Amazon's.

* **The order's own price.** `prices.value` is what the export states as the
  order's value; `discount`, `shipping` and `addonPrice` sit next to it and
  are kept as their own fields rather than folded in, since the export never
  says which of them `value` already includes (`total_revenue` matches the
  sum of `value` minus the refunds, not minus the discounts).

* **When an order happened.** `created_at` is when the PLAN generated the
  slot, not when anything was paid or eaten: on the real export it runs a
  median 89 days ahead of the delivery, and falls in a different calendar
  month than the delivery for 35 of the 40 charged orders. Dating the items by
  it shifts every time-based reading ("what did I spend in June", "what did I
  eat in June") by a quarter, so a charged order is dated by its `charge_date`
  (when the money moved) and a meal by its `shipping_date` (the day the box
  arrived -- the actual delivery of `delivery_dates` is either that same day
  or up to three days later, and is absent on the slots never delivered).
  Both fall back to `created_at`, and a cancelled slot keeps its own
  cancellation date. The timestamp is part of an item's identity (app/dedup.py
  leaves only `fields` volatile for Seazon), so this makes previously imported
  batches need a re-analysis -- or a re-import -- to avoid the same meal
  counting twice.

Meal ids (`Meal` pointers) are only ever resolved through the meals already
ordered: `seazon_log.json`'s `meal_selections.mealsOrdered` is the only place
pairing a meal's id with its name, which is what names the taste preferences
(11 of 13 on the real export -- the two that stay unnamed reference dishes
never seen in a log).
"""
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.importers.base import BaseImporter, NormalizedItem
from app.importers.registry import register
from app.importers.utils import load_json

# Seazon delivers one flat file per domain; `foodcheri_user.json` is the one
# that kept the parent brand's name.
_DETECT_RE = re.compile(r"^seazon_[a-z_]+\.json$", re.IGNORECASE)
_TASTE_FILE = "foodcheri_user.json"

# Export timezone: Seazon writes naive local timestamps ("2026-10-13
# 10:46:00.123000"), shipping days as plain dates, and the order's state
# changes as Parse-style {"__type": "Date", "iso": "..."} pointers. The
# timestamps are kept as written -- an offset found on the ISO dates is
# dropped rather than converted, so every item of one order stays on the same
# wall-clock day the export shows.

_ORDER_STATES = {"shipped": "Livrée", "paid": "Payée", "active": "En préparation", "cancelled": "Annulée"}

# The cancellation motives the account page offers, in English in the export
# whatever the account's language (the log's own French sentence is kept
# as-is on the log item, this is for the structured field).
_CANCEL_MOTIVES = {
    "I do not want to give any reason": "Sans motif donné",
    "I don't need it now": "Pas besoin cette semaine",
    "In professional displacement": "En déplacement professionnel",
    "No dishes that suits me": "Aucun plat qui convient",
    "I have not finished eating my dishes": "Plats de la semaine précédente non terminés",
    "I'm not at home right now": "Absent au moment de la livraison",
    "In holiday": "En vacances",
    "Not available on delivery": "Indisponible à la livraison",
}
_CANCELLED_BY = {"user": "Le client", "cancelPlan": "Fin de l'abonnement"}

_PLAN_STATES = {"active": "Actif", "deleted": "Terminé"}

# Who cancelled a subscription (`cancel_motive_code`), as the account page
# words it.
_PLAN_CANCEL_MOTIVES = {
    "dishesNotFinished": "Plats non terminés",
    "onBusinessTrip": "En déplacement professionnel",
    "holidays": "En vacances",
}

_NOTIFICATION_CHANNELS = {"customer": "Client", "marketing": "Marketing"}
_NOTIFICATION_TRIGGERS = {
    "reminderMealsSelection": "Rappel de choisir ses plats",
    "shippingDayActivation": "Nouvelle carte de la semaine",
    "orderPayment": "Paiement de la commande",
    "customerCare": "Service client",
    "referralPromotion": "Parrainage",
}

_LOG_TYPES = {
    "order": "Commande", "shipping": "Livraison", "coupon": "Bon de réduction", "opinion": "Avis",
    "plan": "Abonnement", "refund": "Remboursement", "preference": "Préférences", "stripe": "Paiement",
    "user": "Compte", "payment": "Paiement",
}
_LOG_ACTIONS = {
    "add": "Ajout", "update": "Modification", "cancel": "Annulation", "active": "Activation",
    "download": "Téléchargement", "delete": "Suppression",
}

_COUPON_STATES = {"used": "Utilisé", "expired": "Expiré", "available": "Disponible"}
_TASTE_MOVES = {"added": "Ajouté au menu", "removed": "Retiré du menu", "kept": "Conservé au menu"}

_DAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_GENDERS = {"male": "Homme", "female": "Femme", "other": "Autre"}
_RELATIONSHIPS = {"couple": "En couple", "single": "Célibataire", "family": "En famille"}
_SPORT = {"regular": "Régulier", "occasional": "Occasionnel", "never": "Jamais"}

_TICKET_STATES = {"open": "Ouvert", "closed": "Fermé", "pending": "En attente"}
_COUPON_SERVICES = {"subscription": "Abonnement", "onetime": "Commande ponctuelle"}
# The export stores the payment provider's name, not the payment method.
_PAYMENTS = {"stripe": "Carte bancaire"}


def _hours(value: Optional[str]) -> Optional[str]:
    """"8-13" (how the export writes a delivery window) -> "8h - 13h"."""
    if not value or "-" not in value:
        return value
    start, _, end = value.partition("-")
    return f"{start.strip()}h - {end.strip()}h"


def _parse_dt(value: Any) -> Optional[datetime]:
    """A Seazon timestamp, however the file wrote it: "2026-10-13
    10:46:00.123000" (microseconds), "2023-06-30" (a shipping day), the ISO
    string of a state change, or the {"__type": "Date", "iso": ...} wrapper
    the order's `state_change_events` use. Unparseable values are treated as
    absent, never as "now"."""
    if isinstance(value, dict):
        value = value.get("iso")
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%y", "%d/%m/%Y"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None
    return parsed.replace(tzinfo=None)


def _date_fr(value: Any) -> Optional[str]:
    parsed = _parse_dt(value)
    return parsed.strftime("%d/%m/%Y") if parsed else None


def _rows(path: Path) -> List[Dict[str, Any]]:
    """Every dict record of one export file, whatever envelope it uses --
    most are a bare list, `seazon_user.json` is a single object."""
    if not path.is_file():
        return []
    try:
        data = load_json(path)
    except Exception:
        return []
    if isinstance(data, dict):
        return [data]
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _one(path: Path) -> Dict[str, Any]:
    rows = _rows(path)
    return rows[0] if rows else {}


def _text(value: Any) -> Optional[str]:
    return value.strip() or None if isinstance(value, str) else None


def _amount(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def _euros(value: Optional[float]) -> Optional[str]:
    return None if value is None else f"{value:.2f} EUR"


def _meal_lines(entries: Any) -> List[Dict[str, Any]]:
    """The `meals`/`initial_meals` list of an order: one entry per dish, each
    naming itself under "name" or "text" depending on the export vintage."""
    lines = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        meal = entry.get("meal") if isinstance(entry.get("meal"), dict) else {}
        name = _text(meal.get("name")) or _text(meal.get("text")) or _text(entry.get("text"))
        if not name:
            continue
        count = entry.get("count")
        lines.append({
            "name": name,
            "type": _text(meal.get("type")),
            "count": count if isinstance(count, int) and count > 0 else 1,
            "addon": _amount(entry.get("addonPrice")),
            "raw": entry,
        })
    return lines


def _meal_list(lines: List[Dict[str, Any]]) -> str:
    return " · ".join(f"{line['name']} ×{line['count']}" if line["count"] > 1 else line["name"] for line in lines)


def _meal_summary(lines: List[Dict[str, Any]]) -> Optional[str]:
    """The heading a meal order shows in a list: the first few dishes by
    name, then how many more there are -- the full list is a field, and the
    dishes are browsable as their own items."""
    if not lines:
        return None
    names = [line["name"] for line in lines]
    if len(names) <= 3:
        return ", ".join(names)
    rest = len(names) - 3
    return f"{', '.join(names[:3])} et {rest} autre" + ("s" if rest > 1 else "")


def _meal_names_by_id(logs: List[Dict[str, Any]]) -> Dict[str, str]:
    """`{meal id: dish name}` -- ids otherwise appear only as pointers."""
    names: Dict[str, str] = {}
    for entry in logs:
        selections = entry.get("meal_selections") if isinstance(entry.get("meal_selections"), dict) else {}
        for meal in selections.get("mealsOrdered") or []:
            if isinstance(meal, dict) and meal.get("id") and _text(meal.get("text")):
                names[meal["id"]] = _text(meal["text"])
    return names


def _address_by_id(user: Dict[str, Any]) -> Dict[str, str]:
    """`{address id: "12 Impasse des Chardonnerets, 64320 IDRON"}` -- orders
    only carry the id of the address they were shipped to."""
    addresses = {}
    for address in user.get("addresses") or []:
        if not isinstance(address, dict) or not address.get("id"):
            continue
        # Street first, then the flat/door detail, then "zip city" -- the
        # order an envelope reads in (the export lists additionalAddress
        # first).
        parts = [address.get("address"), address.get("additionalAddress")]
        label = ", ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
        tail = " ".join(part for part in (address.get("zipCode"), address.get("city"))
                        if isinstance(part, str) and part.strip())
        label = f"{label}, {tail}".strip(", ") if tail else label
        if label:
            addresses[address["id"]] = label
    return addresses


@register
class SeazonImporter(BaseImporter):
    key = "seazon"
    label = "Seazon"
    type_labels = {
        "seazon_order": "Commande",
        "seazon_refund": "Remboursement",
        "order_cancelled": "Créneau annulé",
        "meal": "Plat commandé",
        "opinion": "Avis de livraison",
        "subscription_plan": "Abonnement",
        "account_log": "Événement de compte",
        "coupon": "Bon de réduction",
        "support_ticket": "Ticket de support",
        "taste_preference": "Préférence de plat",
    }

    # Filled by parse() before anything is normalized, so a normalizer called
    # directly (in a test) still finds them.
    _meal_names: Dict[str, str] = {}
    _addresses: Dict[str, str] = {}

    def detect(self, root: Path) -> bool:
        # One `seazon_*.json` is enough: the export pages let you pick which
        # domains to include, so a smaller export holds a subset of them.
        return any(_DETECT_RE.match(path.name) for path in root.rglob("*") if path.is_file())

    def parse(self, root: Path) -> Iterator[NormalizedItem]:
        logs = _rows(root / "seazon_log.json")
        user = _one(root / "seazon_user.json")
        taste = _one(root / _TASTE_FILE)
        self._meal_names = _meal_names_by_id(logs)
        self._addresses = _address_by_id(user)

        yield from self._parse_orders(_rows(root / "seazon_order.json"))
        yield from self._parse_opinions(_rows(root / "seazon_opinion.json"))
        yield from self._parse_plans(_rows(root / "seazon_plan.json"))
        yield from self._parse_notifications(_rows(root / "seazon_notification.json"))
        yield from self._parse_logs(logs)
        yield from self._parse_sessions(_rows(root / "seazon_session.json"))
        yield from self._parse_tickets(_rows(root / "seazon_gorgias_ticket.json"))
        yield from self._parse_coupons(user)
        yield from self._parse_taste(taste)
        yield from self._parse_profile(user, taste)

    # -- Orders (charged and cancelled slots) --------------------------------

    def _parse_orders(self, orders: List[Dict[str, Any]]) -> Iterator[NormalizedItem]:
        for order in orders:
            try:
                charged = isinstance(order.get("prices"), dict)
                state = (order.get("state") or "").lower()
                if not charged and state != "cancelled":
                    # A slot of the current plan: nothing chosen, nothing
                    # paid, and its selection can still change -- it will be
                    # reimported as an order (or a cancellation) once final.
                    continue
                meals = _meal_lines(order.get("meals"))
                if charged:
                    yield self._order_item(order, meals)
                else:
                    yield self._order_cancelled(order, meals)
                yield from self._meal_items(order, meals)
                yield from self._refunds(order)
            except Exception:
                continue

    def _order_item(self, order: Dict[str, Any], meals: List[Dict[str, Any]]) -> NormalizedItem:
        prices = order.get("prices") or {}
        state = (order.get("state") or "").lower()
        value = _amount(prices.get("value"))
        fields: Dict[str, Any] = {"Catégorie": "Repas"}
        if value is not None:
            fields["Montant"] = _euros(value)
        if _amount(prices.get("discount")):
            fields["Remise"] = _euros(_amount(prices.get("discount")))
        if _amount(prices.get("shipping")):
            fields["Livraison"] = _euros(_amount(prices.get("shipping")))
        if _amount(prices.get("addonPrice")):
            fields["Suppléments"] = _euros(_amount(prices.get("addonPrice")))
        fields["Statut"] = _ORDER_STATES.get(state, state.capitalize() or "Inconnu")
        shipping_date = _date_fr(order.get("shipping_date"))
        if shipping_date:
            fields["Livraison prévue"] = shipping_date
        if isinstance(order.get("count"), int):
            fields["Nombre de repas"] = str(order["count"])
        if meals:
            fields["Repas"] = _meal_list(meals)
            initial = _meal_lines(order.get("initial_meals"))
            if initial and _meal_list(initial) != _meal_list(meals):
                fields["Menu initial"] = _meal_list(initial)
        for label, key in (("Adresse de livraison", "shipping_address"), ("Adresse de facturation", "billing_address")):
            address = self._addresses.get(order.get(key) or "")
            if address:
                fields[label] = address
        payment = _text(order.get("payment_type"))
        if payment:
            fields["Mode de paiement"] = _PAYMENTS.get(payment, payment)
        if _text(order.get("invoice_id")):
            fields["Facture"] = order["invoice_id"]
        parcels = order.get("skybill_numbers") or []
        if parcels:
            fields["Colis"] = ", ".join(str(number) for number in parcels)
        deliveries = order.get("delivery_dates") or []
        delivered = _date_fr(deliveries[0].get("date")) if deliveries and isinstance(deliveries[0], dict) else None
        if delivered:
            fields["Livrée le"] = delivered
        if order.get("loyalty_points"):
            fields["Points de fidélité"] = str(order["loyalty_points"])
        # A charged order can still be cancelled afterwards (and refunded):
        # what it was charged keeps counting as spend, the cancellation is
        # told apart by its own fields rather than by dropping the order.
        # `cancelled_date` is when the cancellation took effect (right after
        # the refund), `canceling_date` when it was asked for -- the three
        # charged-then-cancelled orders of the real export have either one.
        cancelled = _date_fr(order.get("cancelled_date") or order.get("canceling_date"))
        if cancelled:
            fields["Annulée le"] = cancelled
        if _text(order.get("cancelled_by")):
            fields["Annulé par"] = _CANCELLED_BY.get(order["cancelled_by"], order["cancelled_by"])
        if _text(order.get("user_motive")):
            fields["Motif d'annulation"] = _CANCEL_MOTIVES.get(order["user_motive"], order["user_motive"])
        refunded = sum(
            _amount(operation.get("refundedAmount")) or 0.0
            for operation in order.get("operations") or [] if isinstance(operation, dict)
        )
        if refunded:
            fields["Remboursé"] = _euros(round(refunded, 2))

        return NormalizedItem(
            item_type="seazon_order",
            category="Commandes",
            # When the money moved, not when the plan opened the slot: see the
            # module docstring.
            timestamp=(_parse_dt(order.get("charge_date")) or _parse_dt(order.get("shipping_date"))
                       or _parse_dt(order.get("created_at"))),
            content=_meal_summary(meals),
            context=_text(order.get("order_id")),  # the whole order: every item shares it
            fields=fields or None,
            raw=order,
        )

    def _refunds(self, order: Dict[str, Any]) -> Iterator[NormalizedItem]:
        """Money given back on a charged order (`operations`), as items of
        their own rather than only a total on the order: the purchases page
        subtracts them from the spend when asked to, so what it calls "net"
        is the same figure Seazon's own profile does (`total_revenue` is the
        charged total minus exactly these). Partial refunds are separate
        operations on one order, hence one item each."""
        order_id = _text(order.get("order_id"))
        for operation in order.get("operations") or []:
            if not isinstance(operation, dict) or operation.get("type") != "refund":
                continue
            amount = _amount(operation.get("refundedAmount"))
            if not amount:
                continue
            fields: Dict[str, Any] = {"Montant remboursé": _euros(round(amount, 2))}
            if operation.get("isPartial"):
                fields["Remboursement partiel"] = "Oui"
            if _text(operation.get("chargeType")):
                fields["Moyen"] = _PAYMENTS.get(operation["chargeType"], operation["chargeType"])
            if operation.get("appliedOnTotalRevenue"):
                fields["Déduit du chiffre d'affaires"] = "Oui"
            yield NormalizedItem(
                item_type="seazon_refund",
                category="Remboursements",
                timestamp=_parse_dt(operation.get("date")) or _parse_dt(order.get("cancelled_date")),
                title=f"Remboursement de {amount:.2f} €",
                content=f"Commande {order_id}" if order_id else None,
                context=order_id,
                fields=fields,
                raw=operation,
            )

    def _order_cancelled(self, order: Dict[str, Any], meals: List[Dict[str, Any]]) -> NormalizedItem:
        """A shipping slot cancelled before anything was charged -- 132 of the
        180 the real export holds, most of them with the meals the customer
        had already picked."""
        shipping_date = _date_fr(order.get("shipping_date"))
        fields: Dict[str, Any] = {}
        if _text(order.get("user_motive")):
            fields["Motif d'annulation"] = _CANCEL_MOTIVES.get(order["user_motive"], order["user_motive"])
        if _text(order.get("cancelled_by")):
            fields["Annulé par"] = _CANCELLED_BY.get(order["cancelled_by"], order["cancelled_by"])
        if _text(order.get("cancel_code")) or _text(order.get("cancel_motive")) or _text(order.get("auto_cancel_motive")):
            fields["Code d'annulation"] = _text(order.get("cancel_code")) or _text(order.get("auto_cancel_motive")) or order["cancel_motive"]
        if shipping_date:
            fields["Livraison prévue"] = shipping_date
        if meals:
            fields["Nombre de repas choisis"] = str(len(meals))
            fields["Repas"] = _meal_list(meals)

        return NormalizedItem(
            item_type="order_cancelled",
            category="Commandes",
            timestamp=_parse_dt(order.get("cancelled_date") or order.get("canceling_date")) or _parse_dt(order.get("created_at")),
            title=f"Créneau du {shipping_date} annulé" if shipping_date else "Créneau annulé",
            content=_meal_summary(meals),
            context=_text(order.get("order_id")),
            fields=fields or None,
            raw=order,
        )

    def _meal_items(self, order: Dict[str, Any], meals: List[Dict[str, Any]]) -> Iterator[NormalizedItem]:
        """Every dish of one slot, as its own item: the export prices the
        order as a whole and never per dish, so these carry no amount and
        stay out of the purchase totals. Dated by the day the box was due to
        arrive, when the dish was eaten -- see the module docstring."""
        state = (order.get("state") or "").lower()
        order_id = _text(order.get("order_id"))
        shipping_date = _date_fr(order.get("shipping_date"))
        for line in meals:
            fields: Dict[str, Any] = {"Commande": _ORDER_STATES.get(state, state.capitalize() or "Inconnue")}
            if line["count"] > 1:
                fields["Quantité"] = str(line["count"])
            if line["type"]:
                fields["Type de plat"] = line["type"].capitalize()
            if line["addon"]:
                fields["Supplément"] = _euros(line["addon"])
            if shipping_date:
                fields["Livraison prévue"] = shipping_date
            yield NormalizedItem(
                item_type="meal",
                category="Repas",
                timestamp=_parse_dt(order.get("shipping_date")) or _parse_dt(order.get("created_at")),
                title=line["name"],
                content=None,
                context=order_id,
                fields=fields,
                raw=line["raw"],  # the order's own "meals" entry, verbatim
            )

    # -- Ratings, subscriptions, notifications, audit trail, logins ----------

    def _parse_opinions(self, opinions: List[Dict[str, Any]]) -> Iterator[NormalizedItem]:
        for opinion in opinions:
            try:
                stars = opinion.get("stars") if isinstance(opinion.get("stars"), dict) else {}
                fields: Dict[str, Any] = {}
                for key, label in (("delivery", "Livraison"), ("packaging", "Emballage"), ("courier", "Livreur")):
                    if isinstance(stars.get(key), int):
                        fields[label] = f"{stars[key]}/5"
                rated = []
                for entry in opinion.get("meals") or []:
                    if not isinstance(entry, dict) or not isinstance(entry.get("stars"), dict):
                        continue
                    meal = entry.get("meal") if isinstance(entry.get("meal"), dict) else {}
                    name = self._meal_names.get(meal.get("objectId") or "") or meal.get("objectId")
                    notes = "/".join(
                        f"{label} {entry['stars'][key]}" for key, label in
                        (("quality", "qualité"), ("quantity", "quantité"), ("taste", "goût"))
                        if isinstance(entry["stars"].get(key), int)
                    )
                    if name and notes:
                        rated.append(f"{name} ({notes})")
                if rated:
                    fields["Plats notés"] = " · ".join(rated)
                if opinion.get("contact_request"):
                    fields["Recontact demandé"] = "Oui"
                order_id = _text(opinion.get("order_id"))
                yield NormalizedItem(
                    item_type="opinion",
                    category="Avis",
                    timestamp=_parse_dt(opinion.get("date")) or _parse_dt(opinion.get("created_at")),
                    title=f"Avis sur la commande du {_date_fr(opinion.get('date'))}" if opinion.get("date") else "Avis",
                    content=f"Commande {order_id}" if order_id else None,
                    context=order_id,
                    fields=fields or None,
                    raw=opinion,
                )
            except Exception:
                continue

    def _parse_plans(self, plans: List[Dict[str, Any]]) -> Iterator[NormalizedItem]:
        for plan in plans:
            try:
                day = plan.get("day_of_week")
                day_label = _DAYS_FR[day - 1] if isinstance(day, int) and 1 <= day <= 7 else None
                fields: Dict[str, Any] = {}
                if day_label:
                    fields["Jour de livraison"] = day_label.capitalize()
                if _text(plan.get("shipping_hours")):
                    fields["Créneau"] = _hours(plan["shipping_hours"])
                fields["Statut"] = _PLAN_STATES.get(plan.get("state") or "", plan.get("state") or "Inconnu")
                for label, key in (("Annulé le", "canceling_date"), ("Réactivé le", "reactivation_date"),
                                   ("Terminé le", "change_over_date")):
                    if _text(plan.get(key)):
                        fields[label] = _date_fr(plan[key]) or str(plan[key])
                if _text(plan.get("cancel_motive_code")):
                    fields["Motif de fin"] = _PLAN_CANCEL_MOTIVES.get(plan["cancel_motive_code"], plan["cancel_motive_code"])
                if plan.get("changed_from_bo"):
                    fields["Modifié par le service client"] = "Oui"
                if plan.get("first"):
                    fields["Premier abonnement"] = "Oui"
                if plan.get("is_current_plan"):
                    fields["Abonnement en cours"] = "Oui"
                yield NormalizedItem(
                    item_type="subscription_plan",
                    category="Abonnements",
                    timestamp=_parse_dt(plan.get("created_at")),
                    title=f"Abonnement du {day_label}" if day_label else "Abonnement",
                    content=f"Livraison le {day_label} ({_hours(_text(plan.get('shipping_hours')))})"
                    if day_label and _text(plan.get("shipping_hours")) else None,
                    context=_text(plan.get("plan_id")),
                    fields=fields or None,
                    raw=plan,
                )
            except Exception:
                continue

    def _parse_notifications(self, notifications: List[Dict[str, Any]]) -> Iterator[NormalizedItem]:
        for notification in notifications:
            try:
                fields: Dict[str, Any] = {}
                if _text(notification.get("trigger")):
                    trigger = notification["trigger"]
                    fields["Motif"] = _NOTIFICATION_TRIGGERS.get(trigger, trigger)
                if _text(notification.get("type")):
                    fields["Destinataire"] = _NOTIFICATION_CHANNELS.get(notification["type"], notification["type"])
                if notification.get("checked"):
                    fields["Lue"] = "Oui"
                    if _text(notification.get("reading_date")):
                        fields["Lue le"] = notification["reading_date"].split(".")[0]
                if _text(notification.get("url")):
                    fields["Lien"] = notification["url"]
                yield NormalizedItem(
                    item_type="notification",
                    category="Notifications",
                    timestamp=_parse_dt(notification.get("date")) or _parse_dt(notification.get("created_at")),
                    title=_text(notification.get("title")),
                    content=_text(notification.get("description")),
                    context=_text(notification.get("trigger")),
                    fields=fields or None,
                    raw=notification,
                )
            except Exception:
                continue

    def _parse_logs(self, logs: List[Dict[str, Any]]) -> Iterator[NormalizedItem]:
        for entry in logs:
            try:
                fields: Dict[str, Any] = {}
                if _text(entry.get("type")):
                    fields["Type"] = _LOG_TYPES.get(entry["type"], entry["type"])
                if _text(entry.get("action")):
                    fields["Action"] = _LOG_ACTIONS.get(entry["action"], entry["action"])
                if _text(entry.get("user_motive")):
                    fields["Motif"] = _CANCEL_MOTIVES.get(entry["user_motive"], entry["user_motive"])
                if _text(entry.get("shipping_date")):
                    fields["Livraison"] = entry["shipping_date"]
                selections = entry.get("meal_selections") if isinstance(entry.get("meal_selections"), dict) else {}
                if selections.get("mealsOrdered"):
                    fields["Plats choisis"] = " · ".join(
                        f"{_text(meal.get('text'))} ×{meal['count']}" if meal.get("count", 1) > 1 else str(_text(meal.get("text")))
                        for meal in selections["mealsOrdered"] if isinstance(meal, dict) and _text(meal.get("text"))
                    )
                if selections.get("mealsChanged"):
                    fields["Plats modifiés"] = " · ".join(
                        f"{_text(meal.get('text'))} ({meal.get('count', 1)})" for meal in selections["mealsChanged"]
                        if isinstance(meal, dict) and _text(meal.get("text"))
                    )
                if _amount(entry.get("refunded_amount")):
                    fields["Remboursé"] = _euros(_amount(entry["refunded_amount"]))
                yield NormalizedItem(
                    item_type="account_log",
                    category="Historique",
                    timestamp=_parse_dt(entry.get("created_at")),
                    content=_text(entry.get("description")),
                    context=_text(entry.get("order_id")) or _text(entry.get("plan_id")),
                    fields=fields or None,
                    raw=entry,
                )
            except Exception:
                continue

    def _parse_sessions(self, sessions: List[Dict[str, Any]]) -> Iterator[NormalizedItem]:
        for session in sessions:
            try:
                created_with = session.get("created_with") if isinstance(session.get("created_with"), dict) else {}
                action = _text(created_with.get("action")) or "login"
                provider = _text(created_with.get("authProvider"))
                fields: Dict[str, Any] = {}
                if provider:
                    fields["Méthode"] = "Mot de passe" if provider == "password" else provider
                if _text(session.get("expires_at")):
                    fields["Expire le"] = session["expires_at"].split(".")[0]
                yield NormalizedItem(
                    item_type="login",
                    category="Connexions",
                    timestamp=_parse_dt(session.get("created_at")),
                    title="Inscription" if action == "signup" else "Connexion",
                    context=_text(session.get("session_id")),
                    fields=fields or None,
                    raw=session,
                )
            except Exception:
                continue

    def _parse_tickets(self, tickets: List[Dict[str, Any]]) -> Iterator[NormalizedItem]:
        for ticket in tickets:
            try:
                fields: Dict[str, Any] = {}
                if _text(ticket.get("state")):
                    fields["Statut"] = _TICKET_STATES.get(ticket["state"], ticket["state"])
                if ticket.get("tags"):
                    fields["Étiquettes"] = ", ".join(str(tag) for tag in ticket["tags"])
                if ticket.get("ticket_id"):
                    fields["Numéro"] = str(ticket["ticket_id"])
                yield NormalizedItem(
                    item_type="support_ticket",
                    category="Support",
                    timestamp=_parse_dt(ticket.get("datetime")) or _parse_dt(ticket.get("created_at")),
                    title=_text(ticket.get("subject")),
                    context=_text(ticket.get("gorgias_ticket_id")),
                    fields=fields or None,
                    raw=ticket,
                )
            except Exception:
                continue

    # -- Account: coupons, meal preferences, profile -------------------------

    def _parse_coupons(self, user: Dict[str, Any]) -> Iterator[NormalizedItem]:
        for coupon in user.get("coupons") or []:
            try:
                if not isinstance(coupon, dict):
                    continue
                uses = [use for use in coupon.get("useOfDiscounts") or [] if isinstance(use, dict)]
                fields: Dict[str, Any] = {}
                if _text(coupon.get("state")):
                    fields["Statut"] = _COUPON_STATES.get(coupon["state"], coupon["state"])
                if _text(coupon.get("service")):
                    fields["Service"] = _COUPON_SERVICES.get(coupon["service"], coupon["service"])
                if coupon.get("code"):
                    fields["Code"] = str(coupon["code"])
                used_on = None
                for use in uses:
                    used_on = _date_fr(use.get("paymentDate")) or used_on
                    if _amount(use.get("usedDiscountValue")):
                        fields["Montant utilisé"] = _euros(_amount(use["usedDiscountValue"]))
                    if _text(use.get("orderId")):
                        fields["Commande"] = use["orderId"]
                if used_on:
                    fields["Utilisé le"] = used_on
                content = None
                if uses:
                    content = f"Utilisé{' le ' + used_on if used_on else ''}"
                elif coupon.get("state") == "expired":
                    content = "Expiré sans utilisation"
                yield NormalizedItem(
                    item_type="coupon",
                    category="Bons de réduction",
                    timestamp=_parse_dt(coupon.get("date")),
                    title=str(coupon.get("code")) if coupon.get("code") else "Bon de réduction",
                    content=content,
                    context=_text(coupon.get("id")),
                    fields=fields or None,
                    raw=coupon,
                )
            except Exception:
                continue

    def _parse_taste(self, taste: Dict[str, Any]) -> Iterator[NormalizedItem]:
        """The dish-taste marks of `foodcheri_user.json`: each entry points at
        a meal by its internal id, named through the ones the customer
        actually ordered (see the module docstring). They carry no date at
        all, so these items are undated."""
        for entry in taste.get("taste") or []:
            try:
                if not isinstance(entry, dict):
                    continue
                meal = entry.get("meal") if isinstance(entry.get("meal"), dict) else {}
                meal_id = _text(meal.get("objectId"))
                if not meal_id:
                    continue
                moves = {key: entry[key] for key in _TASTE_MOVES if isinstance(entry.get(key), int) and entry[key] > 0}
                fields: Dict[str, Any] = {"Identifiant du plat": meal_id}
                for key, label in _TASTE_MOVES.items():
                    fields[label] = str(entry.get(key, 0))
                content = " · ".join(f"{_TASTE_MOVES[key]} {value} fois" for key, value in moves.items()) or None
                yield NormalizedItem(
                    item_type="taste_preference",
                    category="Préférences",
                    timestamp=None,
                    title=self._meal_names.get(meal_id) or f"Plat {meal_id}",
                    content=content,
                    fields=fields,
                    raw=entry,
                )
            except Exception:
                continue

    def _parse_profile(self, user: Dict[str, Any], taste: Dict[str, Any]) -> Iterator[NormalizedItem]:
        if not user:
            return
        try:
            info = user.get("personal_info") if isinstance(user.get("personal_info"), dict) else {}
            notifications = user.get("news_notifications") if isinstance(user.get("news_notifications"), dict) else {}
            fields: Dict[str, Any] = {}
            for label, value in (
                ("E-mail", _text(user.get("email")) or _text(user.get("username"))),
                ("Téléphone", _text(user.get("phone_number"))),
                ("Date de naissance", _date_fr(info.get("birthDate"))),
                ("Genre", _GENDERS.get(info.get("genre"), _text(info.get("genre")))),
                ("Situation", _RELATIONSHIPS.get(info.get("relationship"), _text(info.get("relationship")))),
                ("Sport", _SPORT.get(info.get("sportFrequency"), _text(info.get("sportFrequency")))),
                ("Code postal", _text(user.get("zip_code"))),
                ("Mode de paiement", _PAYMENTS.get(_text(user.get("payment_type")), _text(user.get("payment_type")))),
            ):
                if value:
                    fields[label] = value
            if user.get("created_at"):
                _created = _date_fr(user["created_at"])
                if _created:
                    fields["Compte créé le"] = _created
            if _text(user.get("last_order_date")):
                fields["Dernière commande"] = _date_fr(user["last_order_date"]) or user["last_order_date"]
            if _text(user.get("first_order_id")):
                fields["Première commande"] = user["first_order_id"]
            if isinstance(user.get("order_count"), int):
                fields["Commandes facturées"] = str(user["order_count"])
            if _amount(user.get("total_revenue")) is not None:
                fields["Total dépensé (selon Seazon)"] = _euros(_amount(user["total_revenue"]))
            if _amount(user.get("scoring_churn")) is not None:
                fields["Score d'attrition"] = f"{_amount(user['scoring_churn']):.1f}"
            if isinstance(user.get("loyalty_balance"), int):
                fields["Points de fidélité"] = str(user["loyalty_balance"])
            for key, label in (("mail", "Notifications e-mail"), ("sms", "Notifications SMS"),
                               ("postalMail", "Notifications courrier")):
                setting = notifications.get(key) if isinstance(notifications.get(key), dict) else None
                if setting is not None and "value" in setting:
                    fields[label] = "Oui" if setting["value"] else "Non"
            addresses = _address_by_id(user)
            if addresses:
                fields["Adresses"] = " · ".join(sorted(addresses.values()))
            if isinstance(user.get("filter_by_preferences"), bool):
                fields["Filtrer selon ses préférences"] = "Oui" if user["filter_by_preferences"] else "Non"
            if _text(user.get("anti_churn_accepted_date")):
                fields["Offre anti-attrition acceptée le"] = user["anti_churn_accepted_date"].split(".")[0]
            excluded = ((user.get("preferences") or {}).get("excludedIngredients") or {}) if isinstance(user.get("preferences"), dict) else {}
            if isinstance(excluded, dict) and excluded.get("isActive") is not None:
                fields["Ingrédients exclus actifs"] = "Oui" if excluded.get("isActive") else "Non"
            if _amount(taste.get("scoring_churn")) is not None and "Score d'attrition" not in fields:
                fields["Score d'attrition"] = f"{_amount(taste['scoring_churn']):.1f}"

            name = " ".join(part for part in (_text(user.get("first_name")), _text(user.get("last_name"))) if part)
            yield NormalizedItem(
                item_type="profile",
                category="Compte",
                timestamp=_parse_dt(user.get("created_at")),
                title=name or _text(user.get("username")),
                content="Compte Seazon",
                context=_text(user.get("user_id")),
                fields=fields or None,
                raw=user,
            )
        except Exception:
            return
