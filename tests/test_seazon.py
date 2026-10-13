import json
import sys
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import anonymize, dedup, importers, platforms, stats  # noqa: E402
from app.importers import seazon  # noqa: E402

# A made-up export in the shapes of a real one: a flat zip of seazon_*.json
# files, plus FoodCheri's own taste file. Two of the four shipping slots are
# charged (one shipped, one cancelled after being charged and refunded), one
# is cancelled before any payment with the meals already picked, and one is
# the plan's current slot -- nothing chosen, nothing paid.
USER = {
    "user_id": "u1", "username": "owner@example.com", "email": "owner@example.com",
    "first_name": "Camille", "last_name": "Durand", "zip_code": "64320",
    "created_at": "2023-06-09 20:39:30.087000", "last_order_date": "2025-02-10",
    "first_order_id": "o1", "order_count": 2, "total_revenue": 157.8, "scoring_churn": 64.5,
    "loyalty_balance": 4370, "payment_type": "stripe", "anti_churn_accepted_date": "2024-10-07 00:30:50.418000",
    "personal_info": {"genre": "female", "birthDate": "1994-10-27", "relationship": "couple", "sportFrequency": "regular"},
    "preferences": {"excludedIngredients": {"isActive": True, "value": []}},
    "news_notifications": {
        "mail": {"value": True, "date": {"__type": "Date", "iso": "2023-06-09T20:39:30.084Z"}},
        "sms": {"value": False, "date": {"__type": "Date", "iso": "2023-06-09T20:39:30.084Z"}},
    },
    "addresses": [
        {"id": "a1", "address": "12 Impasse des Chardonnerets", "city": "IDRON", "zipCode": "64320",
         "additionalAddress": "", "addressName": "Maison"},
    ],
    "shipping_address": "a1", "billing_address": "a1",
    "coupons": [
        {"id": "c1", "date": "2023-06-11", "state": "used", "service": "subscription",
         "useOfDiscounts": [{"paymentDate": "2023-06-25", "orderId": "o1", "usedDiscountValue": 20,
                            "orderShippingDate": "2023-06-30"}]},
        {"id": "c2", "date": "2023-09-28", "state": "expired", "service": "subscription", "useOfDiscounts": []},
    ],
}

MEALS = [
    {"meal": {"text": "Chili sin carne", "type": "plat"}, "count": 2, "addonPrice": 0},
    {"meal": {"text": "Poulet korma", "type": "plat"}, "count": 1, "addonPrice": 1.5},
]
INITIAL_MEALS = [
    {"meal": {"text": "Chili sin carne", "type": "plat"}, "count": 2},
    {"meal": {"text": "Orge crémeux au Sainte Maure", "type": "plat"}, "count": 1},
]

ORDERS = [
    {   # charged, shipped
        "order_id": "o1", "plan_id": "p1", "shipping_date": "2023-06-30", "state": "shipped",
        "count": 12, "user_id": "u1", "created_at": "2023-06-11 09:16:32.601000",
        "meals": MEALS, "initial_meals": INITIAL_MEALS, "meals_by_user_selection": True,
        "shipping_address": "a1", "billing_address": "a1", "payment_type": "stripe",
        "charge_date": "2023-06-25 00:15:49.344000", "invoice_id": "2023-06-99631",
        "prices": {"value": 72.9, "discount": 20, "shipping": 0, "grant": 0, "addonPrice": 1.5},
        "skybill_numbers": ["XU278428588VF"],
        "delivery_dates": [{"skybillNumber": "XU278428588VF", "date": "2023-06-30T10:39:52+02:00"}],
        "loyalty_points": 100,
        "used_coupons": [{"coupon": {"__type": "Pointer", "className": "Coupon", "objectId": "c1"},
                          "discountValue": 20, "discountType": "currency"}],
        "opinion_id": "v1",
        "state_change_events": [
            {"state": "paid", "changeDate": {"__type": "Date", "iso": "2023-06-25T00:15:49.213Z"}},
            {"state": "shipped", "changeDate": {"__type": "Date", "iso": "2023-06-30T10:40:34.201Z"}},
        ],
    },
    {   # charged, then cancelled and refunded
        "order_id": "o2", "plan_id": "p1", "shipping_date": "2025-02-14", "state": "cancelled",
        "count": 10, "user_id": "u1", "created_at": "2025-01-10 09:00:00.000000",
        "meals": [{"meal": {"text": "Saumon, crema verde & verduras", "type": "plat"}, "count": 4, "addonPrice": 0}],
        "prices": {"value": 84.9, "discount": 0, "shipping": 0, "grant": 0, "addonPrice": 0},
        "invoice_id": "2025-02-82427", "charge_date": "2025-02-23 01:20:21.998000",
        "canceling_date": "2025-02-20 10:15:15.615000",
        "cancelled_date": "2025-02-23 07:39:31.432000", "cancelled_by": "user",
        "user_motive": "I do not want to give any reason", "refunded": True,
        "operations": [{"type": "refund", "value": 84.9, "refundedAmount": 84.9,
                        "date": {"__type": "Date", "iso": "2025-02-23T07:39:29.636Z"}}],
    },
    {   # cancelled before any payment, meals already picked
        "order_id": "o3", "plan_id": "p1", "shipping_date": "2023-07-07", "state": "cancelled",
        "count": 12, "user_id": "u1", "created_at": "2023-06-11 09:16:33.457000",
        "canceling_date": "2023-06-11 09:17:32.411000", "cancelled_by": "user",
        "user_motive": "In professional displacement",
        "meals": [{"meal": {"text": "Poulet korma", "type": "plat"}, "count": 1, "addonPrice": 0}],
    },
    {   # the plan's current slot: not an order yet
        "order_id": "o4", "plan_id": "p1", "shipping_date": "2026-10-16", "state": "active",
        "count": 10, "user_id": "u1", "created_at": "2026-10-10 23:40:57.329000",
    },
]

