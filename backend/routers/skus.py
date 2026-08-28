from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from database import DEFAULT_STORES, get_session
from models import Inventory, Product, SalesOrderItem, Sku, StockLog, User
from schemas import SkuCreate, SkuBatchCreate, SkuUpdate
from barcode_utils import generate_barcode_png, make_sku_barcode
from security import get_current_user, require_manager
from audit import add_audit_event

router = APIRouter(prefix="/api", tags=["skus"])


def _store_for_user(store: Optional[str], user: User, *, allow_all: bool = False) -> str:
    if user.role != "manager":
        if user.store not in DEFAULT_STORES:
            raise HTTPException(403, "店员账号未绑定有效门店")
        if store and store != user.store:
            raise HTTPException(403, "店员只能查看自己门店库存")
        return user.store
    value = store or DEFAULT_STORES[0]
    if value == "all" and allow_all:
        return value
    if value not in DEFAULT_STORES:
        raise HTTPException(400, "门店不正确")
    return value


def _ensure_inventory_rows(session: Session, sku: Sku) -> None:
    for store in DEFAULT_STORES:
        exists = session.exec(
            select(Inventory).where(Inventory.sku_id == sku.id, Inventory.store == store)
        ).first()
        if not exists:
            session.add(Inventory(
                sku_id=sku.id,
                store=store,
                quantity=sku.quantity if store == DEFAULT_STORES[0] else 0,
                safety_stock=sku.safety_stock,
            ))


class _VariantExists(Exception):
    """该款的「颜色×尺码」组合已存在（含并发下别处刚建好的）。"""


def _next_unique_barcode(session: Session, product: Product) -> str:
    """按规则生成一个当前未被占用的条码。"""
    # 该款已有多少 SKU，序号从已有数量+1 开始，再往后找空位
    seq = session.exec(select(Sku).where(Sku.product_id == product.id)).all()
    n = len(seq) + 1
    while True:
        code = make_sku_barcode(product.code, n)
        if not session.exec(select(Sku).where(Sku.barcode == code)).first():
            return code
        n += 1


def _insert_sku_with_retry(session: Session, product: Product, *, color, size,
                           price, cost, quantity, safety_stock,
                           active: bool = True,
                           actor: Optional[User] = None,
                           explicit_barcode: Optional[str] = None,
                           max_tries: int = 5) -> Sku:
    """插入一个 SKU，并发安全地处理唯一约束冲突：
      - 自动条码撞车（两店同时给同款生成）：重算下一个序号后重试；
      - 颜色×尺码组合已存在：抛 _VariantExists，交给调用方决定报错或跳过；
      - 指定条码已被占用：友好报错。
    数据库唯一约束是最终防线，这里把冲突转成清晰的结果。
    """
    color = color.strip() if isinstance(color, str) else color
    size = size.strip() if isinstance(size, str) else size
    explicit_barcode = explicit_barcode.strip() if explicit_barcode else None
    for _ in range(max_tries):
        barcode_value = explicit_barcode or _next_unique_barcode(session, product)
        sku = Sku(
            product_id=product.id, color=color, size=size, barcode=barcode_value,
            price=price, cost=cost, quantity=quantity, safety_stock=safety_stock,
            active=active,
        )
        session.add(sku)
        try:
            session.flush()
            _ensure_inventory_rows(session, sku)
            add_audit_event(
                session, action="sku.create", entity_type="sku", entity_id=sku.id,
                actor=actor,
                after={"product_id": product.id, "barcode": sku.barcode,
                       "color": sku.color, "size": sku.size, "price": sku.price,
                       "cost": sku.cost, "initial_store": DEFAULT_STORES[0],
                       "initial_quantity": sku.quantity},
            )
            session.commit()
            session.refresh(sku)
            return sku
        except IntegrityError:
            session.rollback()
            # 1) 规格重复（仅当颜色、尺码都非空时唯一约束才会触发）
            if color is not None and size is not None:
                dup = session.exec(select(Sku).where(
                    Sku.product_id == product.id,
                    Sku.color == color, Sku.size == size,
                )).first()
                if dup:
                    raise _VariantExists()
            # 2) 指定条码已被占用
            if explicit_barcode:
                if session.exec(select(Sku).where(Sku.barcode == explicit_barcode)).first():
                    raise HTTPException(400, f"条码 {explicit_barcode} 已存在")
                raise HTTPException(400, "创建失败：数据冲突")
            # 3) 否则是自动条码序号撞车 -> 重算重试
    raise HTTPException(409, "条码生成冲突，请重试")


