from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from database import get_session
from models import Inventory, Product, SalesOrderItem, Sku, StockLog, User
from schemas import ProductCreate, ProductUpdate
from security import require_manager
from audit import add_audit_event

router = APIRouter(prefix="/api/products", tags=["products"])


@router.post("", response_model=Product)
def create_product(data: ProductCreate, manager: User = Depends(require_manager),
                   session: Session = Depends(get_session)):
    # 先查重给出友好提示；并发下两个请求可能同时通过这步，
    # 故再用数据库唯一约束兜底（下方捕获 IntegrityError），双保险。
    values = data.model_dump()
    values["code"] = values["code"].strip()
    values["name"] = values["name"].strip()
    if session.exec(select(Product).where(Product.code == values["code"])).first():
        raise HTTPException(400, f"款号 {values['code']} 已存在")
    product = Product(**values)
    session.add(product)
    try:
        session.flush()
        add_audit_event(
            session, action="product.create", entity_type="product",
            entity_id=product.id, actor=manager,
            after={"code": product.code, "name": product.name,
                   "category": product.category, "brand": product.brand,
                   "tag_price": product.tag_price},
        )
        session.commit()
    except IntegrityError:
        # 并发下另一个请求抢先建了同款号，唯一约束拦下了本次
        session.rollback()
        raise HTTPException(400, f"款号 {values['code']} 已存在（并发提交）")
    session.refresh(product)
    return product


@router.get("", response_model=List[Product])
def list_products(
    keyword: Optional[str] = None,
    category: Optional[str] = None,
    session: Session = Depends(get_session),
):
    stmt = select(Product)
    if keyword:
        stmt = stmt.where(
            Product.name.contains(keyword) | Product.code.contains(keyword)
        )
    if category:
        stmt = stmt.where(Product.category == category)
    return session.exec(stmt.order_by(Product.id.desc())).all()


@router.get("/{product_id}", response_model=Product)
def get_product(product_id: int, session: Session = Depends(get_session)):
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    return product


@router.put("/{product_id}", response_model=Product)
def update_product(
    product_id: int, data: ProductUpdate, manager: User = Depends(require_manager),
    session: Session = Depends(get_session)
):
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    before = {"name": product.name, "category": product.category,
              "brand": product.brand, "tag_price": product.tag_price,
              "image_url": product.image_url}
    for key, value in data.model_dump(exclude_unset=True).items():
        if isinstance(value, str):
            value = value.strip()
        setattr(product, key, value)
    session.add(product)
    add_audit_event(
        session, action="product.update", entity_type="product",
        entity_id=product.id, actor=manager, before=before,
        after={"name": product.name, "category": product.category,
               "brand": product.brand, "tag_price": product.tag_price,
               "image_url": product.image_url},
    )
    session.commit()
    session.refresh(product)
    return product


@router.delete("/{product_id}")
def delete_product(product_id: int, manager: User = Depends(require_manager),
                   session: Session = Depends(get_session)):
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    # 收银/库存系统必须保留历史流水。已有库存变动或销售记录时，不做物理删除。
    skus = session.exec(select(Sku).where(Sku.product_id == product_id)).all()
    for sku in skus:
        has_logs = session.exec(select(StockLog.id).where(StockLog.sku_id == sku.id)).first()
        has_sales = session.exec(select(SalesOrderItem.id).where(SalesOrderItem.sku_id == sku.id)).first()
        inventory_rows = session.exec(select(Inventory).where(Inventory.sku_id == sku.id)).all()
        total_quantity = sum(inv.quantity for inv in inventory_rows)
        if total_quantity != 0 or has_logs or has_sales:
            raise HTTPException(
                400,
                "该款式已有库存或销售历史，不能直接删除；请保留历史账目，必要时改名为停用/下架。",
            )
        for inv in inventory_rows:
            session.delete(inv)
        session.delete(sku)
    add_audit_event(
        session, action="product.delete", entity_type="product",
        entity_id=product.id, actor=manager,
        before={"code": product.code, "name": product.name},
    )
    session.delete(product)
    session.commit()
    return {"ok": True}