# `meal_selections.mealsOrdered` is the only place pairing a meal's id with
# its name -- what names the taste file's pointers ("m2" is nowhere).
LOGS = [
    {"log_id": "l1", "type": "shipping", "action": "update", "user_id": "u1", "order_id": "o1",
     "description": "a modifié ses plats pour sa commande du 30/06/23.",
     "created_at": "2023-06-11 09:16:35.831000",
     "meal_selections": {"mealsOrdered": [{"id": "m1", "text": "Chili sin carne", "count": 2}],
                         "mealsChanged": [{"id": "m1", "text": "Chili sin carne", "count": 2}]}},
    {"log_id": "l2", "type": "order", "action": "cancel", "user_id": "u1", "user_motive": "No dishes that suits me",
     "shipping_date": "07/07/23", "order_id": "o3", "refunded_amount": 12.5,
     "description": "a suspendu la commande 07/07/23. Motif: Pas de plats qui me correspondent",
     "created_at": "2023-06-11 09:17:32.500000"},
]

FILES = {
    "seazon_user.json": json.dumps(USER, ensure_ascii=False),
    "seazon_order.json": json.dumps(ORDERS, ensure_ascii=False),
    "seazon_log.json": json.dumps(LOGS, ensure_ascii=False),
    "seazon_plan.json": json.dumps([
        {"plan_id": "p1", "user_id": "u1", "day_of_week": 5, "shipping_hours": "8-13", "state": "active",
         "is_current_plan": True, "created_at": "2025-03-03 16:04:45.993000"},
        {"plan_id": "p0", "user_id": "u1", "day_of_week": 2, "shipping_hours": "8-13", "state": "deleted",
         "created_at": "2023-06-11 09:16:31.484000", "first": True,
         "cancel_motive_code": "dishesNotFinished", "canceling_date": "2023-10-05 14:57:30.387000",
         "reactivation_date": "2024-03-03 16:32:22.163000"},
    ], ensure_ascii=False),
    "seazon_opinion.json": json.dumps([
        {"opinion_id": "v1", "stars": {"delivery": 5, "packaging": 5, "courier": 5}, "date": "2023-07-02",
         "user_id": "u1", "order_id": "o1", "created_at": "2023-07-02 10:38:22.291000",
         "meals": [{"stars": {"quality": 4, "quantity": 5, "taste": 4},
                    "meal": {"__type": "Pointer", "className": "Meal", "objectId": "m1"}}]},
    ], ensure_ascii=False),
    "seazon_notification.json": json.dumps([
        {"notification_id": "n1", "date": "2023-06-15 13:43:02.311000", "type": "customer",
         "trigger": "shippingDayActivation", "title": "Nouvelle carte : vendredi 07 juillet !",
         "description": "Pensez à personnaliser votre commande.", "url": "https://seazon.fr/mes-commandes/commande-o1",
         "checked": True, "reading_date": "2023-06-28 17:56:01.930000"},
        {"notification_id": "n2", "date": "2024-01-05 08:00:00.000000", "type": "marketing",
         "trigger": "referralPromotion", "title": "Parrainez un ami", "description": "10 € offerts",
         "marketing_notification_id": "m1"},
    ], ensure_ascii=False),
    "seazon_session.json": json.dumps([
        {"session_id": "s1", "user_id": "u1", "created_with": {"action": "signup", "authProvider": "password"},
         "expires_at": "2024-06-08 20:39:30.517000", "created_at": "2023-06-09 20:39:30.517000"},
        {"session_id": "s2", "user_id": "u1", "created_with": {"action": "login", "authProvider": "password"},
         "created_at": "2023-06-09 20:39:32.384000"},
    ], ensure_ascii=False),
    "seazon_gorgias_ticket.json": json.dumps([
        {"gorgias_ticket_id": "g1", "tags": ["SZN AVIS"], "datetime": "2023-07-22 14:46:47.071000",
         "ticket_id": 389059779, "state": "open", "subject": "AVIS COMMANDE 2023-07-21",
         "created_at": "2023-07-22 14:46:47.533000"},
    ], ensure_ascii=False),
    "foodcheri_user.json": json.dumps({
        "scoring_churn": 64.5,
        "taste": [
            {"meal": {"__type": "Pointer", "className": "Meal", "objectId": "m1"}, "added": 1, "removed": 0, "kept": 0},
            {"meal": {"__type": "Pointer", "className": "Meal", "objectId": "m2"}, "added": 0, "removed": 1, "kept": 0},
        ],
        "confirmation_email": {"value": True, "date": "2024-09-10 10:18:22.283000"},
    }, ensure_ascii=False),
}


