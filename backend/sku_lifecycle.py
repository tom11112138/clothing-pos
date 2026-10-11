from fastapi import HTTPException
from sqlalchemy import update
from sqlmodel import select

from audit import add_audit_event
from database import DEFAULT_STORES
from models import (Inventory, InventoryCount, InventoryCountItem, SalesOrderItem,
                    SalesRefundItem, Sku, StockLog, StockTransfer, StockTransferBatch,
                    StockTransferItem)
from time_utils import utc_iso


def lock_skus(session, sku_ids, *, allow_archived=False, require_active=False):
    """All inventory writers take SKU locks in the same order before inventory locks."""
    ids = sorted(set(sku_ids))
    if not ids:
        return []
    rows = session.exec(select(Sku).where(Sku.id.in_(ids)).order_by(Sku.id)
                        .with_for_update().execution_options(populate_existing=True)).all()
    if len(rows) != len(ids):
        raise HTTPException(404, "SKU 不存在或已被删除")
    for sku in rows:
        if sku.archived_at is not None and not allow_archived:
            raise HTTPException(409, f"{sku.barcode} 已归档，请先在归档商品中恢复")
        if require_active and not sku.active:
            raise HTTPException(400, f"{sku.barcode} 已停用，不能销售")
    return rows


def lifecycle_status(session, sku, *, lock_inventory=False):
    stmt = select(Inventory).where(Inventory.sku_id == sku.id).order_by(Inventory.store)
    if lock_inventory:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    rows = session.exec(stmt).all()
    quantities = {row.store: row.quantity for row in rows}
    for store in DEFAULT_STORES:
        quantities.setdefault(store, sku.quantity if store == DEFAULT_STORES[0] and sku.archived_at is None else 0)
    archive_reasons = [f"{store} 仍有库存 {qty} 件" for store, qty in quantities.items() if qty != 0]
    in_transit = session.exec(select(StockTransfer.id).where(
        StockTransfer.sku_id == sku.id, StockTransfer.status == "shipped"
    )).first()
    batch_pending = session.exec(select(StockTransferItem.id).join(
        StockTransferBatch, StockTransferItem.transfer_id == StockTransferBatch.id
    ).where(StockTransferItem.sku_id == sku.id,
            StockTransferBatch.status.in_(("shipped", "partial")),
            StockTransferItem.qty > StockTransferItem.received_qty + StockTransferItem.rejected_qty)).first()
    if in_transit is not None or batch_pending is not None:
        archive_reasons.append("仍有在途调拨或待收货商品，请先完成收货或退回")
    draft_count = session.exec(select(InventoryCount.count_no).join(
        InventoryCountItem, InventoryCountItem.count_id == InventoryCount.id
    ).where(InventoryCountItem.sku_id == sku.id, InventoryCount.status == "draft")).first()
    if draft_count:
        archive_reasons.append(f"仍在未完成盘点单 {draft_count} 中，请先完成或取消盘点")
    delete_reasons = list(archive_reasons)
    for model, label in ((StockLog, "库存流水"), (SalesOrderItem, "销售历史"),
                         (SalesRefundItem, "退货历史"), (StockTransfer, "调拨记录"),
                         (StockTransferItem, "批量调拨记录"), (InventoryCountItem, "盘点记录")):
        if session.exec(select(model.id).where(model.sku_id == sku.id)).first() is not None:
            delete_reasons.append(f"已有{label}，必须保留原 SKU 和条码")
    if sku.archived_at is not None:
        archive_reasons.insert(0, "该 SKU 已归档")
        delete_reasons.insert(0, "该 SKU 已归档，不能直接永久删除")
    return {
        "version": sku.version, "active": sku.active, "archived_at": utc_iso(sku.archived_at),
        "inventories": [{"store": store, "quantity": qty} for store, qty in quantities.items()],
        "can_archive": not archive_reasons, "archive_reasons": archive_reasons,
        "can_delete": not delete_reasons, "delete_reasons": delete_reasons,
        "can_restore": sku.archived_at is not None,
    }


def set_archived(session, sku, archived_at, actor, *, request_id=None, note=None):
    before = {"active": sku.active, "archived_at": utc_iso(sku.archived_at), "version": sku.version}
    result = session.execute(update(Sku).where(Sku.id == sku.id, Sku.version == sku.version)
                             .values(active=False, archived_at=archived_at, version=sku.version + 1)
                             .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise HTTPException(409, "SKU 已被其他操作修改，请重新加载核对")
    session.refresh(sku)
    add_audit_event(session, action="sku.archive" if archived_at is not None else "sku.restore",
                    entity_type="sku", entity_id=sku.id, actor=actor, request_id=request_id,
                    before=before, after={"active": sku.active, "archived_at": utc_iso(sku.archived_at),
                                          "version": sku.version}, detail={"reason": note})
