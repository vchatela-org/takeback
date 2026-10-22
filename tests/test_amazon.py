import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.importers import amazon  # noqa: E402

FIRE_TABLET_USAGE_HEADER = (
    '"Amazon Marketplace","App Package Name","Device Type","Duration Milliseconds","Launch Count","Start Time"\n'
)
CREDITS_HEADER = (
    '"Consumed Credit","Consumed Credit Revenue","Consumed Date","Consumed Order","Consumed Status",'
    '"Credit Reason",Credit Status,Currency Code,"Expire Date","Issue Date",Plan\n'
)
PRIME_HEADER = (
    '"Amazon Device Model Name","Device Model",Is Deleted,"Playback Start Datetime (UTC)",'
    '"Seconds Viewed",Title\n'
)
GIFT_HEADER = (
    '"Currency Code","Expiration Date","External Transaction ID","Issuance Date",Marketplace,'
    '"Original Gift Card Balance","Purchaser Name","Recipient Email","Recipient Name",'
    '"Remaining Gift Card Balance",Status,"Delivery Type"\n'
)
ADDRESS_HEADER = (
    '"Address Line 1","Address Line 2","Country Code","Creation Date",'
    '"Is Address Active",Label,"Last Order Date",Municipality,Name,"Postal Code"\n'
)
PREFERENCE_HEADER = '"Address ID","Attribute Data","Attribute Type","Country Code"\n'


def write(export: Path, name: str, text: str) -> None:
    path = export / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig")


@pytest.fixture
def export(tmp_path):
    write(
        tmp_path, "Your Fire Tablet Apps & Usage/App Usage.csv", FIRE_TABLET_USAGE_HEADER
        + 'Amazon.fr,com.amazon.kindle,Ereader,65000,1,2026-09-09T22:26:19Z\n',
    )
    write(
        tmp_path, "Your Audible Account & Membership/Credits History.csv", CREDITS_HEADER
        + '1.00,0.94,2026-10-09,D01-3878773-0248622,consumed,"Access Monthly",Inactive,EUR,2026-11-08,'
        '2026-10-08,"Access Monthly"\n'
        + '1.00,0.00,Not available,Not available,Not available,"Access Monthly",Active,EUR,2026-12-08,'
        '2026-11-08,"Access Monthly"\n',
    )
    write(
        tmp_path, "Your Prime Video Viewing Activity/Viewing History.csv", PRIME_HEADER
        + '"""Not available""","""MIBOX3""","""No""",2024-05-08T13:16:16Z,901,'
        '"""Auditions continue, Season 1"""\n'
        + '"""Not available""","""Not available""","""Yes""",2024-05-09T10:00:00Z,60,'
        '"""Deleted from history"""\n'
        + '"""Not available""","""Not available""","""No""",2024-05-10T10:00:00Z,120,'
        '"""Season 2 episode"""\n',
    )
    write(
        tmp_path, "Your Gift Cards & Balances/Gift Cards.csv", GIFT_HEADER
        + 'EUR,2036-09-06T21:59:59Z,113-1151616-8094645,2026-09-06T10:07:06Z,www.amazon.fr,'
        '41.16,"Not Available",Jane Doe,Jane Doe,0.0,Claimed,ClaimToAccount\n'
        + 'EUR,Not Available,Not Available,2024-01-02T00:00:00Z,www.amazon.fr,10.0,'
        '"Not Available","Not Available","Not Available",10.0,Claimed,Email\n',
    )
    write(
        tmp_path, "Your Saved Addresses/Addresses.csv", ADDRESS_HEADER
        + '"1 Example Street","Not Available",FR,2026-04-23T11:30:51Z,Yes,'
        '"Jane Doe- Springfield",Not Available,Springfield,Jane Doe,12345\n',
    )
    write(
        tmp_path, "Your Address Preferences & Delivery/Address Preferences.csv", PREFERENCE_HEADER
        + '1623082732103,FRONTDOOR,"SafePlaceLocationPreference",FR\n',
    )
    return tmp_path


@pytest.fixture
def items(export):
    return list(amazon.AmazonImporter().parse(export))


def by_type(items, item_type):
    return [i for i in items if i.item_type == item_type]


def test_fire_tablet_app_usage_is_read_under_its_own_device(items):
    (usage,) = [i for i in items if i.category == "Fire Tablet"]
    assert usage.item_type == "app_engagement"
    assert usage.content == "com.amazon.kindle"
    assert usage.fields == {"Durée": "1.1 min", "Lancements": "1"}


def test_audible_credits_read_with_their_value_and_expiry(items):
    credits = by_type(items, "audible_credit")
    assert len(credits) == 2
    consumed, unused = credits
    assert consumed.content == "Access Monthly"
    assert consumed.timestamp == datetime(2026, 10, 9)
    assert consumed.context == "D01-3878773-0248622"
    assert consumed.fields == {
        "Crédits": "1.00", "Valeur": "0.94 EUR", "Statut": "consumed", "Expire le": "08/11/2026",
    }
    assert unused.timestamp == datetime(2026, 11, 8)  # no consumed date: falls back to issue date
    assert unused.fields["Valeur"] == "0.00 EUR"


def test_prime_video_views_are_unquoted_and_deleted_ones_dropped(items):
    views = by_type(items, "prime_video_view")
    assert [v.content for v in views] == ["Auditions continue, Season 1", "Season 2 episode"]
    first = views[0]
    assert first.timestamp == datetime(2024, 5, 8, 13, 16, 16, tzinfo=timezone.utc)
    assert first.fields == {"Durée vue": "15 min", "Appareil": "MIBOX3"}
    assert views[1].fields == {"Durée vue": "2 min"}  # no device recorded


def test_gift_cards_leave_out_the_other_people_named_on_them(items):
    cards = by_type(items, "gift_card")
    assert len(cards) == 2
    funded, received = cards
    assert funded.content == "Carte cadeau 41.16 EUR"
    assert funded.fields == {
        "Montant initial": "41.16 EUR",
        "Solde restant": "0.00 EUR",
        "Expiration": "06/09/2036",
        "Statut": "Claimed",
        "Mode de livraison": "ClaimToAccount",
        "Site": "www.amazon.fr",
    }
    assert all("Jane" not in str(card.fields) for card in cards)
    assert "Expiration" not in received.fields


def test_saved_addresses_and_delivery_preferences(items):
    (address,) = by_type(items, "saved_address")
    assert address.content == "Jane Doe"
    assert address.context == "1 Example Street"
    assert address.fields == {
        "Ville": "12345 Springfield", "Pays": "FR", "Libellé": "Jane Doe- Springfield", "Active": "Oui",
    }

    (preference,) = by_type(items, "delivery_preference")
    assert preference.content == "FRONTDOOR"
    assert preference.context == "SafePlaceLocationPreference"


def test_files_it_does_not_read_yield_nothing(tmp_path):
    write(tmp_path, "Your Audible Account & Membership/Membership History.csv", "Action,Event Date\nx,2026-10-08\n")
    write(tmp_path, "Your Gift Cards & Balances/Gift Card Balance.csv", "Currency Code,Current Gift Card Balance\nEUR,0.0\n")
    assert list(amazon.AmazonImporter().parse(tmp_path)) == []