@pytest.fixture()
def export(tmp_path):
    for name, content in FILES.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def items(export):
    return list(seazon.SeazonImporter().parse(export))


def by_type(items, item_type):
    return [item for item in items if item.item_type == item_type]


def test_detected_as_seazon_only(export, tmp_path_factory):
    assert importers.detect(export).key == "seazon"
    elsewhere = tmp_path_factory.mktemp("other")
    (elsewhere / "orders.json").write_text("[]", encoding="utf-8")
    assert importers.detect(elsewhere) is None


def test_detected_from_one_file_of_a_partial_export(tmp_path):
    (tmp_path / "seazon_order.json").write_text("[]", encoding="utf-8")
    assert importers.detect(tmp_path).key == "seazon"


def test_only_charged_slots_are_purchases(items):
    orders = by_type(items, "seazon_order")
    assert {o.context for o in orders} == {"o1", "o2"}  # o3 and o4 never charged
    assert {o.fields["Montant"] for o in orders} == {"72.90 EUR", "84.90 EUR"}
    assert all(o.fields["Catégorie"] == "Repas" for o in orders)
    # The plan's current slot (nothing chosen, nothing paid) is not imported
    # at all -- it will come back as an order or a cancellation once final.
    assert "o4" not in {item.context for item in items}


def test_a_charged_order_cancelled_afterwards_keeps_its_amount(items):
    order, = [o for o in by_type(items, "seazon_order") if o.context == "o2"]
    assert order.fields["Statut"] == "Annulée"
    assert order.fields["Annulée le"] == "23/02/2025"  # when it took effect, not when it was asked for
    assert order.fields["Motif d'annulation"] == "Sans motif donné"
    assert order.fields["Remboursé"] == "84.90 EUR"


def test_a_cancelled_slot_is_not_a_purchase_but_keeps_its_motive(items):
    cancelled, = by_type(items, "order_cancelled")
    assert cancelled.context == "o3"
    assert cancelled.title == "Créneau du 07/07/2023 annulé"
    assert cancelled.fields["Motif d'annulation"] == "En déplacement professionnel"
    assert cancelled.fields["Annulé par"] == "Le client"
    assert cancelled.fields["Nombre de repas choisis"] == "1"


def test_order_detail_fields(items):
    order, = [o for o in by_type(items, "seazon_order") if o.context == "o1"]
    assert order.fields["Adresse de livraison"] == "12 Impasse des Chardonnerets, 64320 IDRON"
    assert order.fields["Mode de paiement"] == "Carte bancaire"
    assert order.fields["Colis"] == "XU278428588VF" and order.fields["Livrée le"] == "30/06/2023"
    assert order.fields["Facture"] == "2023-06-99631"
    assert order.fields["Nombre de repas"] == "12"
    # The initial menu is kept only when the customer changed it.
    assert order.fields["Menu initial"].startswith("Chili sin carne ×2 · Orge crémeux")
    assert order.fields["Repas"] == "Chili sin carne ×2 · Poulet korma"
    assert order.content == "Chili sin carne, Poulet korma"


def test_meals_are_items_of_their_own_carrying_no_amount(items):
    meals = by_type(items, "meal")
    assert Counter(m.context for m in meals) == {"o1": 2, "o2": 1, "o3": 1}
    assert "Montant" not in {label for meal in meals for label in meal.fields}
    chili, = [m for m in meals if m.title == "Chili sin carne"]
    assert chili.fields == {"Commande": "Livrée", "Quantité": "2", "Type de plat": "Plat",
                            "Livraison prévue": "30/06/2023"}
    korma, = [m for m in meals if m.title == "Poulet korma" and m.context == "o1"]
    assert korma.fields["Supplément"] == "1.50 EUR"
    # The dish of a slot cancelled before payment says so.
    assert [m.fields["Commande"] for m in meals if m.context == "o3"] == ["Annulée"]


