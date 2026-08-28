import hashlib
import json
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy import update
from sqlmodel import Session, select

from database import DEFAULT_STORES, get_session
from audit import add_audit_event
from models import (Inventory, SalesOrder, SalesOrderItem, SalesRefund,
                    SalesRefundItem, Sku, StockLog)
from schemas import RefundCreate
from security import get_current_user, require_manager
from models import User
from time_utils import local_date_bounds_utc, utc_iso, utc_now

router = APIRouter(prefix="/api/orders", tags=["orders"])
BUSINESS_TIMEZONE = os.getenv("BUSINESS_TIMEZONE", "Asia/Shanghai")


def _yuan(cents: int) -> float:
    return round((cents or 0) / 100, 2)


@router.get("")
def list_orders(
    store: Optional[str] = None,
    keyword: Optional[str] = None,        # 按单号模糊查
    date_from: Optional[str] = None,      # YYYY-MM-DD
    date_to: Optional[str] = None,        # YYYY-MM-DD（含当天）
    limit: int = Query(default=200, ge=1, le=500),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """账单流水：销售单列表，支持按门店 / 单号 / 日期区间筛选。"""
    stmt = select(SalesOrder)
    if user.role != "manager":
        if not user.store:
            raise HTTPException(403, "店员账号未绑定门店")
        stmt = stmt.where(SalesOrder.store == user.store)
    elif store:
        if store not in DEFAULT_STORES:
            raise HTTPException(400, "门店不正确")
        stmt = stmt.where(SalesOrder.store == store)
    if keyword:
        stmt = stmt.where(SalesOrder.order_no.contains(keyword))
    if date_from:
        start, _ = local_date_bounds_utc(date_from, BUSINESS_TIMEZONE)
        stmt = stmt.where(SalesOrder.created_at >= start)
    if date_to:
        _, end = local_date_bounds_utc(date_to, BUSINESS_TIMEZONE)
        stmt = stmt.where(SalesOrder.created_at <= end)

    orders = session.exec(stmt.order_by(SalesOrder.id.desc()).limit(limit)).all()
    return {
        "summary": {
            "count": len(orders),
            "total": _yuan(sum(o.total_cents for o in orders)),
            "item_count": sum(o.item_count for o in orders),
        },
        "orders": [
            {
                "id": o.id,
                "order_no": o.order_no,
                "store": o.store,
                "operator": o.operator,
                "item_count": o.item_count,
                "total": _yuan(o.total_cents),
                "status": o.status,
                "created_at": utc_iso(o.created_at),
            }
            for o in orders
        ],
    }


@router.get("/{order_id}")
def get_order(order_id: int, user: User = Depends(get_current_user),
              session: Session = Depends(get_session)):
    """单张账单明细（用于查看 / 重打小票）。"""
    order = session.get(SalesOrder, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    if user.role != "manager" and order.store != user.store:
        raise HTTPException(403, "店员只能查看自己门店订单")
    items = session.exec(
        select(SalesOrderItem).where(SalesOrderItem.order_id == order_id)
    ).all()
    return {
        "id": order.id,
        "order_no": order.order_no,
        "store": order.store,
        "operator": order.operator,
        "item_count": order.item_count,
        "total": _yuan(order.total_cents),
        "refunded": _yuan(order.refunded_cents),
        "status": order.status,
        "created_at": utc_iso(order.created_at),
        "items": [
            {
                "sku_id": it.sku_id,
                "barcode": it.barcode,
                "product_name": it.product_name,
                "product_code": it.product_code,
                "color": it.color,
                "size": it.size,
                "price": _yuan(it.unit_price_cents),
                "qty": it.qty,
                "refunded_qty": it.refunded_qty,
                "returnable": it.qty - it.refunded_qty,
                "subtotal": _yuan(it.subtotal_cents),
            }
            for it in items
        ],
    }


def _refund_result(session: Session, refund: SalesRefund, order: SalesOrder,
                   *, duplicate: bool = False) -> dict:
    rows = session.exec(
        select(SalesRefundItem).where(SalesRefundItem.refund_id == refund.id)
    ).all()
    return {
        "ok": True,
        "duplicate": duplicate,
        "refund_no": refund.refund_no,
        "order_no": order.order_no,
        "refund_total": _yuan(refund.total_cents),
        "status": order.status,
        "items": [{
            "barcode": row.barcode, "product_name": row.product_name,
            "qty": row.qty, "amount": _yuan(row.amount_cents),
        } for row in rows],
    }


@router.post("/{order_id}/refund")
def refund_order(order_id: int, data: RefundCreate,
                 manager: User = Depends(require_manager),
                 session: Session = Depends(get_session)):
    """Create an immutable, idempotent refund and atomically restore inventory."""
    order = session.exec(
        select(SalesOrder).where(SalesOrder.id == order_id).with_for_update()
    ).first()
    if not order:
        raise HTTPException(404, "订单不存在")
    return_store = data.store or order.store or DEFAULT_STORES[0]
    if return_store not in DEFAULT_STORES:
        raise HTTPException(400, "退货门店不正确")

    requested: dict[tuple[str, object], int] = {}
    for row in data.items:
        key = ("sku", row.sku_id) if row.sku_id is not None else ("barcode", row.barcode)
        if key[1] is None:
            raise HTTPException(400, "退货商品必须提供 sku_id 或条码")
        requested[key] = requested.get(key, 0) + row.qty
    request_hash = hashlib.sha256(json.dumps(
        {"order_id": order_id, "store": return_store,
         "items": [[kind, str(value), qty]
                   for (kind, value), qty in sorted(requested.items(), key=lambda item: str(item[0]))]},
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()

    existing = session.exec(select(SalesRefund).where(
        SalesRefund.client_request_id == data.client_request_id
    )).first()
    if existing:
        if (
            existing.order_id != order_id or existing.operator_user_id != manager.id
            or existing.request_hash != request_hash
        ):
            raise HTTPException(409, "该请求编号已经用于其他退款")
        session.refresh(order)
        return _refund_result(session, existing, order, duplicate=True)

    items = session.exec(
        select(SalesOrderItem).where(SalesOrderItem.order_id == order_id)
    ).all()
    by_sku = {item.sku_id: item for item in items if item.sku_id is not None}
    by_barcode = {item.barcode: item for item in items}

    refund = SalesRefund(
        refund_no=f"RF{utc_now():%Y%m%d%H%M%S}{uuid.uuid4().hex[:6].upper()}",
        client_request_id=data.client_request_id,
        request_hash=request_hash,
        order_id=order.id,
        store=return_store,
        operator=manager.username,
        operator_user_id=manager.id,
        note=data.note,
    )
    session.add(refund)
    session.flush()

    refund_cents = 0
    for (key_type, key_value), qty in requested.items():
        order_item = by_sku.get(key_value) if key_type == "sku" else by_barcode.get(key_value)
        if order_item is None:
            raise HTTPException(400, f"该订单不含商品：{key_value}")
        returnable = order_item.qty - order_item.refunded_qty
        if qty > returnable:
            raise HTTPException(400, f"{order_item.barcode} 可退数量仅 {returnable}")

        line_res = session.execute(
            update(SalesOrderItem)
            .where(
                SalesOrderItem.id == order_item.id,
                SalesOrderItem.refunded_qty <= SalesOrderItem.qty - qty,
            )
            .values(refunded_qty=SalesOrderItem.refunded_qty + qty)
            .execution_options(synchronize_session=False)
        )
        if line_res.rowcount != 1:
            session.rollback()
            raise HTTPException(409, f"{order_item.barcode} 可退数量已变化，请刷新后重试")

        if order_item.sku_id is not None:
            inv = session.exec(select(Inventory).where(
                Inventory.sku_id == order_item.sku_id,
                Inventory.store == return_store,
            )).first()
            if not inv:
                inv = Inventory(
                    sku_id=order_item.sku_id, store=return_store,
                    quantity=0, safety_stock=0,
                )
                session.add(inv)
                session.flush()
            res = session.execute(
                update(Inventory)
                .where(Inventory.sku_id == order_item.sku_id, Inventory.store == return_store)
                .values(quantity=Inventory.quantity + qty, updated_at=utc_now())
            )
            if res.rowcount == 1:
                session.add(StockLog(
                    sku_id=order_item.sku_id, change=qty, type="return",
                    note=data.note or f"退款单 {refund.refund_no}；原销售门店：{order.store}",
                    operator=manager.username, operator_user_id=manager.id,
                    store=return_store, order_id=order.id,
                ))

        line_cents = order_item.unit_price_cents * qty
        refund_cents += line_cents
        session.add(SalesRefundItem(
            refund_id=refund.id, order_item_id=order_item.id,
            sku_id=order_item.sku_id, barcode=order_item.barcode,
            product_name=order_item.product_name, qty=qty,
            amount_cents=line_cents,
        ))

    session.flush()
    session.expire_all()
    fresh_items = session.exec(
        select(SalesOrderItem).where(SalesOrderItem.order_id == order_id)
    ).all()
    total_q = sum(item.qty for item in fresh_items)
    total_refunded = sum(item.refunded_qty for item in fresh_items)
    order.refunded_cents = sum(item.unit_price_cents * item.refunded_qty for item in fresh_items)
    order.status = "refunded" if total_refunded >= total_q else "partial_refunded"
    refund.total_cents = refund_cents
    session.add(order)
    session.add(refund)
    add_audit_event(
        session, action="refund.create", entity_type="sales_refund",
        entity_id=refund.id, actor=manager, store=return_store,
        request_id=data.client_request_id,
        detail={"order_id": order.id, "total_cents": refund_cents},
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.exec(select(SalesRefund).where(
            SalesRefund.client_request_id == data.client_request_id
        )).first()
        if existing and existing.order_id == order_id and existing.request_hash == request_hash:
            fresh_order = session.get(SalesOrder, order_id)
            return _refund_result(session, existing, fresh_order, duplicate=True)
        raise HTTPException(409, "退款提交冲突，请刷新后重试")

    return _refund_result(session, refund, order)
