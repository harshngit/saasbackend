"""scripts/audit_confirmed_purchase_returns.py on an in-memory database."""

import csv
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, event, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.database import Base  # noqa: E402
from app.models import (  # noqa: E402
    Organization, Product, PurchaseInvoice, PurchaseInvoiceItem, StockMovement, Supplier, Warehouse,
    WarehouseStock,
)
from app.models.purchase_return import PurchaseReturn, PurchaseReturnItem  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


audit = _load("audit_confirmed_purchase_returns")


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    event.listen(engine, "connect", lambda c, _r: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def world(db):
    org = Organization(name="Audit Firm", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}")
    db.add(org)
    db.flush()
    sup = Supplier(organization_id=org.id, name="Coastal Beverages", is_active=True)
    main = Warehouse(organization_id=org.id, name="Main", code="MAIN", is_default=True, is_active=True)
    other = Warehouse(organization_id=org.id, name="Other", code="OTH", is_active=True)
    db.add_all([sup, main, other])
    db.flush()
    return {"org": org, "sup": sup, "main": main, "other": other}


def _product(db, w, name, counter):
    p = Product(organization_id=w["org"].id, name=name, sku=f"S-{uuid.uuid4().hex[:6]}", total_inventory=counter,
                is_active=True)
    db.add(p)
    db.flush()
    return p


def _stock(db, w, wh, product, qty):
    db.add(WarehouseStock(organization_id=w["org"].id, warehouse_id=wh.id, product_id=product.id, variant_id=None,
                          on_hand_quantity=qty))
    db.flush()


def _return(db, w, number, status, lines, warehouse=None, movements=True, movement_qty=None, org=None):
    """lines: [(product, qty)]. movements: write the confirm's stock_movements rows."""
    org = org or w["org"]
    pi = PurchaseInvoice(organization_id=org.id, invoice_number=f"PO-{uuid.uuid4().hex[:5]}", status="approved")
    db.add(pi)
    db.flush()
    wh = warehouse if warehouse is not None else w["main"]
    pr = PurchaseReturn(organization_id=org.id, return_number=number, purchase_id=pi.id, supplier_id=w["sup"].id,
                        warehouse_id=wh.id, status=status)
    for product, qty in lines:
        pr.items.append(PurchaseReturnItem(product_id=product.id, product_name=product.name, quantity=qty,
                                           unit_price=10, line_total=10 * qty))
        if movements:
            db.add(StockMovement(organization_id=org.id, warehouse_id=wh.id, product_id=product.id,
                                 movement_type="purchase_return", quantity=-(movement_qty or qty), balance_after=0,
                                 note=f"Return {number} on {pi.invoice_number} — damaged"))
    db.add(pr)
    db.flush()
    return pr


def _by_return(lines):
    out = {}
    for line in lines:
        out.setdefault(line["return_number"], []).append(line)
    return out


def test_correct_return_has_no_flags(db, world):
    p = _product(db, world, "Good", 6)
    _stock(db, world, world["main"], p, 6)  # 10 received, 4 returned through the warehouse
    _return(db, world, "PR-1", "confirmed", [(p, 4)])
    [line] = audit.audit_lines(db)
    assert line["flags"] == "" and line["movement_found"] == "yes" and line["movement_qty"] == "4"
    assert line["warehouse_on_hand_now"] == "6" and line["counter_now"] == 6


def test_old_confirm_path_counter_moved_but_warehouse_not(db, world):
    """The pattern the old code produced: product tracked in another warehouse, return
    from Main (no row there): counter went 20 -> 15, warehouses still hold 20."""
    p = _product(db, world, "Electronics", 15)
    _stock(db, world, world["other"], p, 20)
    _return(db, world, "PR-2", "confirmed", [(p, 5)])
    [line] = audit.audit_lines(db)
    assert set(line["flags"].split(";")) == {"WAREHOUSE_NOT_TRACKED", "COUNTER_DISAGREES"}
    assert line["warehouse_on_hand_now"] == "" and line["all_warehouses_on_hand_now"] == "20"
    assert line["counter_now"] == 15


def test_untracked_item_only_flags_the_warehouse(db, world):
    p = _product(db, world, "Untracked", 5)  # no warehouse rows anywhere: the counter is the only figure
    _return(db, world, "PR-3", "dispatched", [(p, 5)])
    [line] = audit.audit_lines(db)
    assert line["flags"] == "WAREHOUSE_NOT_TRACKED" and line["all_warehouses_on_hand_now"] == ""


def test_missing_and_wrong_movements_are_flagged(db, world):
    p = _product(db, world, "P", 10)
    _stock(db, world, world["main"], p, 10)
    _return(db, world, "PR-4", "completed", [(p, 3)], movements=False)
    _return(db, world, "PR-5", "confirmed", [(p, 3)], movement_qty=2)
    lines = _by_return(audit.audit_lines(db))
    assert lines["PR-4"][0]["flags"] == "NO_STOCK_MOVEMENT" and lines["PR-4"][0]["movement_found"] == "no"
    assert lines["PR-5"][0]["flags"] == "MOVEMENT_QTY_MISMATCH"


def test_only_returns_whose_stock_left_and_cancel_notes_are_not_matches(db, world):
    p = _product(db, world, "P", 10)
    _stock(db, world, world["main"], p, 10)
    _return(db, world, "PR-6", "draft", [(p, 1)], movements=False)
    _return(db, world, "PR-7", "cancelled", [(p, 1)], movements=False)
    pr = _return(db, world, "PR-8", "confirmed", [(p, 1)], movements=False)
    db.add(StockMovement(organization_id=world["org"].id, warehouse_id=world["main"].id, product_id=p.id,
                         movement_type="purchase_return", quantity=1, balance_after=0,
                         note=f"Cancelled Return {pr.return_number} on PO"))
    # "PR-80" must not be taken for "PR-8"
    db.add(StockMovement(organization_id=world["org"].id, warehouse_id=world["main"].id, product_id=p.id,
                         movement_type="purchase_return", quantity=-1, balance_after=0, note="Return PR-80 on PO"))
    db.flush()
    lines = _by_return(audit.audit_lines(db))
    assert set(lines) == {"PR-8"} and lines["PR-8"][0]["flags"] == "NO_STOCK_MOVEMENT"


def test_organization_filter(db, world):
    p = _product(db, world, "P", 10)
    _stock(db, world, world["main"], p, 10)
    _return(db, world, "PR-9", "confirmed", [(p, 1)])
    other = Organization(name="Other Firm", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}")
    db.add(other)
    db.flush()
    assert [r["return_number"] for r in audit.audit_lines(db, world["org"].id)] == ["PR-9"]
    assert audit.audit_lines(db, other.id) == []


def test_run_audit_writes_csvs_and_changes_nothing(db, world, tmp_path):
    good = _product(db, world, "Good", 6)
    _stock(db, world, world["main"], good, 6)
    bad = _product(db, world, "Bad", 15)
    _stock(db, world, world["other"], bad, 20)
    _return(db, world, "PR-10", "confirmed", [(good, 4)])
    _return(db, world, "PR-11", "confirmed", [(good, 1), (bad, 5)])
    db.commit()

    def snapshot():
        return [db.scalar(select(func.count()).select_from(m)) for m in (StockMovement, WarehouseStock)] + [
            db.get(Product, bad.id).total_inventory]

    before = snapshot()
    counts = audit.run_audit(db, tmp_path)
    assert snapshot() == before
    assert counts == {audit.FILE_LINES: 3, audit.FILE_FLAGGED: 1}
    with (tmp_path / audit.FILE_LINES).open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == audit.LINE_COLUMNS
    with (tmp_path / audit.FILE_FLAGGED).open(encoding="utf-8") as fh:
        [flagged] = list(csv.DictReader(fh))
    assert flagged["return_number"] == "PR-11" and flagged["lines"] == "2" and flagged["flagged_lines"] == "1"
    assert flagged["flags"] == "COUNTER_DISAGREES;WAREHOUSE_NOT_TRACKED"
    assert audit.flag_counts(rows) == {"COUNTER_DISAGREES": 1, "WAREHOUSE_NOT_TRACKED": 1}


def test_the_new_confirm_code_produces_no_flags(db, world, monkeypatch):
    """A return confirmed by the fixed service passes the audit cleanly."""
    from app.services import purchase_return_service, stock_service
    from app.schemas.purchase_return import PurchaseReturnCreate

    monkeypatch.setattr(stock_service, "queue_event", lambda *a, **k: None, raising=False)
    p = _product(db, world, "Wire", 0)
    pi = PurchaseInvoice(organization_id=world["org"].id, invoice_number="PO-NEW", status="approved",
                         supplier_id=world["sup"].id, warehouse_id=world["main"].id, total=1000)
    pi.items.append(PurchaseInvoiceItem(product_id=p.id, product_name=p.name, quantity=10, received_qty=10,
                                        purchase_price=10, line_total=100))
    db.add(pi)
    stock_service.adjust_on_hand(db, world["org"].id, world["main"].id, p.id, None, 10.0, "purchase_in")
    db.commit()
    ret = purchase_return_service.create_purchase_return(
        db, world["org"].id, PurchaseReturnCreate(purchase_id=pi.id, items=[{"product_id": p.id, "quantity": 4}]))
    purchase_return_service.confirm_purchase_return(db, world["org"].id, ret.id)
    [line] = audit.audit_lines(db)
    assert line["flags"] == "" and line["movement_qty"] == "4" and line["warehouse_on_hand_now"] == "6"
    assert line["counter_now"] == 6


def test_cli_writes_both_files_and_prints_counts(tmp_path, monkeypatch, capsys):
    import app.core.database as database

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    assert audit.main(["--out-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "read-only audit" in out and "0 rows" in out
    assert (tmp_path / audit.FILE_LINES).exists() and (tmp_path / audit.FILE_FLAGGED).exists()