def test_the_purchase_dashboard_counts_orders_not_meal_lines(items):
    rows = [
        (index, item.timestamp, item.item_type, item.context, item.content, json.dumps(item.fields))
        for index, item in enumerate(items)
        if item.item_type in ("seazon_order", "order_cancelled", "meal")
    ]
    dashboard = stats.build_purchase_dashboard(rows, "year", "category", ["Repas"])
    assert dashboard["total_spend"] == 157.8 and dashboard["total_orders"] == 2
    assert dashboard["total_lines"] == 2  # the meals are not lines
    assert stats.detect_currency(rows) == "EUR"


def test_a_refund_is_its_own_item_and_nets_the_spend(items):
    """Seazon's refunds reach the purchases page's returns, so "net of
    refunds" is the figure Seazon's own profile states."""
    refund, = by_type(items, "seazon_refund")
    assert refund.context == "o2" and refund.fields["Montant remboursé"] == "84.90 EUR"
    assert refund.timestamp == datetime(2025, 2, 23, 7, 39, 29, 636000)  # the refund's own date

    rows = [
        (index, item.timestamp, item.item_type, item.context, item.content, json.dumps(item.fields))
        for index, item in enumerate(items)
        if item.item_type in stats.PURCHASE_ITEM_TYPES
    ]
    dashboard = stats.build_purchase_dashboard(rows, "year", "category", ["Repas"])
    assert dashboard["total_spend"] == 157.8 and dashboard["total_refunded"] == 84.9
    assert dashboard["total_returns"] == 1
    assert dashboard["cumulative_series"][-1] == 157.8
    assert dashboard["cumulative_net_series"][-1] == 72.9  # the other order, kept
    # A refund is browsable like a return, and links back to its order.
    assert stats.purchase_line_type("seazon") == "seazon_order"


def test_seazon_has_its_own_purchase_type_dimensions_and_wording():
    """Nothing of Seazon's purchases comes from Amazon: its own item type,
    its own category field, its own dimensions and its own wording -- and
    Amazon's side is left exactly as it was."""
    assert stats.SEAZON_ORDER_TYPE == "seazon_order"
    assert stats.SEAZON_ORDER_TYPE in stats.PURCHASE_LINE_ITEM_TYPES
    # Its refunds are a return type of its own too, and the "what was
    # ordered" link names that platform's lines.
    assert stats.SEAZON_REFUND_TYPE == "seazon_refund"
    assert stats.SEAZON_REFUND_TYPE in stats.RETURN_ITEM_TYPES
    assert stats.SEAZON_REFUND_TYPE in stats.PURCHASE_ITEM_TYPES
    assert stats.purchase_line_type("seazon") == "seazon_order"
    assert stats.purchase_line_type("amazon") == "order_item" and stats.purchase_line_type(None) == "order_item"
    assert stats.purchase_category("seazon_order", {"Catégorie": "Repas"}) == "Repas"
    assert stats.purchase_category("seazon_order", {}) == "Sans catégorie"
    # Amazon keeps its department, and never reads Seazon's field (or the
    # other way round).
    assert stats.purchase_category("order_item", {"Département": "Kitchen"}) == "Kitchen"
    assert stats.purchase_category("order_item", {"Catégorie": "Repas"}) == "Sans département"

    # Its page groups by the formula it was paid as -- its own "Catégorie" is
    # the word "Repas" for every order ever placed, so it never was a
    # dimension worth offering.
    seazon = stats.purchase_group_dimensions("seazon")
    assert seazon["formula"] == "Formule"
    assert set(seazon) == {"formula", "none", "shipping_address", "payment_method", "status"}
    assert stats.purchase_group("seazon_order", {"Nombre de repas": "10"}, "formula") == "10 plats"
    assert stats.purchase_group("seazon_order", {}, "formula") == "Non renseigné"
    amazon = stats.purchase_group_dimensions("amazon")
    assert amazon["category"] == "Département"
    assert set(amazon) == set(stats.PURCHASE_GROUP_DIMENSIONS)
    # The formula is Seazon's own: it never leaks into Amazon's dimensions,
    # where it could only ever answer "Non renseigné".
    assert "formula" not in amazon and "formula" not in stats.PURCHASE_GROUP_DIMENSIONS
    # A platform with no set of its own gets the Amazon-shaped one.
    assert stats.purchase_group_dimensions(None) == amazon

    # And their own `kind` in the Influx purchase series, so a Grafana query
    # can tell them from Amazon's lines.
    from app import influx_export
    purchase_series, = [s for s in influx_export.SERIES if s.measurement == "purchase"]
    assert influx_export._KIND_BY_PURCHASE["seazon_order"] == "meal"
    assert "seazon_order" in purchase_series.item_types


