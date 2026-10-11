import hashlib
import json
import os
import uuid
from datetime import UTC, date as Date, datetime, timedelta
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy import case, func, literal, union_all, update
from sqlmodel import Session, select

from database import DEFAULT_STORES, get_session
from audit import add_audit_event
from models import (Inventory, SalesOrder, SalesOrderItem, SalesRefund,
                    SalesRefundItem, Sku, StockLog)
from schemas import RefundCreate
from security import get_current_user, require_manager
from models import User
from time_utils import local_date_bounds_utc, utc_iso, utc_now
from sku_lifecycle import lock_skus, set_archived

router = APIRouter(prefix="/api/orders", tags=["orders"])
BUSINESS_TIMEZONE = os.getenv("BUSINESS_TIMEZONE", "Asia/Shanghai")
PAYMENT_METHODS = {"cash": "现金", "wechat": "微信", "alipay": "支付宝", "card": "刷卡"}


def _order_store(store: Optional[str], user: User) -> Optional[str]:
    if user.role != "manager":
        if user.store not in DEFAULT_STORES:
            raise HTTPException(403, "店员账号未绑定有效门店")
        if store and store != user.store:
            raise HTTPException(403, "店员只能查看自己门店流水")
        return user.store
    if store and store not in DEFAULT_STORES:
        raise HTTPException(400, "门店不正确")
    return store or None


def _date_bounds(value: str):
    try:
        if Date.fromisoformat(value).isoformat() != value:
            raise ValueError
        return local_date_bounds_utc(value, BUSINESS_TIMEZONE)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD，且为有效日期") from exc


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
    store = _order_store(store, user)
    if store:
        stmt = stmt.where(SalesOrder.store == store)
    if keyword:
        stmt = stmt.where(SalesOrder.order_no.contains(keyword))
    if date_from:
        start, _ = _date_bounds(date_from)
        stmt = stmt.where(SalesOrder.created_at >= start)
    if date_to:
        _, end = _date_bounds(date_to)
        stmt = stmt.where(SalesOrder.created_at <= end)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(400, "开始日期不能晚于结束日期")

    matched = stmt.subquery()
    count, total, item_count = session.execute(select(
        func.count(), func.coalesce(func.sum(matched.c.total_cents), 0),
        func.coalesce(func.sum(matched.c.item_count), 0),
    ).select_from(matched)).one()
    orders = session.exec(stmt.order_by(SalesOrder.id.desc()).limit(limit)).all()
    return {
        "returned_count": len(orders), "has_more": count > len(orders),
        "summary": {
            "count": count, "total": _yuan(total), "item_count": item_count,
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
                "payment_method": o.payment_method,
                "note": o.note,
                "created_at": utc_iso(o.created_at),
            }
            for o in orders
        ],
    }


def _turnover_period(period: str, value: Optional[str]):
    today = datetime.now(ZoneInfo(BUSINESS_TIMEZONE)).date()
    value = value or (today.isoformat() if period == "day" else today.strftime("%Y-%m"))
    try:
        if period == "day":
            first = Date.fromisoformat(value)
            if first.isoformat() != value:
                raise ValueError
            end = first + timedelta(days=1)
        elif period == "month":
            first = Date.fromisoformat(value + "-01")
            if first.isoformat()[:7] != value:
                raise ValueError
            end = Date(first.year + 1, 1, 1) if first.month == 12 else Date(first.year, first.month + 1, 1)
        else:
            raise ValueError
    except (ValueError, OverflowError) as exc:
        raise HTTPException(400, "请选择有效日期（日流水 YYYY-MM-DD，月流水 YYYY-MM）") from exc
    days = [first + timedelta(days=i) for i in range((end - first).days)]
    try:
        boundaries = [local_date_bounds_utc(day.isoformat(), BUSINESS_TIMEZONE)[0] for day in days + [end]]
    except (ValueError, OverflowError) as exc:
        raise HTTPException(400, "日期超出可用范围") from exc
    return value, days, boundaries


def _empty_turnover(**identity):
    return {**identity, "sales_cents": 0, "refund_cents": 0, "net_cents": 0,
            "order_count": 0, "refund_count": 0, "item_count": 0}


