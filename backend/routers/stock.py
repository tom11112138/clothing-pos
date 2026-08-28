import uuid
import hashlib
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy import update
from sqlmodel import Session, select

from database import DEFAULT_STORES, get_session
from models import (Inventory, InventoryCount, InventoryCountItem, Product, SalesOrder,
                    SalesOrderItem, Sku, StockLog, StockTransfer as TransferOrder,
                    StockTransferBatch, StockTransferItem, User)
from schemas import (InventoryCountCreate, InventoryCountFinish, InventoryCountLineUpdate,
                     SaleCreate, StockMove, StockTransfer as StockTransferCreate,
                     TransferBatchCreate, TransferBatchReceive, TransferCancel, TransferReceive)
from security import get_current_user, require_manager
from audit import add_audit_event
from time_utils import utc_iso, utc_now

router = APIRouter(prefix="/api", tags=["stock"])


def _store_value(store: Optional[str], user: Optional[User] = None) -> str:
    if user and user.role != "manager":
        if user.store:
            if store and store != user.store:
                raise HTTPException(403, "店员只能操作自己所属门店")
            return user.store
        raise HTTPException(403, "店员账号未绑定门店，不能操作库存")
    return store or DEFAULT_STORES[0]


def _validate_store(store: str) -> str:
    if store not in DEFAULT_STORES:
        raise HTTPException(400, f"门店不正确：{store}")
    return store


def _ensure_inventory(session: Session, sku_id: int, store: str) -> Inventory:
    inv = session.exec(
        select(Inventory).where(Inventory.sku_id == sku_id, Inventory.store == store)
    ).first()
    if inv:
        return inv
    sku = session.get(Sku, sku_id)
    if not sku:
        raise HTTPException(404, "SKU 不存在")
    inv = Inventory(
        sku_id=sku_id,
        store=store,
        quantity=sku.quantity if store == DEFAULT_STORES[0] else 0,
        safety_stock=sku.safety_stock,
    )
    session.add(inv)
    session.flush()
    return inv


def _adjust_stock(session: Session, sku_id: int, store: str, delta: int) -> int:
    """原子调整库存。delta 正为增、负为减。

    关键：用「条件 UPDATE」一次完成，出库时附带 quantity >= 需扣量 的条件，
    数据库层面保证不会扣成负数。返回受影响行数：
      1  -> 成功
      0  -> SKU 不存在，或（出库时）库存不足
    这样多门店/多收银台并发结算同一件商品也不会超卖，
    不再依赖「先查后改」那种有竞态的两步操作。
    """
    _ensure_inventory(session, sku_id, store)
    conds = [Inventory.sku_id == sku_id, Inventory.store == store]
    if delta < 0:
        conds.append(Inventory.quantity >= -delta)
    stmt = update(Inventory).where(*conds).values(
        quantity=Inventory.quantity + delta,
        updated_at=utc_now(),
    )
    return session.execute(stmt).rowcount


def _current_qty(session: Session, sku_id: int, store: str) -> int:
    return session.exec(
        select(Inventory.quantity).where(Inventory.sku_id == sku_id, Inventory.store == store)
    ).one()


def _safe_current_qty(session: Session, sku_id: int, store: str) -> int:
    qty = session.exec(
        select(Inventory.quantity).where(Inventory.sku_id == sku_id, Inventory.store == store)
    ).first()
    return qty if qty is not None else 0


def _stock_move_result(session: Session, log: StockLog) -> dict:
    return {
        "ok": True,
        "sku_id": log.sku_id,
        "store": log.store,
        "quantity": _current_qty(session, log.sku_id, log.store),
        "duplicate": True,
    }


def _find_stock_move_retry(
    session: Session, data: StockMove, store: str, user: User, movement_type: str,
) -> Optional[StockLog]:
    existing = session.exec(
        select(StockLog).where(StockLog.client_request_id == data.client_request_id)
    ).first()
    if not existing:
        return None
    expected_change = data.qty if movement_type == "in" else -data.qty
    if (
        existing.type != movement_type
        or existing.sku_id != data.sku_id
        or existing.store != store
        or existing.operator_user_id != user.id
        or existing.change != expected_change
    ):
        raise HTTPException(409, "该请求编号已经用于其他库存操作")
    return existing


def _sale_result(order: SalesOrder, items: list[dict]) -> dict:
    return {
        "ok": True,
        "order_no": order.order_no,
        "total": round(order.total_cents / 100, 2),
        "item_count": order.item_count,
        "items": items,
    }


def _existing_sale_result(session: Session, order: SalesOrder) -> dict:
    items = session.exec(
        select(SalesOrderItem).where(SalesOrderItem.order_id == order.id)
    ).all()
    return _sale_result(order, [
        {
            "sku_id": item.sku_id,
            "barcode": item.barcode,
            "product_name": item.product_name,
            "color": item.color,
            "size": item.size,
            "qty": item.qty,
            "price": round(item.unit_price_cents / 100, 2),
            "subtotal": round(item.subtotal_cents / 100, 2),
        }
        for item in items
    ])