def test_plans_keep_their_whole_history(items):
    plans = {plan.context: plan for plan in by_type(items, "subscription_plan")}
    assert plans["p1"].title == "Abonnement du vendredi" and plans["p1"].fields["Statut"] == "Actif"
    assert plans["p1"].fields["Créneau"] == "8h - 13h"
    assert plans["p0"].content == "Livraison le mardi (8h - 13h)"
    assert plans["p0"].fields["Motif de fin"] == "Plats non terminés"
    assert plans["p0"].fields["Réactivé le"] == "03/03/2024" and plans["p0"].fields["Premier abonnement"] == "Oui"


def test_opinions_name_the_rated_dishes(items):
    opinion, = by_type(items, "opinion")
    assert opinion.context == "o1" and opinion.fields["Livraison"] == "5/5"
    # The dish is only a pointer in the export: named through the meals the
    # customer ordered, which is the only place its id meets its name.
    assert opinion.fields["Plats notés"] == "Chili sin carne (qualité 4/quantité 5/goût 4)"


def test_notifications_and_logs_are_labelled_in_french(items):
    read, unread = by_type(items, "notification")
    assert (read.title, read.fields["Lue"], read.fields["Motif"]) == (
        "Nouvelle carte : vendredi 07 juillet !", "Oui", "Nouvelle carte de la semaine")
    assert unread.fields["Destinataire"] == "Marketing" and "Lue" not in unread.fields
    cancelled, = [log for log in by_type(items, "account_log") if log.context == "o3"]
    assert cancelled.content == "a suspendu la commande 07/07/23. Motif: Pas de plats qui me correspondent"
    assert cancelled.fields["Action"] == "Annulation" and cancelled.fields["Motif"] == "Aucun plat qui convient"
    assert cancelled.fields["Remboursé"] == "12.50 EUR"
    selection, = [log for log in by_type(items, "account_log") if log.context == "o1"]
    assert selection.fields["Plats choisis"] == "Chili sin carne ×2"


def test_logins_coupons_and_tickets(items):
    assert [login.title for login in by_type(items, "login")] == ["Inscription", "Connexion"]
    used, expired = by_type(items, "coupon")
    assert used.fields["Statut"] == "Utilisé" and used.fields["Montant utilisé"] == "20.00 EUR"
    assert used.fields["Utilisé le"] == "25/06/2023" and used.fields["Commande"] == "o1"
    assert expired.content == "Expiré sans utilisation"
    ticket, = by_type(items, "support_ticket")
    assert ticket.title == "AVIS COMMANDE 2023-07-21" and ticket.fields["Statut"] == "Ouvert"


def test_taste_preferences_are_named_through_the_ordered_meals(items):
    named, unnamed = by_type(items, "taste_preference")
    assert named.title == "Chili sin carne" and named.content == "Ajouté au menu 1 fois"
    assert named.fields["Retiré du menu"] == "0"
    # Nothing in the export names the dish behind this pointer.
    assert unnamed.title == "Plat m2" and unnamed.content == "Retiré du menu 1 fois"
    # They carry no date of their own.
    assert named.timestamp is None


def test_the_profile_summarizes_the_account(items):
    profile, = by_type(items, "profile")
    assert profile.title == "Camille Durand" and profile.timestamp == datetime(2023, 6, 9, 20, 39, 30, 87000)
    assert profile.fields["Date de naissance"] == "27/10/1994" and profile.fields["Genre"] == "Femme"
    assert profile.fields["Commandes facturées"] == "2"
    assert profile.fields["Total dépensé (selon Seazon)"] == "157.80 EUR"
    assert profile.fields["Notifications e-mail"] == "Oui" and profile.fields["Notifications SMS"] == "Non"
    assert profile.fields["Adresses"] == "12 Impasse des Chardonnerets, 64320 IDRON"


def test_the_platform_shows_its_purchases_and_activity_views(items):
    views = [view.key for view in platforms.available_views({item.item_type for item in items})]
    assert "purchases" in views and "activity" in views
    # Seazon's own two, between the purchases and the activity tab, and only
    # for a platform whose data has boxes and dishes to read.
    assert views[:4] == ["overview", "purchases", "formulas", "dishes"]
    amazon = platforms.available_views({"order_item", "return"})
    assert "formulas" not in amazon and "dishes" not in amazon


def test_a_progressing_order_stays_the_same_record():
    def signature(fields):
        return dedup.item_signature("seazon", "seazon_order", "Commandes", datetime(2025, 1, 10), None, None, None,
                                    "o2", json.dumps(fields))

    # Charged, then shipped (tracking number), then rated: every field the
    # export fills in afterwards stays out of the fingerprint.
    assert signature({"Statut": "Payée", "Montant": "84.90 EUR"}) == signature(
        {"Statut": "Livrée", "Montant": "84.90 EUR", "Colis": "XU1", "Avis": "5/5"})
    # A cancelled-and-refunded order keeps the same identity too.
    assert signature({"Statut": "Payée", "Montant": "84.90 EUR"}) == signature(
        {"Statut": "Annulée", "Montant": "84.90 EUR", "Remboursé": "84.90 EUR"})


