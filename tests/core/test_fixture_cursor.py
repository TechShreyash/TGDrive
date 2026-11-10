"""The offline browser double exposes cursor semantics rather than list.sort."""

from tests.support.memory_mongo import FixtureCursor


def test_fixture_cursor_sorts_missing_values_and_returns_a_chainable_cursor():
    rows = [
        {"_id": "later", "order": 2},
        {"_id": "missing"},
        {"_id": "first", "order": 1},
    ]
    cursor = FixtureCursor(rows)

    assert cursor.sort("order") is cursor
    assert [row["_id"] for row in cursor] == ["missing", "first", "later"]
    assert len(cursor) == 3 and cursor[0]["_id"] == "missing"
    assert [row["_id"] for row in cursor.sort("order", -1).limit(2)] == [
        "later",
        "first",
    ]