@router.get("/turnover")
def get_turnover(
    period: Literal["day", "month"] = "day",
    date: Optional[str] = None,
    store: Optional[str] = None,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Cash-flow periods use event time and actual sale/refund store, never list limits."""
    store = _order_store(store, user)
    value, days, boundaries = _turnover_period(period, date)
    visible_stores = [store] if store else list(DEFAULT_STORES)
    method = case((SalesOrder.payment_method.in_(list(PAYMENT_METHODS)), SalesOrder.payment_method),
                  else_="unrecorded")
    sales = select(
        SalesOrder.store.label("store"), SalesOrder.created_at.label("created_at"),
        method.label("payment_method"), SalesOrder.total_cents.label("sales_cents"),
        literal(0).label("refund_cents"), literal(1).label("order_count"),
        literal(0).label("refund_count"), SalesOrder.item_count.label("item_count"),
    ).where(SalesOrder.store.in_(visible_stores),
            SalesOrder.created_at >= boundaries[0], SalesOrder.created_at < boundaries[-1])
    refunds = select(
        SalesRefund.store, SalesRefund.created_at, method,
        literal(0), SalesRefund.total_cents, literal(0), literal(1), literal(0),
    ).join(SalesOrder, SalesOrder.id == SalesRefund.order_id).where(
        SalesRefund.store.in_(visible_stores),
        SalesRefund.created_at >= boundaries[0], SalesRefund.created_at < boundaries[-1],
    )
    events = union_all(sales, refunds).subquery()
    # UTC range comparisons work on both databases, including DST business days.
    business_date = case(*[
        (events.c.created_at < boundaries[i + 1], day.isoformat())
        for i, day in enumerate(days)
    ], else_=days[-1].isoformat())
    keys = ("sales_cents", "refund_cents", "order_count", "refund_count", "item_count")
    rows = session.execute(select(
        events.c.store, events.c.payment_method, business_date.label("date"),
        *[func.sum(events.c[key]).label(key) for key in keys],
    ).group_by(events.c.store, events.c.payment_method, business_date)).mappings().all()
    summary = _empty_turnover()
    stores = {name: _empty_turnover(store=name) for name in visible_stores}
    methods = {code: _empty_turnover(payment_method=code, name=name)
               for code, name in {**PAYMENT_METHODS, "unrecorded": "未记录（历史订单）"}.items()}
    daily = {day.isoformat(): _empty_turnover(date=day.isoformat()) for day in days}
    for row in rows:
        for target in (summary, stores[row["store"]], methods[row["payment_method"]], daily[row["date"]]):
            for key in keys:
                target[key] += int(row[key] or 0)
            target["net_cents"] = target["sales_cents"] - target["refund_cents"]
    return {
        "period": period, "date": value, "store": store, "timezone": BUSINESS_TIMEZONE,
        "date_from": days[0].isoformat(), "date_to": days[-1].isoformat(),
        "generated_at": utc_iso(datetime.now(UTC)),
        "summary": summary, "stores": list(stores.values()),
        "payments": list(methods.values()), "days": list(daily.values()),
    }


@router.get("/{order_id}")
def get_order(order_id: int, user: User = Depends(get_current_user),
              session: Session = Depends(get_session)):
    """单张账单明细（用于查看 / 重打小票）。"""
    order = session.get(SalesOrder, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    allowed_store = _order_store(None, user)
    if allowed_store and order.store != allowed_store:
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
        "payment_method": order.payment_method,
        "note": order.note,
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

    resolved_items = []
    for key_type, key_value in requested:
        item = by_sku.get(key_value) if key_type == "sku" else by_barcode.get(key_value)
        if item is None:
            raise HTTPException(400, f"该订单不含商品：{key_value}")
        resolved_items.append(item)
    locked_skus = lock_skus(session, [item.sku_id for item in resolved_items if item.sku_id is not None],
                            allow_archived=True)

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
    for sku in locked_skus:
        if sku.archived_at is not None:
            set_archived(session, sku, None, manager, request_id=data.client_request_id,
                         note=f"历史订单 {order.order_no} 退货入库，自动恢复为停用")
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