def _inventory_for(session: Session, sku: Sku, store: str) -> Inventory:
    inv = session.exec(
        select(Inventory).where(Inventory.sku_id == sku.id, Inventory.store == store)
    ).first()
    if inv:
        return inv
    inv = Inventory(
        sku_id=sku.id,
        store=store,
        quantity=sku.quantity if store == DEFAULT_STORES[0] else 0,
        safety_stock=sku.safety_stock,
    )
    session.add(inv)
    session.commit()
    session.refresh(inv)
    return inv


def _sku_view(sku: Sku, product: Product, *, quantity: int, safety_stock: int, store: str) -> dict:
    """组装前端需要的展示结构：SKU + 所属款式信息 + 低库存标记。"""
    return {
        "id": sku.id,
        "product_id": sku.product_id,
        "product_code": product.code if product else None,
        "product_name": product.name if product else None,
        "category": product.category if product else None,
        "color": sku.color,
        "size": sku.size,
        "barcode": sku.barcode,
        "price": sku.price,
        "cost": sku.cost,
        "quantity": quantity,
        "safety_stock": safety_stock,
        "store": store,
        "active": sku.active,
        "low_stock": quantity <= safety_stock,
    }


# ---------- 创建 ----------
@router.post("/products/{product_id}/skus", response_model=Sku)
def create_sku(
    product_id: int, data: SkuCreate, manager: User = Depends(require_manager),
    session: Session = Depends(get_session)
):
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    # 指定条码先查重给友好提示（数据库唯一约束兜底）
    if data.barcode and session.exec(select(Sku).where(Sku.barcode == data.barcode)).first():
        raise HTTPException(400, f"条码 {data.barcode} 已存在")
    try:
        return _insert_sku_with_retry(
            session, product, color=data.color, size=data.size,
            price=data.price, cost=data.cost, quantity=data.quantity,
            safety_stock=data.safety_stock, active=data.active,
            actor=manager,
            explicit_barcode=data.barcode,
        )
    except _VariantExists:
        raise HTTPException(400, f"该规格已存在：{data.color or '-'}/{data.size or '-'}")


@router.post("/products/{product_id}/skus/batch", response_model=List[Sku])
def batch_create_skus(
    product_id: int, data: SkuBatchCreate, manager: User = Depends(require_manager),
    session: Session = Depends(get_session)
):
    """颜色 × 尺码 笛卡尔积批量建 SKU，条码自动生成。
    已存在的组合自动跳过；并发下别处刚建好的也会被唯一约束拦下并安全跳过。"""
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")

    existing = {
        (s.color, s.size)
        for s in session.exec(select(Sku).where(Sku.product_id == product_id)).all()
    }
    created: List[Sku] = []
    for color in data.colors:
        for size in data.sizes:
            if (color, size) in existing:
                continue
            try:
                sku = _insert_sku_with_retry(
                    session, product, color=color, size=size,
                    price=data.price, cost=data.cost, quantity=data.quantity,
                    safety_stock=data.safety_stock,
                    actor=manager,
                )
                created.append(sku)
            except _VariantExists:
                pass  # 并发下别处已建该规格，跳过即可
            existing.add((color, size))
    return created


