from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from database import DEFAULT_STORES, get_session
from models import Inventory, Product, SalesOrderItem, Sku, StockLog, User
from schemas import ProductCreate, ProductUpdate, ProductLabelUpdate, ProductIntake, validate_money
from label_standards import LABEL_FIELDS, label_configuration, label_issues, normalize_category, normalize_standard, standards_catalog
from security import require_manager
from audit import add_audit_event
from management_operations import begin_operation, finish_operation
from routers.skus import _next_unique_barcode

router = APIRouter(prefix="/api/products", tags=["products"])


@router.post("", response_model=Product)
def create_product(data: ProductCreate, manager: User = Depends(require_manager),
                   session: Session = Depends(get_session)):
    # 先查重给出友好提示；并发下两个请求可能同时通过这步，
    # 故再用数据库唯一约束兜底（下方捕获 IntegrityError），双保险。
    values = data.model_dump()
    values["code"] = values["code"].strip()
    values["name"] = values["name"].strip()
    values["category"] = normalize_category(values.get("category"))
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


@router.get("/label-catalog")
def get_label_catalog():
    return standards_catalog()


@router.post("/intake")
def intake_product(data: ProductIntake, manager: User = Depends(require_manager),
                   session: Session = Depends(get_session)):
    if data.store not in DEFAULT_STORES:
        raise HTTPException(400, "请选择有效的初始入库门店")
    values = data.product.model_dump()
    values["code"], values["name"] = values["code"].strip(), values["name"].strip()
    values["category"] = normalize_category(values["category"])
    if not values["code"] or not values["name"]:
        raise HTTPException(400, "款号和品名不能为空")
    try:
        validate_money(values["tag_price"])
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    config = {key: (value.strip() or None) if isinstance(value, str) else value
              for key, value in data.label.model_dump(exclude={"expected_version"}).items()}
    config["execution_standard"] = normalize_standard(config["execution_standard"]) or None
    issues = label_issues(config)
    if config["label_verified"] and issues:
        raise HTTPException(400, "；".join(issues))
    pairs = [(item.color, item.size) for item in data.variants]
    if len(pairs) != len(set(pairs)):
        raise HTTPException(400, "颜色和尺码组合不能重复")
    payload = {"product": values, "label": config, "store": data.store,
               "variants": [item.model_dump() for item in data.variants]}
    try:
        operation, replay = begin_operation(session, data.client_request_id, "product.intake", manager, payload)
        if replay is not None:
            return replay
        if session.exec(select(Product.id).where(Product.code == values["code"])).first():
            raise HTTPException(409, "款号已存在，请更换款号或为已有款式追加规格")
        product = Product(**values, **config)
        session.add(product)
        session.flush()
        items = []
        for variant in data.variants:
            sku = Sku(product_id=product.id, color=variant.color, size=variant.size,
                      barcode=_next_unique_barcode(session, product), price=variant.price,
                      cost=variant.cost, safety_stock=variant.safety_stock)
            session.add(sku)
            session.flush()
            for store in DEFAULT_STORES:
                session.add(Inventory(sku_id=sku.id, store=store,
                                      quantity=variant.quantity if store == data.store else 0,
                                      safety_stock=variant.safety_stock))
            if variant.quantity:
                session.add(StockLog(sku_id=sku.id, store=data.store, type="in",
                                     change=variant.quantity, note="新款初始入库",
                                     operator_user_id=manager.id))
            items.append({"id": sku.id, "barcode": sku.barcode, **variant.model_dump()})
        add_audit_event(session, action="product.intake", entity_type="product", entity_id=product.id,
                        actor=manager, store=data.store, request_id=data.client_request_id,
                        after={**values, **config, "store": data.store, "items": items},
                        detail={"client_request_id": data.client_request_id})
        result = {"ok": True, "product_id": product.id, "code": product.code,
                  "store": data.store, "created_count": len(items), "items": items}
        finish_operation(session, operation, result)
        session.commit()
        return result
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "款号、规格或条码冲突，本次未录入任何商品，请检查后重试")
    except Exception:
        session.rollback()
        raise


@router.get("/{product_id}", response_model=Product)
def get_product(product_id: int, session: Session = Depends(get_session)):
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(404, "商品不存在")
    return product


@router.put("/{product_id}/label", response_model=Product)
def update_product_label(product_id: int, data: ProductLabelUpdate,
                         manager: User = Depends(require_manager), session: Session = Depends(get_session)):
    product = session.exec(select(Product).where(Product.id == product_id)
                           .with_for_update().execution_options(populate_existing=True)).first()
    if not product:
        raise HTTPException(404, "商品不存在")
    if product.label_version != data.expected_version:
        session.rollback()
        raise HTTPException(409, "标签资料已被其他人修改，请重新打开标签后再保存")
    config = {key: (value.strip() or None) if isinstance(value, str) else value
              for key, value in data.model_dump(exclude={"expected_version"}).items()}
    config["execution_standard"] = normalize_standard(config["execution_standard"]) or None
    issues = label_issues(config)
    if config["label_verified"] and issues:
        session.rollback()
        raise HTTPException(400, "；".join(issues))
    before = {**label_configuration(product), "label_version": product.label_version}
    try:
        for key in LABEL_FIELDS:
            setattr(product, key, config[key])
        product.label_version += 1
        session.add(product)
        add_audit_event(session, action="product.label.update", entity_type="product", entity_id=product.id,
                        actor=manager, before=before,
                        after={**label_configuration(product), "label_version": product.label_version})
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(product)
    return product


@router.put("/{product_id}", response_model=Product)
def update_product(
    product_id: int, data: ProductUpdate, manager: User = Depends(require_manager),
    session: Session = Depends(get_session)
):
    product = session.exec(select(Product).where(Product.id == product_id)
                           .with_for_update().execution_options(populate_existing=True)).first()
    if not product:
        raise HTTPException(404, "商品不存在")
    before = {"name": product.name, "category": product.category,
              "brand": product.brand, "tag_price": product.tag_price,
              "image_url": product.image_url, "label_verified": product.label_verified,
              "label_version": product.label_version}
    values = data.model_dump(exclude_unset=True)
    if "category" in values:
        values["category"] = normalize_category(values["category"])
    if any(key in values and values[key] != getattr(product, key) for key in ("name", "category")):
        product.label_verified = False
        product.label_version += 1
    for key, value in values.items():
        if isinstance(value, str):
            value = value.strip()
        setattr(product, key, value)
    session.add(product)
    add_audit_event(
        session, action="product.update", entity_type="product",
        entity_id=product.id, actor=manager, before=before,
        after={"name": product.name, "category": product.category,
               "brand": product.brand, "tag_price": product.tag_price,
               "image_url": product.image_url, "label_verified": product.label_verified,
               "label_version": product.label_version},
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
