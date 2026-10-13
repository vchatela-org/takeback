import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import stats  # noqa: E402

ROWS = [
    (1, datetime(2024, 3, 1, 10), "me", "Alice"),
    (2, datetime(2024, 3, 1, 11), "alice", "Alice"),
    (3, datetime(2024, 3, 1, 12), "me", "Alice"),
    (4, datetime(2024, 3, 2, 9), "alice", "Alice"),
    (5, datetime(2024, 3, 3, 9), "me", "Bob"),
    (6, None, "bob", "Bob"),
    (7, datetime(2024, 4, 1), "me", "Carol"),
]
START, END = datetime(2024, 3, 1), datetime(2024, 3, 31, 23, 59, 59)


def test_counts_each_side_per_conversation_busiest_first():
    threads = stats.period_threads(ROWS, "me", None, START, END)
    assert [(t["context"], t["sent"], t["received"]) for t in threads] == [
        ("Alice", 2, 2),
        ("Bob", 1, 0),
    ]


def test_shares_of_all_messages_of_the_period_add_up_to_100():
    threads = stats.period_threads(ROWS, "me", None, START, END)
    assert [t["share"] for t in threads] == [80.0, 20.0]


def test_direction_restricts_counts_and_shares():
    threads = stats.period_threads(ROWS, "me", "received", START, END)
    assert [(t["context"], t["count"], t["share"]) for t in threads] == [("Alice", 2, 100.0)]


def test_conversation_with_no_message_of_that_direction_is_left_out():
    threads = stats.period_threads(ROWS, "me", "sent", START, END)
    assert [t["context"] for t in threads] == ["Alice", "Bob"]


def test_undated_and_out_of_period_messages_are_not_counted():
    threads = stats.period_threads(ROWS, "me", None, START, END)
    assert all(t["context"] != "Carol" for t in threads)
    # Five dated March messages: the undated one and the April one are out.
    assert sum(t["count"] for t in threads) == 5


def test_group_chat_is_flagged():
    rows = [
        (1, datetime(2024, 3, 1), "me", "Team"),
        (2, datetime(2024, 3, 1), "ann", "Team"),
        (3, datetime(2024, 3, 1), "ben", "Team"),
    ]
    (team,) = stats.period_threads(rows, "me", None, START, END)
    assert team["group"] is True


def test_no_messages_gives_empty_list():
    assert stats.period_threads([], "me", None, START, END) == []