# ---------- 查询（固定路径放在 /skus/{id} 之前）----------
@router.get("/skus")
def list_skus(
    keyword: Optional[str] = None,
    product_id: Optional[int] = None,
    store: Optional[str] = None,
    low_stock: bool = False,
    include_inactive: bool = False,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """库存查询主接口：支持关键词（款名/款号/条码）、按款式、低库存筛选。"""
    stmt = select(Sku, Product).join(Product, Sku.product_id == Product.id)
    if not include_inactive or user.role != "manager":
        stmt = stmt.where(Sku.active == True)  # noqa: E712
    if product_id:
        stmt = stmt.where(Sku.product_id == product_id)
    if keyword:
        stmt = stmt.where(
            Product.name.contains(keyword)
            | Product.code.contains(keyword)
            | Sku.barcode.contains(keyword)
        )
    rows = session.exec(stmt.order_by(Sku.id.desc())).all()
    store_value = _store_for_user(store, user, allow_all=True)
    result = []
    for sku, product in rows:
        if store_value == "all":
            inventories = session.exec(
                select(Inventory).where(Inventory.sku_id == sku.id)
            ).all()
            quantity = sum(inv.quantity for inv in inventories)
            safety_stock = sum(inv.safety_stock for inv in inventories)
            result.append(_sku_view(
                sku, product, quantity=quantity, safety_stock=safety_stock, store="all",
            ))
        else:
            inv = _inventory_for(session, sku, store_value)
            result.append(_sku_view(
                sku, product, quantity=inv.quantity, safety_stock=inv.safety_stock, store=store_value,
            ))
    if low_stock:
        result = [r for r in result if r["low_stock"]]
    return result


@router.get("/skus/by-barcode/{barcode}")
def get_by_barcode(
    barcode: str,
    store: Optional[str] = None,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """扫码核心：扫描枪/摄像头拿到条码，查出对应 SKU。"""
    sku = session.exec(select(Sku).where(Sku.barcode == barcode)).first()
    if not sku:
        raise HTTPException(404, f"未找到条码 {barcode} 对应的商品")
    if not sku.active:
        raise HTTPException(400, f"条码 {barcode} 对应的 SKU 已停用，不能销售")
    product = session.get(Product, sku.product_id)
    store_value = _store_for_user(store, user)
    inv = _inventory_for(session, sku, store_value)
    return _sku_view(sku, product, quantity=inv.quantity, safety_stock=inv.safety_stock, store=store_value)


@router.get("/skus/{sku_id}/barcode.png")
def sku_barcode_image(sku_id: int, session: Session = Depends(get_session)):
    """返回该 SKU 的条形码图片，可直接 <img> 显示或打印。"""
    sku = session.get(Sku, sku_id)
    if not sku:
        raise HTTPException(404, "SKU 不存在")
    before = {"color": sku.color, "size": sku.size, "price": sku.price,
              "cost": sku.cost, "safety_stock": sku.safety_stock,
              "active": sku.active}
    png = generate_barcode_png(sku.barcode)
    return Response(content=png, media_type="image/png")


@router.get("/skus/{sku_id}")
def get_sku(sku_id: int, store: Optional[str] = None,
            user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    sku = session.get(Sku, sku_id)
    if not sku:
        raise HTTPException(404, "SKU 不存在")
    product = session.get(Product, sku.product_id)
    store_value = _store_for_user(store, user)
    inv = _inventory_for(session, sku, store_value)
    return _sku_view(sku, product, quantity=inv.quantity, safety_stock=inv.safety_stock, store=store_value)


# ---------- 修改 / 删除 ----------
@router.put("/skus/{sku_id}", response_model=Sku)
def update_sku(
    sku_id: int,
    data: SkuUpdate,
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    sku = session.get(Sku, sku_id)
    if not sku:
        raise HTTPException(404, "SKU 不存在")
    # 注意：库存数量不在这里直接改，应走入库/出库接口以保留流水
    for key, value in data.model_dump(exclude_unset=True).items():
        if isinstance(value, str):
            value = value.strip()
        setattr(sku, key, value)
    session.add(sku)
    add_audit_event(
        session, action="sku.update", entity_type="sku", entity_id=sku.id,
        actor=manager, before=before,
        after={"color": sku.color, "size": sku.size, "price": sku.price,
               "cost": sku.cost, "safety_stock": sku.safety_stock,
               "active": sku.active},
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(400, "SKU 规格或条码已存在，不能重复")
    session.refresh(sku)
    return sku


@router.delete("/skus/{sku_id}")
def delete_sku(sku_id: int, manager: User = Depends(require_manager),
               session: Session = Depends(get_session)):
    sku = session.get(Sku, sku_id)
    if not sku:
        raise HTTPException(404, "SKU 不存在")
    has_logs = session.exec(select(StockLog.id).where(StockLog.sku_id == sku_id)).first()
    has_sales = session.exec(select(SalesOrderItem.id).where(SalesOrderItem.sku_id == sku_id)).first()
    inventory_rows = session.exec(select(Inventory).where(Inventory.sku_id == sku_id)).all()
    total_quantity = sum(inv.quantity for inv in inventory_rows)
    if total_quantity != 0 or has_logs or has_sales:
        raise HTTPException(
            400,
            "该 SKU 已有库存或销售历史，不能直接删除；请保留历史账目，必要时将款式标记为停用/下架。",
        )
    for inv in inventory_rows:
        session.delete(inv)
    add_audit_event(
        session, action="sku.delete", entity_type="sku", entity_id=sku.id,
        actor=manager,
        before={"barcode": sku.barcode, "product_id": sku.product_id,
                "color": sku.color, "size": sku.size},
    )
    session.delete(sku)
    session.commit()
    return {"ok": True}