def test_an_amazon_order_line_still_tells_its_amounts_apart():
    """Seazon's per-source volatility must not reach Amazon's shared
    `order_item`, whose fingerprints are unchanged."""
    def signature(fields):
        return dedup.item_signature("amazon", "order_item", "Commandes", datetime(2025, 1, 10), None, "Produit",
                                    None, "ORDER-1", json.dumps(fields))

    assert signature({"Montant": "10.00 EUR"}) != signature({"Montant": "20.00 EUR"})


def test_an_order_charged_in_one_export_and_delivered_in_the_next_is_counted_once(tmp_path):
    """The export fills a record in as the order progresses, so the same
    order must not become a second purchase once its tracking number, its
    delivery or its refund shows up in a later export."""
    from app.database import Base, SessionLocal, engine
    from app.models import ImportBatch, ImportStatus, Item

    Base.metadata.create_all(engine)
    session = SessionLocal()
    # The source a real import stores ("processing" uses the importer's key),
    # which is also what dedup keys the per-source volatility on.
    source = "seazon"

    def store(batch_id, items):
        for item in items:
            session.add(Item(
                batch_id=batch_id, source=source, category=item.category, item_type=item.item_type,
                timestamp=item.timestamp, title=item.title, content=item.content, author=item.author,
                context=item.context, fields_json=json.dumps(item.fields) if item.fields else None,
            ))
        session.commit()

    def write(folder, files):
        folder.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (folder / name).write_text(content, encoding="utf-8")
        return folder

    first = ImportBatch(original_filename="seazon-2025.zip", source=source, status=ImportStatus.done)
    second = ImportBatch(original_filename="seazon-2026.zip", source=source, status=ImportStatus.done)
    session.add_all([first, second])
    session.commit()
    both = [first.id, second.id]

    def visible():
        return [
            item_type for item_type, in session.query(Item.item_type).filter(
                Item.batch_id.in_(both), Item.is_duplicate.is_(False))
        ]

    charged = write(tmp_path / "first", FILES)
    store(first.id, seazon.SeazonImporter().parse(charged))
    dedup.finalize_batch(session, first.id, source)
    assert Counter(visible()) == {"seazon_order": 2, "seazon_refund": 1, "order_cancelled": 1, "meal": 4,
                                  "notification": 2, "account_log": 2, "subscription_plan": 2, "opinion": 1,
                                  "login": 2, "support_ticket": 1, "coupon": 2, "taste_preference": 2,
                                  "profile": 1}

    later = json.loads(json.dumps(FILES))
    orders = json.loads(later["seazon_order.json"])
    charged_order, = [order for order in orders if order["order_id"] == "o2"]
    charged_order["state"] = "shipped"          # was cancelled: refunded afterwards
    charged_order["cancelled_date"] = None
    charged_order["skybill_numbers"] = ["XU999"]
    charged_order["delivery_dates"] = [{"date": "2025-02-19T10:39:52+02:00"}]
    later["seazon_order.json"] = json.dumps(orders, ensure_ascii=False)
    store(second.id, seazon.SeazonImporter().parse(write(tmp_path / "second", later)))
    dedup.finalize_batch(session, second.id, source)

    # Not a single extra purchase: the newest copy of the order is the one
    # the platform views count.
    assert Counter(visible()) == {"seazon_order": 2, "seazon_refund": 1, "order_cancelled": 1, "meal": 4,
                                  "notification": 2, "account_log": 2, "subscription_plan": 2, "opinion": 1,
                                  "login": 2, "support_ticket": 1, "coupon": 2, "taste_preference": 2,
                                  "profile": 1}
    (kept,) = session.query(Item).filter(
        Item.source == source, Item.item_type == "seazon_order", Item.context == "o2", Item.is_duplicate.is_(False)
    ).all()
    assert kept.batch_id == second.id
    assert "Livrée le" in json.loads(kept.fields_json) and json.loads(kept.fields_json)["Statut"] == "Livrée"


def test_the_anonymized_copy_still_imports_as_seazon(export, tmp_path_factory):
    out = tmp_path_factory.mktemp("anon")
    assert anonymize.anonymize_export(export, out / "anon.zip", source_key="seazon") == []
    with zipfile.ZipFile(out / "anon.zip") as zf:
        names = zf.namelist()
        zf.extractall(out / "files")
    # Filenames are never renamed, so the export is recognized as Seazon.
    assert "seazon_order.json" in names
    assert importers.detect(out / "files").key == "seazon"
    anonymized = list(seazon.SeazonImporter().parse(out / "files"))
    assert len(by_type(anonymized, "seazon_order")) == 2
    assert "Camille" not in (out / "files" / "seazon_user.json").read_text(encoding="utf-8")
    # Orders are still linked to their meals and ratings by the same order id.
    orders = {item.context for item in by_type(anonymized, "seazon_order")}
    assert orders and orders == {item.context for item in by_type(anonymized, "meal")}


