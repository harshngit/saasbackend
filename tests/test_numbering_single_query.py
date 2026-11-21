"""Document numbers in one round trip, with the highest-issued re-check inside it.

Once a series' counter row exists, each number is one `UPDATE number_sequences ... RETURNING`
whose new value is max(counter, highest number already issued under the exact stem) + 1.
Covers: both order-number formats side by side (legacy SO-1102 and SO-<company>-<year>-NNNN),
a number written past the counter (import / script), the first number of a series, an exact
stem when the prefix contains '_', journal entry numbers, and parallel allocation.
"""

import os
import re
import sys
import threading
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import event  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.models import Customer, Organization, SalesOrder  # noqa: E402
from app.models.number_sequence import NumberSequence  # noqa: E402
from app.services import accounting_service, numbering_service  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


def _org(prefixes: dict | None = None) -> tuple[str, str, str]:
    """(org id, customer id, the stem current-format SO numbers use)."""
    with SessionLocal() as db:
        code = uuid.uuid4().int % 90000 + 10000
        org = Organization(name=f"Num {uuid.uuid4().hex[:6]}", company_code=f"CMP-{code}", state="Maharashtra",
                           currency="INR", number_prefixes=prefixes or None)
        db.add(org)
        db.flush()
        cust = Customer(organization_id=org.id, name="C", state="Maharashtra")
        db.add(cust)
        db.commit()
        first = numbering_service.next_number(db, org.id, SalesOrder.order_number, "SO")
        db.rollback()                                  # only to learn the stem; nothing kept
        stem = first[: first.rfind("-") + 1]
        return org.id, cust.id, stem


def _add_order(org_id: str, cust_id: str, number: str) -> None:
    with SessionLocal() as db:
        db.add(SalesOrder(organization_id=org_id, customer_id=cust_id, order_number=number,
                          sales_order_number=number, status="placed", total=0, subtotal=0))
        db.commit()


def _next(org_id: str, cust_id: str, keep: bool = True) -> tuple[str, list[str]]:
    """Allocate the next SO number (and store an order with it); return it and the SQL run."""
    stmts: list[str] = []

    def _cap(conn, cur, statement, *a):
        stmts.append(re.sub(r"\s+", " ", statement))

    with SessionLocal() as db:
        event.listen(engine, "before_cursor_execute", _cap)
        try:
            number = numbering_service.next_number(db, org_id, SalesOrder.order_number, "SO")
        finally:
            event.remove(engine, "before_cursor_execute", _cap)
        if keep:
            db.add(SalesOrder(organization_id=org_id, customer_id=cust_id, order_number=number,
                              sales_order_number=number, status="placed", total=0, subtotal=0))
        db.commit()
    return number, stmts


def _numbering_stmts(stmts: list[str]) -> list[str]:
    """Statements on the counter or the numbered table. (A fresh session also loads the
    organization for its company code / prefix; inside a request it is already loaded.)"""
    return [s for s in stmts if re.search(r"\b(number_sequences|sales_orders)\b", s)]


def _seq_no(number: str) -> int:
    return int(number.rsplit("-", 1)[1])


def test_one_round_trip_once_the_counter_exists():
    org, cust, stem = _org()
    first, _ = _next(org, cust)                      # creates the counter row (slow path, once)
    second, stmts = _next(org, cust)
    assert _seq_no(second) == _seq_no(first) + 1
    numbering = _numbering_stmts(stmts)
    assert len(numbering) == 1 and numbering[0].startswith("UPDATE number_sequences"), numbering


def test_both_formats_side_by_side():
    """Legacy SO-1102 numbers neither move the counter nor collide; the current format
    continues from its own highest number."""
    org, cust, stem = _org()
    for legacy in ("SO-1102", "SO-1150", "SO-9999"):
        _add_order(org, cust, legacy)
    a, _ = _next(org, cust)
    _add_order(org, cust, "SO-99999")                # more legacy numbers after the counter exists
    b, _ = _next(org, cust)
    assert a.startswith(stem) and b.startswith(stem)
    assert _seq_no(b) == _seq_no(a) + 1
    with SessionLocal() as db:
        numbers = [n for (n,) in db.query(SalesOrder.order_number).filter(SalesOrder.organization_id == org)]
    assert len(numbers) == len(set(numbers))


def test_number_written_past_the_counter_is_skipped_in_the_same_query():
    org, cust, stem = _org()
    first, _ = _next(org, cust)
    _add_order(org, cust, f"{stem}0050")            # e.g. an import wrote a higher number directly
    nxt, stmts = _next(org, cust)
    assert nxt == f"{stem}0051"
    assert len(_numbering_stmts(stmts)) == 1


def test_non_numeric_and_other_stems_are_ignored():
    org, cust, stem = _org()
    _next(org, cust)
    _add_order(org, cust, f"{stem}9A")              # not all digits after the stem
    _add_order(org, cust, f"{stem}0007-X")
    other_year = stem.replace(stem.split("-")[2], "1999") if stem.count("-") >= 3 else "SO-0-1999-"
    _add_order(org, cust, f"{other_year}0900")       # another year's stem
    nxt, _ = _next(org, cust)
    assert nxt == f"{stem}0002"


def test_prefix_with_underscore_matches_exactly():
    """'_' in a custom prefix must not act as a LIKE wildcard."""
    org, cust, stem = _org({"SO": "S_O"})
    assert stem.startswith("S_O-")
    _next(org, cust)
    _add_order(org, cust, stem.replace("S_O", "SXO", 1) + "0777")   # would match 'S_O%' as a wildcard
    nxt, _ = _next(org, cust)
    assert nxt == f"{stem}0002"


def test_first_number_of_a_series_still_seeds_from_existing_numbers():
    org, cust, stem = _org()
    _add_order(org, cust, f"{stem}0040")             # numbers present before any counter row
    with SessionLocal() as db:
        assert db.query(NumberSequence).filter(NumberSequence.organization_id == org,
                                               NumberSequence.series == "SO").count() == 0
    first, _ = _next(org, cust)
    assert first == f"{stem}0041"


def test_journal_entry_numbers_one_round_trip_after_the_first():
    org, _cust, _stem = _org()
    with SessionLocal() as db:
        a = accounting_service._allocate_journal_entry_number(db, org)
        stmts: list[str] = []

        def _cap(conn, cur, statement, *a_):
            stmts.append(statement)

        event.listen(engine, "before_cursor_execute", _cap)
        try:
            b = accounting_service._allocate_journal_entry_number(db, org)
        finally:
            event.remove(engine, "before_cursor_execute", _cap)
        db.commit()
    assert int(b.rsplit("-", 1)[1]) == int(a.rsplit("-", 1)[1]) + 1
    assert len(stmts) == 1 and stmts[0].lstrip().startswith("UPDATE number_sequences")


def test_parallel_allocations_never_repeat():
    org, cust, _stem = _org()
    _next(org, cust)
    got: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(6)

    def go():
        start.wait()
        n, _ = _next(org, cust)
        with lock:
            got.append(n)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(got) == 6 and len(set(got)) == 6
