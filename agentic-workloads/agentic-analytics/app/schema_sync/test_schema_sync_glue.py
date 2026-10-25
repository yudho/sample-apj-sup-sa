"""Publish rewrites the Glue column list from the Postgres snapshot."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import schema_sync_lambda as m  # noqa: E402

EXISTING = [{"Name": "booking_id", "Type": "string"}, {"Name": "price", "Type": "decimal(10,2)"},
            {"Name": "promo_code", "Type": "string"}]


def _obj(*cols):
    return {"kind": "table", "columns": [{"name": n, "type": t, "comment": c} for n, t, c in cols]}


def test_renamed_column_takes_the_new_name_and_keeps_the_old_type():
    obj = _obj(("booking_id", "uuid", ""), ("price", "numeric(10,2)", ""),
               ("promotion_code", "character varying(20)", "Promo code"))
    changes = [{"type": "column_renamed", "object": "bookings", "from": "promo_code", "to": "promotion_code"}]
    cols = m.glue_columns("bookings", obj, EXISTING, changes)
    assert [c["Name"] for c in cols] == ["booking_id", "price", "promotion_code"]
    assert cols[2] == {"Name": "promotion_code", "Type": "string", "Comment": "Promo code"}


def test_added_column_gets_a_mapped_type_and_dropped_column_disappears():
    obj = _obj(("booking_id", "uuid", ""), ("discount_pct", "numeric(5,2)", ""),
               ("created_at", "timestamp with time zone", ""), ("seats", "integer", ""))
    cols = m.glue_columns("bookings", obj, EXISTING, [])
    assert {c["Name"]: c["Type"] for c in cols} == {
        "booking_id": "string", "discount_pct": "decimal(5,2)", "created_at": "timestamp", "seats": "int"}


def test_rename_on_another_table_is_ignored():
    obj = _obj(("promotion_code", "text", ""))
    changes = [{"type": "column_renamed", "object": "payments", "from": "promo_code", "to": "promotion_code"}]
    assert m.glue_columns("bookings", obj, EXISTING, changes) == [{"Name": "promotion_code", "Type": "string"}]