def test_a_box_is_dated_when_it_was_paid_and_a_dish_when_it_arrived(items):
    """`created_at` is when the plan opened the slot, roughly a quarter before
    anything happened: every time-based reading would be shifted by it."""
    order, = [o for o in by_type(items, "seazon_order") if o.context == "o1"]
    assert order.timestamp == datetime(2023, 6, 25, 0, 15, 49, 344000)  # charge_date, not created_at
    chili, = [m for m in by_type(items, "meal") if m.title == "Chili sin carne"]
    assert chili.timestamp == datetime(2023, 6, 30)  # the day the box arrived


# ── The formulas, and the dishes that went in them ────────────────────────
#
# The two Seazon views read the same items as the pages do: (id, timestamp,
# item_type, context, content, fields_json) for the boxes (the shape
# crud.get_dashboard_rows returns) and (id, timestamp, title, fields_json) for
# the dishes (crud.get_titled_rows, which carries the dish name the dashboard
# row shape has no room for).

def box_rows(*entries):
    return [
        (index, ts, "seazon_order", order_id, None, json.dumps(fields))
        for index, (ts, order_id, fields) in enumerate(entries)
    ]


def plate(index, ts, dish, **fields):
    return (index, ts, dish, json.dumps(fields))


DELIVERED = {"Commande": "Livrée"}
CANCELLED = {"Commande": "Annulée"}

BOXES = box_rows(
    (datetime(2023, 9, 1), "o1", {"Nombre de repas": "10", "Montant": "79.90 EUR"}),
    (datetime(2024, 9, 1), "o2", {"Nombre de repas": "10", "Montant": "84.90 EUR"}),
    (datetime(2024, 10, 1), "o3", {"Nombre de repas": "14", "Montant": "109.90 EUR"}),
)


def test_the_formula_dashboard_reads_boxes_by_plates_and_price_per_plate():
    dashboard = stats.build_formula_dashboard(BOXES, "year", ["10 plats", "14 plats"])
    ten, fourteen = dashboard["formulas"]
    assert (ten["label"], ten["boxes"], ten["plates"], ten["spend"]) == ("10 plats", 2, 20, 164.8)
    # Seazon charges one amount for the whole box: what a plate cost is a
    # division, and the figure that compares two boxes of different sizes.
    assert (ten["per_box"], ten["per_plate"]) == (82.4, 8.24)
    assert (fourteen["boxes"], fourteen["plates"], fourteen["per_plate"]) == (1, 14, 7.85)
    assert (dashboard["total_boxes"], dashboard["total_plates"], dashboard["total_spend"]) == (3, 34, 274.7)

    assert dashboard["boxes"]["labels"] == ["2023", "2024"]
    assert [(s["label"], s["data"]) for s in dashboard["boxes"]["series"]] == [
        ("10 plats", [1, 1]), ("14 plats", [0, 1]),
    ]
    # A formula with no box in a bucket leaves a gap in its line rather than
    # a price of zero.
    assert [(s["label"], s["data"]) for s in dashboard["unit_price"]["series"]] == [
        ("10 plats", [7.99, 8.49]), ("14 plats", [None, 7.85]),
    ]


def test_a_box_without_a_stated_formula_is_not_guessed_at():
    rows = box_rows((datetime(2024, 9, 1), "o1", {"Montant": "79.90 EUR"}))
    dashboard = stats.build_formula_dashboard(BOXES[:1] + rows, "year", ["10 plats", "Non renseigné"])
    assert [(f["label"], f["boxes"], f["per_plate"]) for f in dashboard["formulas"]] == [
        ("10 plats", 1, 7.99), ("Non renseigné", 1, None),
    ]


def test_the_dish_ranking_counts_portions_eaten_never_the_boxes():
    rows = [
        plate(0, datetime(2024, 1, 5), "Chili sin carne", Quantité="3", **DELIVERED),
        plate(1, datetime(2024, 2, 5), "Chili sin carne ", **DELIVERED),  # a stray space: one dish
        plate(2, datetime(2024, 3, 5), "Chili sin carne", **CANCELLED),
        plate(3, datetime(2024, 3, 8), "Curry vert indien", Quantité="2", **DELIVERED),
        plate(4, datetime(2024, 4, 5), "Tarte citron", **CANCELLED),
    ]
    ranking = stats.build_dish_ranking(rows)
    assert [(d["label"], d["count"], d["undelivered"]) for d in ranking["top"]] == [
        ("Chili sin carne", 4, 1), ("Curry vert indien", 2, 0), ("Tarte citron", 0, 1),
    ]
    # Three dishes were picked, two of them were ever eaten.
    assert (ranking["distinct"], ranking["total"], ranking["undelivered_total"]) == (2, 6, 2)