@router.post("/stock/in")
def stock_in(
    data: StockMove,
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    """入库（进货）。"""
    if data.qty <= 0:
        raise HTTPException(400, "数量必须为正")
    store = _validate_store(_store_value(data.store, manager))
    existing = _find_stock_move_retry(session, data, store, manager, "in")
    if existing:
        return _stock_move_result(session, existing)
    if _adjust_stock(session, data.sku_id, store, data.qty) != 1:
        raise HTTPException(404, "SKU 不存在")
    log = StockLog(
        sku_id=data.sku_id, change=data.qty, type="in", note=data.note,
        operator=manager.username, operator_user_id=manager.id, store=store,
        client_request_id=data.client_request_id,
    )
    session.add(log)
    add_audit_event(
        session, action="stock.in", entity_type="sku", entity_id=data.sku_id,
        actor=manager, store=store, request_id=data.client_request_id,
        detail={"qty": data.qty, "note": data.note},
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = _find_stock_move_retry(session, data, store, manager, "in")
        if existing:
            return _stock_move_result(session, existing)
        raise HTTPException(409, "入库提交冲突，请刷新后重试")
    return {"ok": True, "sku_id": data.sku_id, "store": store, "quantity": _current_qty(session, data.sku_id, store)}


@router.post("/stock/out")
def stock_out(
    data: StockMove,
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    """手动出库（盘亏、报损、调拨等，非销售）。"""
    if data.qty <= 0:
        raise HTTPException(400, "数量必须为正")
    store = _validate_store(_store_value(data.store, manager))
    existing = _find_stock_move_retry(session, data, store, manager, "out")
    if existing:
        return _stock_move_result(session, existing)
    if _adjust_stock(session, data.sku_id, store, -data.qty) != 1:
        session.rollback()
        if not session.get(Sku, data.sku_id):
            raise HTTPException(404, "SKU 不存在")
        raise HTTPException(400, f"库存不足，当前仅 {_safe_current_qty(session, data.sku_id, store)}")
    log = StockLog(
        sku_id=data.sku_id, change=-data.qty, type="out", note=data.note,
        operator=manager.username, operator_user_id=manager.id, store=store,
        client_request_id=data.client_request_id,
    )
    session.add(log)
    add_audit_event(
        session, action="stock.out", entity_type="sku", entity_id=data.sku_id,
        actor=manager, store=store, request_id=data.client_request_id,
        detail={"qty": data.qty, "note": data.note},
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = _find_stock_move_retry(session, data, store, manager, "out")
        if existing:
            return _stock_move_result(session, existing)
        raise HTTPException(409, "出库提交冲突，请刷新后重试")
    return {"ok": True, "sku_id": data.sku_id, "store": store, "quantity": _current_qty(session, data.sku_id, store)}


def _transfer_view(transfer: TransferOrder, sku: Sku, product: Optional[Product]) -> dict:
    return {
        "id": transfer.id,
        "transfer_no": transfer.transfer_no,
        "sku_id": transfer.sku_id,
        "barcode": sku.barcode,
        "product_name": product.name if product else "",
        "product_code": product.code if product else None,
        "color": sku.color,
        "size": sku.size,
        "qty": transfer.qty,
        "from_store": transfer.from_store,
        "to_store": transfer.to_store,
        "status": transfer.status,
        "note": transfer.note,
        "created_by": transfer.created_by,
        "received_by": transfer.received_by,
        "cancelled_by": transfer.cancelled_by,
        "created_at": utc_iso(transfer.created_at),
        "received_at": utc_iso(transfer.received_at),
        "cancelled_at": utc_iso(transfer.cancelled_at),
    }


def _create_transfer(data: StockTransferCreate, manager: User, session: Session):
    from_store = _validate_store(data.from_store)
    to_store = _validate_store(data.to_store)
    if from_store == to_store:
        raise HTTPException(400, "调出门店和调入门店不能相同")
    existing = session.exec(select(TransferOrder).where(
        TransferOrder.client_request_id == data.client_request_id
    )).first()
    if existing:
        if (
            existing.sku_id != data.sku_id or existing.qty != data.qty
            or existing.from_store != from_store or existing.to_store != to_store
            or existing.created_by != manager.username
        ):
            raise HTTPException(409, "该请求编号已经用于其他调拨")
        sku = session.get(Sku, existing.sku_id)
        return {"ok": True, "duplicate": True,
                "transfer": _transfer_view(existing, sku, session.get(Product, sku.product_id))}
    sku = session.get(Sku, data.sku_id)
    if not sku:
        raise HTTPException(404, "SKU 不存在")
    if _adjust_stock(session, data.sku_id, from_store, -data.qty) != 1:
        session.rollback()
        raise HTTPException(400, f"调出门店库存不足，当前仅 {_safe_current_qty(session, data.sku_id, from_store)}")

    transfer = TransferOrder(
        transfer_no=f"TR{utc_now():%Y%m%d%H%M%S}{uuid.uuid4().hex[:6].upper()}",
        client_request_id=data.client_request_id,
        sku_id=data.sku_id,
        qty=data.qty,
        from_store=from_store,
        to_store=to_store,
        status="shipped",
        note=data.note,
        created_by=manager.username,
    )
    session.add(transfer)
    try:
        session.flush()
        session.add(StockLog(
            sku_id=data.sku_id, change=-data.qty, type="transfer_out",
            note=data.note or f"调拨单 {transfer.transfer_no} 已发货，货物在途",
            operator=manager.username, operator_user_id=manager.id,
            store=from_store, transfer_id=transfer.id,
        ))
        add_audit_event(
            session, action="transfer.create", entity_type="stock_transfer",
            entity_id=transfer.id, actor=manager, store=from_store,
            request_id=data.client_request_id,
            detail={"sku_id": data.sku_id, "qty": data.qty, "to_store": to_store},
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.exec(select(TransferOrder).where(
            TransferOrder.client_request_id == data.client_request_id
        )).first()
        if existing:
            if (
                existing.sku_id != data.sku_id or existing.qty != data.qty
                or existing.from_store != from_store or existing.to_store != to_store
                or existing.created_by != manager.username
            ):
                raise HTTPException(409, "该请求编号已经用于其他调拨")
            sku = session.get(Sku, existing.sku_id)
            return {"ok": True, "duplicate": True,
                    "transfer": _transfer_view(existing, sku, session.get(Product, sku.product_id))}
        raise HTTPException(409, "调拨提交冲突，请刷新后重试")
    session.refresh(transfer)
    return {"ok": True, "transfer": _transfer_view(transfer, sku, session.get(Product, sku.product_id))}


@router.post("/stock/transfers")
def create_transfer(
    data: StockTransferCreate,
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    return _create_transfer(data, manager, session)


@router.post("/stock/transfer")
def transfer_stock_compat(
    data: StockTransferCreate,
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    """Compatibility endpoint; new clients should use /stock/transfers."""
    return _create_transfer(data, manager, session)


@router.get("/stock/transfers")
def list_transfers(
    status: Optional[str] = None,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    stmt = select(TransferOrder, Sku, Product).join(Sku, TransferOrder.sku_id == Sku.id).join(
        Product, Sku.product_id == Product.id
    )
    if status:
        if status not in {"shipped", "received", "cancelled"}:
            raise HTTPException(400, "调拨状态不正确")
        stmt = stmt.where(TransferOrder.status == status)
    if user.role != "manager":
        store = _store_value(None, user)
        stmt = stmt.where((TransferOrder.from_store == store) | (TransferOrder.to_store == store))
    rows = session.exec(stmt.order_by(TransferOrder.id.desc()).limit(200)).all()
    return [_transfer_view(transfer, sku, product) for transfer, sku, product in rows]


@router.post("/stock/transfers/{transfer_id}/receive")
def receive_transfer(
    transfer_id: int,
    data: TransferReceive,
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    transfer = session.exec(
        select(TransferOrder).where(TransferOrder.id == transfer_id).with_for_update()
    ).first()
    if not transfer:
        raise HTTPException(404, "调拨单不存在")
    if transfer.status != "shipped":
        raise HTTPException(409, "该调拨单不是在途状态，不能收货")
    if _adjust_stock(session, transfer.sku_id, transfer.to_store, transfer.qty) != 1:
        session.rollback()
        raise HTTPException(404, "SKU 不存在")
    transfer.status = "received"
    transfer.received_by = manager.username
    transfer.received_at = utc_now()
    if data.note:
        transfer.note = data.note
    session.add(StockLog(
        sku_id=transfer.sku_id, change=transfer.qty, type="transfer_in",
        note=data.note or f"调拨单 {transfer.transfer_no} 已确认收货",
        operator=manager.username, operator_user_id=manager.id,
        store=transfer.to_store, transfer_id=transfer.id,
    ))
    add_audit_event(
        session, action="transfer.receive", entity_type="stock_transfer",
        entity_id=transfer.id, actor=manager, store=transfer.to_store,
        detail={"qty": transfer.qty, "from_store": transfer.from_store},
    )
    session.add(transfer)
    session.commit()
    return {"ok": True, "transfer_no": transfer.transfer_no, "status": transfer.status}


@router.post("/stock/transfers/{transfer_id}/cancel")
def cancel_transfer(
    transfer_id: int,
    data: TransferCancel,
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    transfer = session.exec(
        select(TransferOrder).where(TransferOrder.id == transfer_id).with_for_update()
    ).first()
    if not transfer:
        raise HTTPException(404, "调拨单不存在")
    if transfer.status != "shipped":
        raise HTTPException(409, "只能取消在途调拨单")
    if _adjust_stock(session, transfer.sku_id, transfer.from_store, transfer.qty) != 1:
        session.rollback()
        raise HTTPException(404, "SKU 不存在")
    transfer.status = "cancelled"
    transfer.cancelled_by = manager.username
    transfer.cancelled_at = utc_now()
    if data.note:
        transfer.note = data.note
    session.add(StockLog(
        sku_id=transfer.sku_id, change=transfer.qty, type="transfer_cancel",
        note=data.note or f"调拨单 {transfer.transfer_no} 已取消，货物退回调出店",
        operator=manager.username, operator_user_id=manager.id,
        store=transfer.from_store, transfer_id=transfer.id,
    ))
    add_audit_event(
        session, action="transfer.cancel", entity_type="stock_transfer",
        entity_id=transfer.id, actor=manager, store=transfer.from_store,
        detail={"qty": transfer.qty, "to_store": transfer.to_store},
    )
    session.add(transfer)
    session.commit()
    return {"ok": True, "transfer_no": transfer.transfer_no, "status": transfer.status}


def _batch_item_view(item: StockTransferItem, sku: Sku, product: Optional[Product]) -> dict:
    return {
        "id": item.id,
        "sku_id": item.sku_id,
        "barcode": sku.barcode,
        "product_name": product.name if product else "",
        "product_code": product.code if product else None,
        "color": sku.color,
        "size": sku.size,
        "qty": item.qty,
        "received_qty": item.received_qty,
        "rejected_qty": item.rejected_qty,
        "remaining_qty": item.qty - item.received_qty - item.rejected_qty,
    }


def _batch_view(session: Session, batch: StockTransferBatch, *, with_items: bool = True) -> dict:
    result = {
        "id": batch.id,
        "transfer_no": batch.transfer_no,
        "from_store": batch.from_store,
        "to_store": batch.to_store,
        "status": batch.status,
        "note": batch.note,
        "created_by": batch.created_by,
        "received_by": batch.received_by,
        "cancelled_by": batch.cancelled_by,
        "created_at": utc_iso(batch.created_at),
        "received_at": utc_iso(batch.received_at),
        "cancelled_at": utc_iso(batch.cancelled_at),
    }
    if with_items:
        rows = session.exec(
            select(StockTransferItem, Sku, Product)
            .join(Sku, StockTransferItem.sku_id == Sku.id)
            .join(Product, Sku.product_id == Product.id)
            .where(StockTransferItem.transfer_id == batch.id)
            .order_by(StockTransferItem.id)
        ).all()
        result["items"] = [_batch_item_view(item, sku, product) for item, sku, product in rows]
    return result


def _find_batch_retry(
    session: Session, data: TransferBatchCreate, manager: User,
    from_store: str, to_store: str, merged: dict[int, int],
) -> Optional[StockTransferBatch]:
    batch = session.exec(select(StockTransferBatch).where(
        StockTransferBatch.client_request_id == data.client_request_id
    )).first()
    if not batch:
        return None
    rows = session.exec(select(StockTransferItem).where(
        StockTransferItem.transfer_id == batch.id
    )).all()
    existing_items = {item.sku_id: item.qty for item in rows}
    if (
        batch.from_store != from_store or batch.to_store != to_store
        or batch.created_by != manager.username or existing_items != merged
    ):
        raise HTTPException(409, "该请求编号已经用于其他批量调拨")
    return batch


@router.post("/stock/transfer-batches")
def create_transfer_batch(
    data: TransferBatchCreate,
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    from_store = _validate_store(data.from_store)
    to_store = _validate_store(data.to_store)
    if from_store == to_store:
        raise HTTPException(400, "调出门店和调入门店不能相同")
    if not data.items:
        raise HTTPException(400, "调拨商品不能为空")

    merged: dict[int, int] = {}
    for item in data.items:
        merged[item.sku_id] = merged.get(item.sku_id, 0) + item.qty
    existing = _find_batch_retry(session, data, manager, from_store, to_store, merged)
    if existing:
        return {"ok": True, "duplicate": True, "transfer": _batch_view(session, existing)}
    sku_rows: dict[int, Sku] = {}
    for sku_id, qty in merged.items():
        sku = session.get(Sku, sku_id)
        if not sku:
            raise HTTPException(404, f"SKU 不存在：{sku_id}")
        if _adjust_stock(session, sku_id, from_store, -qty) != 1:
            session.rollback()
            raise HTTPException(400, f"{sku.barcode} 调出门店库存不足，当前仅 {_safe_current_qty(session, sku_id, from_store)}")
        sku_rows[sku_id] = sku

    batch = StockTransferBatch(
        transfer_no=f"TB{utc_now():%Y%m%d%H%M%S}{uuid.uuid4().hex[:6].upper()}",
        client_request_id=data.client_request_id,
        from_store=from_store,
        to_store=to_store,
        status="shipped",
        note=data.note,
        created_by=manager.username,
    )
    session.add(batch)
    try:
        session.flush()
        for sku_id, qty in merged.items():
            session.add(StockTransferItem(transfer_id=batch.id, sku_id=sku_id, qty=qty))
            session.add(StockLog(
                sku_id=sku_id, change=-qty, type="transfer_batch_out",
                note=data.note or f"批量调拨单 {batch.transfer_no} 已发货，货物在途",
                operator=manager.username, operator_user_id=manager.id,
                store=from_store, transfer_batch_id=batch.id,
            ))
        add_audit_event(
            session, action="transfer_batch.create", entity_type="stock_transfer_batch",
            entity_id=batch.id, actor=manager, store=from_store,
            request_id=data.client_request_id,
            detail={"to_store": to_store, "items": merged, "note": data.note},
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = _find_batch_retry(session, data, manager, from_store, to_store, merged)
        if existing:
            return {"ok": True, "duplicate": True, "transfer": _batch_view(session, existing)}
        raise HTTPException(409, "批量调拨提交冲突，请刷新后重试")
    session.refresh(batch)
    return {"ok": True, "transfer": _batch_view(session, batch)}


@router.get("/stock/transfer-batches")
def list_transfer_batches(
    status: Optional[str] = None,
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    stmt = select(StockTransferBatch)
    if status:
        if status not in {"shipped", "partial", "received", "cancelled"}:
            raise HTTPException(400, "调拨状态不正确")
        stmt = stmt.where(StockTransferBatch.status == status)
    batches = session.exec(stmt.order_by(StockTransferBatch.id.desc()).limit(100)).all()
    return [_batch_view(session, batch) for batch in batches]


@router.post("/stock/transfer-batches/{batch_id}/receive")
def receive_transfer_batch(
    batch_id: int,
    data: TransferBatchReceive,
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    batch = session.exec(
        select(StockTransferBatch).where(StockTransferBatch.id == batch_id).with_for_update()
    ).first()
    if not batch:
        raise HTTPException(404, "批量调拨单不存在")
    if batch.status not in {"shipped", "partial"}:
        raise HTTPException(409, "该调拨单当前不能收货")
    if not data.items:
        raise HTTPException(400, "请填写本次实收或拒收数量")
    items = session.exec(
        select(StockTransferItem).where(StockTransferItem.transfer_id == batch.id).with_for_update()
    ).all()
    item_by_id = {item.id: item for item in items}
    seen: set[int] = set()
    for receipt in data.items:
        if receipt.item_id in seen:
            raise HTTPException(400, "同一调拨商品不能重复提交")
        seen.add(receipt.item_id)
        item = item_by_id.get(receipt.item_id)
        if not item:
            raise HTTPException(400, "收货商品不属于该调拨单")
        change = receipt.received_qty + receipt.rejected_qty
        remaining = item.qty - item.received_qty - item.rejected_qty
        if change <= 0 or change > remaining:
            raise HTTPException(400, "本次实收和拒收数量必须大于 0，且不能超过在途数量")
        if receipt.received_qty:
            _adjust_stock(session, item.sku_id, batch.to_store, receipt.received_qty)
            session.add(StockLog(
                sku_id=item.sku_id, change=receipt.received_qty, type="transfer_batch_in",
                note=data.note or f"批量调拨单 {batch.transfer_no} 确认收货",
                operator=manager.username, operator_user_id=manager.id,
                store=batch.to_store, transfer_batch_id=batch.id,
            ))
        if receipt.rejected_qty:
            _adjust_stock(session, item.sku_id, batch.from_store, receipt.rejected_qty)
            session.add(StockLog(
                sku_id=item.sku_id, change=receipt.rejected_qty, type="transfer_batch_reject",
                note=data.note or f"批量调拨单 {batch.transfer_no} 拒收退回调出店",
                operator=manager.username, operator_user_id=manager.id,
                store=batch.from_store, transfer_batch_id=batch.id,
            ))
        item.received_qty += receipt.received_qty
        item.rejected_qty += receipt.rejected_qty
        session.add(item)

    accounted = sum(item.received_qty + item.rejected_qty for item in items)
    planned = sum(item.qty for item in items)
    batch.status = "received" if accounted == planned else "partial"
    batch.received_by = manager.username
    if batch.status == "received":
        batch.received_at = utc_now()
    if data.note:
        batch.note = data.note
    session.add(batch)
    add_audit_event(
        session, action="transfer_batch.receive", entity_type="stock_transfer_batch",
        entity_id=batch.id, actor=manager, store=batch.to_store,
        detail={
            "status": batch.status,
            "items": [{"item_id": item.item_id, "received_qty": item.received_qty,
                       "rejected_qty": item.rejected_qty} for item in data.items],
        },
    )
    session.commit()
    return {"ok": True, "transfer": _batch_view(session, batch)}


@router.post("/stock/transfer-batches/{batch_id}/cancel")
def cancel_transfer_batch(
    batch_id: int,
    data: TransferCancel,
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    batch = session.exec(
        select(StockTransferBatch).where(StockTransferBatch.id == batch_id).with_for_update()
    ).first()
    if not batch:
        raise HTTPException(404, "批量调拨单不存在")
    if batch.status != "shipped":
        raise HTTPException(409, "只能取消尚未收货的调拨单")
    items = session.exec(select(StockTransferItem).where(StockTransferItem.transfer_id == batch.id)).all()
    for item in items:
        _adjust_stock(session, item.sku_id, batch.from_store, item.qty)
        session.add(StockLog(
            sku_id=item.sku_id, change=item.qty, type="transfer_batch_cancel",
            note=data.note or f"批量调拨单 {batch.transfer_no} 已取消，货物退回调出店",
            operator=manager.username, operator_user_id=manager.id,
            store=batch.from_store, transfer_batch_id=batch.id,
        ))
    batch.status = "cancelled"
    batch.cancelled_by = manager.username
    batch.cancelled_at = utc_now()
    if data.note:
        batch.note = data.note
    session.add(batch)
    add_audit_event(
        session, action="transfer_batch.cancel", entity_type="stock_transfer_batch",
        entity_id=batch.id, actor=manager, store=batch.from_store,
        detail={"to_store": batch.to_store},
    )
    session.commit()
    return {"ok": True, "transfer": _batch_view(session, batch)}


def _count_view(session: Session, count: InventoryCount, *, with_items: bool = False) -> dict:
    result = {
        "id": count.id,
        "count_no": count.count_no,
        "store": count.store,
        "status": count.status,
        "note": count.note,
        "created_by": count.created_by,
        "completed_by": count.completed_by,
        "cancelled_by": count.cancelled_by,
        "created_at": utc_iso(count.created_at),
        "completed_at": utc_iso(count.completed_at),
    }
    rows = session.exec(
        select(InventoryCountItem, Sku, Product)
        .join(Sku, InventoryCountItem.sku_id == Sku.id)
        .join(Product, Sku.product_id == Product.id)
        .where(InventoryCountItem.count_id == count.id)
        .order_by(Product.code, Sku.id)
    ).all()
    result["item_count"] = len(rows)
    result["entered_count"] = sum(item.actual_qty is not None for item, _, _ in rows)
    if with_items:
        result["items"] = [{
            "id": item.id, "sku_id": item.sku_id, "barcode": sku.barcode,
            "product_name": product.name, "product_code": product.code,
            "color": sku.color, "size": sku.size, "system_qty": item.system_qty,
            "actual_qty": item.actual_qty,
            "difference": item.actual_qty - item.system_qty if item.actual_qty is not None else None,
        } for item, sku, product in rows]
    return result


@router.post("/stock/counts")
def create_inventory_count(
    data: InventoryCountCreate,
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    store = _validate_store(data.store)
    existing = session.exec(
        select(InventoryCount).where(InventoryCount.store == store, InventoryCount.status == "draft")
    ).first()
    if existing:
        raise HTTPException(409, f"{store} 已有未完成盘点单：{existing.count_no}")
    count = InventoryCount(
        count_no=f"CT{utc_now():%Y%m%d%H%M%S}{uuid.uuid4().hex[:6].upper()}",
        store=store, note=data.note, created_by=manager.username,
    )
    session.add(count)
    session.flush()
    for sku in session.exec(select(Sku).order_by(Sku.id)).all():
        inv = _ensure_inventory(session, sku.id, store)
        session.add(InventoryCountItem(
            count_id=count.id, sku_id=sku.id, system_qty=inv.quantity,
            inventory_updated_at=inv.updated_at,
        ))
    add_audit_event(
        session, action="inventory_count.create", entity_type="inventory_count",
        entity_id=count.id, actor=manager, store=store,
        detail={"note": data.note},
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, f"{store} 已有未完成盘点单")
    session.refresh(count)
    return {"ok": True, "count": _count_view(session, count, with_items=True)}


@router.get("/stock/counts")
def list_inventory_counts(
    store: Optional[str] = None,
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    stmt = select(InventoryCount)
    if store:
        stmt = stmt.where(InventoryCount.store == _validate_store(store))
    counts = session.exec(stmt.order_by(InventoryCount.id.desc()).limit(50)).all()
    return [_count_view(session, count) for count in counts]


@router.get("/stock/counts/{count_id}")
def get_inventory_count(
    count_id: int,
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    count = session.get(InventoryCount, count_id)
    if not count:
        raise HTTPException(404, "盘点单不存在")
    return _count_view(session, count, with_items=True)


@router.put("/stock/counts/{count_id}/items/{item_id}")
def update_inventory_count_item(
    count_id: int,
    item_id: int,
    data: InventoryCountLineUpdate,
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    count = session.get(InventoryCount, count_id)
    if not count:
        raise HTTPException(404, "盘点单不存在")
    if count.status != "draft":
        raise HTTPException(409, "只有草稿盘点单可以录入实盘数")
    item = session.get(InventoryCountItem, item_id)
    if not item or item.count_id != count.id:
        raise HTTPException(404, "盘点明细不存在")
    item.actual_qty = data.actual_qty
    session.add(item)
    session.commit()
    return {"ok": True, "item_id": item.id, "actual_qty": item.actual_qty}


@router.post("/stock/counts/{count_id}/complete")
def complete_inventory_count(
    count_id: int,
    data: InventoryCountFinish,
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    count = session.exec(
        select(InventoryCount).where(InventoryCount.id == count_id).with_for_update()
    ).first()
    if not count:
        raise HTTPException(404, "盘点单不存在")
    if count.status != "draft":
        raise HTTPException(409, "该盘点单已经结束")
    items = session.exec(
        select(InventoryCountItem).where(InventoryCountItem.count_id == count.id).with_for_update()
    ).all()
    unentered = [item.id for item in items if item.actual_qty is None]
    if unentered:
        raise HTTPException(400, f"还有 {len(unentered)} 个商品未录入实盘数")
    conflicts = []
    for item in items:
        inv = session.exec(
            select(Inventory).where(Inventory.sku_id == item.sku_id, Inventory.store == count.store)
        ).first()
        if not inv or inv.updated_at != item.inventory_updated_at:
            conflicts.append(item.sku_id)
    if conflicts:
        raise HTTPException(409, f"盘点期间有 {len(conflicts)} 个 SKU 发生库存变化，请新建盘点单复盘")

    now = utc_now()
    for item in items:
        delta = item.actual_qty - item.system_qty
        result = session.execute(
            update(Inventory)
            .where(
                Inventory.sku_id == item.sku_id,
                Inventory.store == count.store,
                Inventory.updated_at == item.inventory_updated_at,
            )
            .values(quantity=item.actual_qty, updated_at=now)
        )
        if result.rowcount != 1:
            session.rollback()
            raise HTTPException(409, "盘点完成时库存发生变化，请新建盘点单复盘")
        if delta:
            session.add(StockLog(
                sku_id=item.sku_id, change=delta, type="count_adjust",
                note=data.note or f"盘点单 {count.count_no} 差异调整",
                operator=manager.username, operator_user_id=manager.id,
                store=count.store, count_id=count.id,
            ))
    count.status = "completed"
    count.completed_by = manager.username
    count.completed_at = now
    if data.note:
        count.note = data.note
    session.add(count)
    add_audit_event(
        session, action="inventory_count.complete", entity_type="inventory_count",
        entity_id=count.id, actor=manager, store=count.store,
        detail={"adjusted_items": sum(1 for item in items if item.actual_qty != item.system_qty)},
    )
    session.commit()
    return {"ok": True, "count": _count_view(session, count, with_items=True)}


@router.post("/stock/counts/{count_id}/cancel")
def cancel_inventory_count(
    count_id: int,
    data: InventoryCountFinish,
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    count = session.exec(
        select(InventoryCount).where(InventoryCount.id == count_id).with_for_update()
    ).first()
    if not count:
        raise HTTPException(404, "盘点单不存在")
    if count.status != "draft":
        raise HTTPException(409, "只有盘点中的单据可以取消")
    count.status = "cancelled"
    count.cancelled_by = manager.username
    count.cancelled_at = utc_now()
    if data.note:
        count.note = data.note
    session.add(count)
    add_audit_event(
        session, action="inventory_count.cancel", entity_type="inventory_count",
        entity_id=count.id, actor=manager, store=count.store,
        detail={"note": data.note},
    )
    session.commit()
    return {"ok": True, "count": _count_view(session, count, with_items=True)}


@router.post("/sales")
def create_sale(
    data: SaleCreate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """收银结算：一单可含多件。

    流程：先把每件解析成 SKU（顺带快照名称/单价）→ 逐件原子扣减库存，
    任一件不足立即整单回滚、报错，绝不超卖 → 全部扣减成功后落一张订单
    （SalesOrder）+ 明细（SalesOrderItem）+ 销售流水（StockLog），一并提交。
    金额统一按「分」计算，避免浮点累加误差。
    """
    if not data.items:
        raise HTTPException(400, "购物清单为空")
    store = _validate_store(_store_value(data.store, user))
    existing = session.exec(
        select(SalesOrder).where(SalesOrder.client_request_id == data.client_request_id)
    ).first()
    if existing and (
        existing.store != store
        or (existing.operator_user_id is not None and existing.operator_user_id != user.id)
        or (existing.operator_user_id is None and existing.operator != user.username)
    ):
        raise HTTPException(409, "收银请求号已被其他门店或账号使用")

    # 1) 解析 + 快照（先合并同一 SKU 的重复行，数量相加）
    merged: dict = {}
    order_seq = []  # 保持录入顺序
    for item in data.items:
        if item.qty <= 0:
            raise HTTPException(400, "数量必须为正")
        if item.sku_id:
            sku = session.get(Sku, item.sku_id)
        elif item.barcode:
            sku = session.exec(select(Sku).where(Sku.barcode == item.barcode)).first()
        else:
            raise HTTPException(400, "每件商品需提供 sku_id 或 barcode")
        if not sku:
            raise HTTPException(404, f"找不到商品：{item.sku_id or item.barcode}")
        if not sku.active:
            raise HTTPException(400, f"{sku.barcode} 已停用，不能销售")
        if sku.id not in merged:
            product = session.get(Product, sku.product_id)
            merged[sku.id] = {
                "sku": sku, "product": product, "qty": 0,
                "unit_cents": round(sku.price * 100),
            }
            order_seq.append(sku.id)
        merged[sku.id]["qty"] += item.qty

    request_hash = hashlib.sha256(json.dumps(
        {"store": store, "user_id": user.id,
         "items": [[sid, merged[sid]["qty"]] for sid in sorted(merged)]},
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    if existing:
        if existing.request_hash and existing.request_hash != request_hash:
            raise HTTPException(409, "同一收银请求号的商品内容不一致")
        return _existing_sale_result(session, existing)

    # 2) 逐件原子扣减，任一不足整单回滚
    for sid in sorted(order_seq):
        row = merged[sid]
        if _adjust_stock(session, sid, store, -row["qty"]) != 1:
            session.rollback()
            raise HTTPException(400, f"{row['sku'].barcode} 库存不足，当前仅 {_safe_current_qty(session, sid, store)}")

    # 3) 落订单 + 明细 + 流水
    order_no = f"SO{utc_now():%Y%m%d%H%M%S}{uuid.uuid4().hex[:4].upper()}"
    order = SalesOrder(
        order_no=order_no,
        client_request_id=data.client_request_id,
        request_hash=request_hash,
        store=store,
        operator=user.username,
        operator_user_id=user.id,
        status="paid",
    )
    session.add(order)
    session.flush()  # 拿到 order.id

    total_cents = 0
    item_count = 0
    items_out = []
    for sid in order_seq:
        row = merged[sid]
        sku, product, qty = row["sku"], row["product"], row["qty"]
        unit_cents = row["unit_cents"]
        subtotal_cents = unit_cents * qty
        total_cents += subtotal_cents
        item_count += qty
        session.add(SalesOrderItem(
            order_id=order.id, sku_id=sku.id, barcode=sku.barcode,
            product_name=product.name if product else "",
            product_code=product.code if product else None,
            color=sku.color, size=sku.size,
            unit_price_cents=unit_cents, qty=qty, subtotal_cents=subtotal_cents,
        ))
        session.add(StockLog(sku_id=sku.id, change=-qty, type="sale",
                             note="收银销售", operator=user.username,
                             operator_user_id=user.id,
                             store=store, order_id=order.id))
        items_out.append({
            "sku_id": sku.id, "barcode": sku.barcode,
            "product_name": product.name if product else "",
            "color": sku.color, "size": sku.size,
            "qty": qty, "price": round(unit_cents / 100, 2),
            "subtotal": round(subtotal_cents / 100, 2),
        })

    order.total_cents = total_cents
    order.item_count = item_count
    add_audit_event(
        session, action="sale.create", entity_type="sales_order",
        entity_id=order.id, actor=user, store=store,
        request_id=data.client_request_id,
        detail={"item_count": item_count, "total_cents": total_cents},
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.exec(
            select(SalesOrder).where(SalesOrder.client_request_id == data.client_request_id)
        ).first()
        if existing:
            if existing.request_hash and existing.request_hash != request_hash:
                raise HTTPException(409, "同一收银请求号的商品内容不一致")
            if (
                existing.store != store
                or (existing.operator_user_id is not None and existing.operator_user_id != user.id)
                or (existing.operator_user_id is None and existing.operator != user.username)
            ):
                raise HTTPException(409, "收银请求号已被其他门店或账号使用")
            return _existing_sale_result(session, existing)
        raise HTTPException(409, "订单提交冲突，请刷新后重试")

    return _sale_result(order, items_out)


@router.get("/stock/logs")
def list_logs(
    sku_id: Optional[int] = None,
    type: Optional[str] = None,
    store: Optional[str] = None,
    limit: int = 100,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """库存流水查询。"""
    stmt = select(StockLog, Sku, Product).join(
        Sku, StockLog.sku_id == Sku.id
    ).join(Product, Sku.product_id == Product.id)
    if sku_id:
        stmt = stmt.where(StockLog.sku_id == sku_id)
    if type:
        stmt = stmt.where(StockLog.type == type)
    if user.role != "manager":
        stmt = stmt.where(StockLog.store == _store_value(None, user))
    elif store:
        stmt = stmt.where(StockLog.store == _validate_store(store))
    rows = session.exec(stmt.order_by(StockLog.id.desc()).limit(limit)).all()
    return [
        {
            "id": log.id,
            "barcode": sku.barcode,
            "product_name": product.name,
            "color": sku.color,
            "size": sku.size,
            "change": log.change,
            "type": log.type,
            "note": log.note,
            "operator": log.operator,
            "store": log.store,
            "order_id": log.order_id,
            "created_at": utc_iso(log.created_at),
        }
        for log, sku, product in rows
    ]