def test_the_dish_trend_draws_the_portions_actually_eaten():
    rows = [
        plate(0, datetime(2023, 6, 5), "Chili sin carne", Quantité="2", **DELIVERED),
        plate(1, datetime(2024, 1, 5), "Chili sin carne", **DELIVERED),
        plate(2, datetime(2024, 3, 8), "Curry vert indien", Quantité="2", **DELIVERED),
        plate(3, datetime(2024, 4, 5), "Tarte citron", Quantité="5", **CANCELLED),
    ]
    leaders = stats.build_dish_ranking(rows)["top"]
    assert [leader["label"] for leader in leaders][-1] == "Tarte citron"
    trend = stats.build_dish_trend(rows, "year", leaders)
    assert trend["labels"] == ["2023", "2024"]
    # A cancelled box's five helpings of tarte citron were never eaten, so it
    # is never drawn -- but it kept the last colour, and takes it back the
    # year it is eaten again.
    assert [(s["label"], s["data"]) for s in trend["series"]] == [
        ("Chili sin carne", [2, 1]), ("Curry vert indien", [0, 2]),
    ]


def test_a_panel_opens_exactly_what_was_counted():
    rows = [
        plate(0, datetime(2024, 1, 5), "Chili sin carne", **DELIVERED),
        plate(1, datetime(2024, 2, 5), "Chili sin carne", **CANCELLED),
        plate(2, datetime(2024, 3, 8), "Curry vert indien", **DELIVERED),
        plate(3, datetime(2023, 6, 5), "Curry vert indien", Quantité="2", **CANCELLED),
    ]
    # A dish opens its plates, most recent first -- the eaten ones, as the
    # ranking counted them.
    assert stats.select_meals(rows, "chili sin carne") == [0]
    assert stats.select_meals(rows, "chili sin carne", delivered_only=False) == [1, 0]
    # Everything a chart did not name ("Autres"), or everything at all.
    assert stats.select_meals(rows, None, exclude=["chili sin carne"]) == [2]
    assert stats.select_meals(rows, None, exclude=["chili sin carne"], delivered_only=False) == [2, 3]
    assert stats.select_meals(rows) == [2, 0]
    assert stats.select_meals(rows, date_from=datetime(2024, 3, 1), date_to=datetime(2024, 12, 31)) == [2]


def test_a_bar_opens_the_boxes_of_its_formula_and_period():
    assert stats.select_formula_orders(BOXES, "10 plats") == [1, 0]
    assert stats.select_formula_orders(BOXES) == [2, 1, 0]
    assert stats.select_formula_orders(BOXES, "10 plats", datetime(2024, 1, 1), datetime(2024, 12, 31)) == [1]


# ── The pages themselves ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from fastapi.testclient import TestClient

    from app import processing
    from app.database import Base, SessionLocal, engine
    from app.main import app
    from app.models import ImportBatch, ImportStatus

    root = tmp_path_factory.mktemp("seazon-export")
    for name, content in FILES.items():
        (root / name).write_text(content, encoding="utf-8")
    Base.metadata.create_all(engine)
    db = SessionLocal()
    batch = ImportBatch(original_filename="gdpr_export.zip", status=ImportStatus.parsing)
    db.add(batch)
    db.commit()
    processing._detect_and_parse(db, batch, root)
    db.close()
    return TestClient(app)


def test_the_formulas_tab_reads_the_boxes_the_money_was_paid_in(client):
    html = client.get("/p/seazon").text
    for tab in ("formulas", "dishes"):
        assert f'href="/p/seazon/{tab}"' in html

    html = client.get("/p/seazon/formulas").text
    assert "Prix du plat dans le temps" in html and "Boîtes dans le temps, par formule" in html
    assert "10 plats" in html and "12 plats" in html and "84,90" in html
    assert "unitPriceChart" in html


def test_the_dishes_tab_lists_what_was_eaten(client):
    html = client.get("/p/seazon/dishes").text
    assert "Tous les plats" in html and "Chili sin carne" in html
    # The two meals of o1 were delivered; o2's and o3's boxes never were.
    assert "Plats mangés" in html and "Commandés, non livrés" in html

    html = client.get("/p/seazon/dishes/panel", params={"dish": "chili sin carne"}).text
    assert "Chili sin carne" in html


def test_the_purchases_page_groups_by_formula_not_by_a_category_that_says_nothing(client):
    html = client.get("/p/seazon/purchases").text
    assert "Par formule" in html and "Par catégorie" not in html
    # The money itself is untouched: both charged boxes are still counted.
    assert "157,80" in html
