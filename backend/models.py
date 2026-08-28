from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import SQLModel, Field

from time_utils import utc_now


class Product(SQLModel, table=True):
    """SPU：一个款式。例如『纯棉圆领T恤』。
    颜色、尺码的每个组合是一个独立的 SKU（见下表）。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)          # 款号，店内唯一，如 TS001
    name: str                                           # 款式名
    category: Optional[str] = Field(default=None, index=True)  # 品类：上衣/裤装/外套...
    brand: Optional[str] = None
    tag_price: float = 0                                # 吊牌价
    image_url: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class Sku(SQLModel, table=True):
    """SKU：可销售的最小单位 = 款式 × 颜色 × 尺码。每个 SKU 有自己的条码和库存。

    实际库存以 Inventory 表为唯一业务来源。quantity/safety_stock 仅保留用于
    兼容早期数据库，新业务不可直接修改这两个旧字段。
    """
    # 同一款式下，颜色 × 尺码 不允许重复（防并发批量生成造成重复规格）。
    # 注意：color/size 为 NULL 时按 SQL 规范视为互不相同，不受此约束限制。
    __table_args__ = (
        UniqueConstraint("product_id", "color", "size", name="uq_sku_variant"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    color: Optional[str] = None
    size: Optional[str] = None
    barcode: str = Field(index=True, unique=True)       # 条码，全局唯一，扫码就靠它
    price: float = 0                                    # 实际售价
    cost: float = 0                                     # 进货成本
    quantity: int = 0                                   # 当前库存
    safety_stock: int = 0                               # 安全库存，低于此值预警
    active: bool = True                                 # 是否启用；下架/停用后不再出现在默认销售列表
    created_at: datetime = Field(default_factory=utc_now)


class Inventory(SQLModel, table=True):
    """门店库存。一个 SKU 在每家门店各有一条库存记录。"""
    __table_args__ = (
        UniqueConstraint("sku_id", "store", name="uq_inventory_sku_store"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    sku_id: int = Field(foreign_key="sku.id", index=True)
    store: str = Field(index=True)
    quantity: int = 0
    safety_stock: int = 0
    updated_at: datetime = Field(default_factory=utc_now)


class StockTransfer(SQLModel, table=True):
    """Cross-store transfer. Goods are in transit until receiving is confirmed."""
    id: Optional[int] = Field(default=None, primary_key=True)
    transfer_no: str = Field(index=True, unique=True)
    client_request_id: Optional[str] = Field(default=None, index=True, unique=True)
    sku_id: int = Field(foreign_key="sku.id", index=True)
    qty: int
    from_store: str = Field(index=True)
    to_store: str = Field(index=True)
    status: str = Field(default="shipped", index=True)
    note: Optional[str] = None
    created_by: str
    received_by: Optional[str] = None
    cancelled_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now, index=True)
    received_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None


class StockTransferBatch(SQLModel, table=True):
    """Multi-SKU transfer header. Items stay in transit until counted at destination."""
    id: Optional[int] = Field(default=None, primary_key=True)
    transfer_no: str = Field(index=True, unique=True)
    client_request_id: Optional[str] = Field(default=None, index=True, unique=True)
    from_store: str = Field(index=True)
    to_store: str = Field(index=True)
    status: str = Field(default="shipped", index=True)  # shipped / partial / received / cancelled
    note: Optional[str] = None
    created_by: str
    received_by: Optional[str] = None
    cancelled_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now, index=True)
    received_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None


class StockTransferItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    transfer_id: int = Field(foreign_key="stocktransferbatch.id", index=True)
    sku_id: int = Field(foreign_key="sku.id", index=True)
    qty: int
    received_qty: int = 0
    rejected_qty: int = 0


class InventoryCount(SQLModel, table=True):
    """Store count sheet. It keeps a snapshot to prevent overwriting later sales."""
    id: Optional[int] = Field(default=None, primary_key=True)
    count_no: str = Field(index=True, unique=True)
    store: str = Field(index=True)
    status: str = Field(default="draft", index=True)  # draft / completed / cancelled
    note: Optional[str] = None
    created_by: str
    completed_by: Optional[str] = None
    cancelled_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now, index=True)
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None


class InventoryCountItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    count_id: int = Field(foreign_key="inventorycount.id", index=True)
    sku_id: int = Field(foreign_key="sku.id", index=True)
    system_qty: int
    actual_qty: Optional[int] = None
    inventory_updated_at: datetime


class StockLog(SQLModel, table=True):
    """库存变动流水。每次入库/出库/销售/盘点都记一条，库存数才可追溯。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    sku_id: int = Field(foreign_key="sku.id", index=True)
    change: int                                         # 变动量：入库为正，出库为负
    type: str                                           # in / out / sale / adjust / return
    note: Optional[str] = None
    operator: Optional[str] = None
    operator_user_id: Optional[int] = Field(default=None, foreign_key="app_user.id", index=True)
    client_request_id: Optional[str] = Field(default=None, index=True, unique=True)
    store: Optional[str] = Field(default=None, index=True)  # 哪家门店的独立库存发生变化
    order_id: Optional[int] = Field(default=None, foreign_key="salesorder.id", index=True)  # 销售流水关联的订单
    transfer_id: Optional[int] = Field(default=None, foreign_key="stocktransfer.id", index=True)
    transfer_batch_id: Optional[int] = Field(default=None, foreign_key="stocktransferbatch.id", index=True)
    count_id: Optional[int] = Field(default=None, foreign_key="inventorycount.id", index=True)
    created_at: datetime = Field(default_factory=utc_now)


class SalesOrder(SQLModel, table=True):
    """销售单（账单）。一次收银结算 = 一张订单，对应多条明细。

    每家门店有独立库存，订单的 store 决定扣减哪家门店。
    金额统一用「分」存储，避免浮点累加误差。
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    order_no: str = Field(index=True, unique=True)      # 单号，对外展示，如 SO20260624000001
    client_request_id: Optional[str] = Field(default=None, index=True, unique=True)  # 收银端防重复提交号
    request_hash: Optional[str] = None
    store: Optional[str] = Field(default=None, index=True)  # 收银门店
    operator: Optional[str] = None                      # 收银员
    operator_user_id: Optional[int] = Field(default=None, foreign_key="app_user.id", index=True)
    item_count: int = 0                                 # 商品件数合计
    total_cents: int = 0                                # 应收合计（分）
    refunded_cents: int = 0                             # 已退金额累计（分）
    status: str = "paid"                                # paid / partial_refunded / refunded
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now, index=True)


class SalesOrderItem(SQLModel, table=True):
    """订单明细。下单时把款名/规格/单价快照下来，
    之后即便商品被改名/改价/删除，历史账单依然如实。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="salesorder.id", index=True)
    sku_id: Optional[int] = Field(default=None, foreign_key="sku.id", index=True)  # 软引用，SKU 删了也留账
    barcode: str
    product_name: str                                   # 快照
    product_code: Optional[str] = None                  # 快照
    color: Optional[str] = None                         # 快照
    size: Optional[str] = None                          # 快照
    unit_price_cents: int = 0                           # 成交单价（分），快照
    qty: int = 1
    subtotal_cents: int = 0                             # 小计（分）= 单价 × 数量
    refunded_qty: int = 0                               # 本行已退数量（支持部分退货）


class SalesRefund(SQLModel, table=True):
    """One immutable refund operation, separate from the order aggregate."""
    id: Optional[int] = Field(default=None, primary_key=True)
    refund_no: str = Field(index=True, unique=True)
    client_request_id: str = Field(index=True, unique=True)
    request_hash: str
    order_id: int = Field(foreign_key="salesorder.id", index=True)
    store: str = Field(index=True)
    operator: str
    operator_user_id: Optional[int] = Field(default=None, foreign_key="app_user.id", index=True)
    total_cents: int = 0
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now, index=True)


class SalesRefundItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    refund_id: int = Field(foreign_key="salesrefund.id", index=True)
    order_item_id: int = Field(foreign_key="salesorderitem.id", index=True)
    sku_id: Optional[int] = Field(default=None, foreign_key="sku.id", index=True)
    barcode: str
    product_name: str
    qty: int
    amount_cents: int


class User(SQLModel, table=True):
    """系统用户。角色 staff(店员) / manager(店长)。
    表名用 app_user，避开 PostgreSQL 的保留字 user。"""
    __tablename__ = "app_user"
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    name: Optional[str] = None
    role: str = "staff"                                 # staff / manager
    store: Optional[str] = None                         # 所属门店（可空）
    active: bool = True
    created_at: datetime = Field(default_factory=utc_now)


class AuthToken(SQLModel, table=True):
    """Session token digest. The raw token is only kept in an HttpOnly cookie."""
    __tablename__ = "auth_token"
    id: Optional[int] = Field(default=None, primary_key=True)
    token: str = Field(index=True, unique=True)
    csrf_hash: Optional[str] = None
    user_id: int = Field(foreign_key="app_user.id", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now, index=True)
    expires_at: Optional[datetime] = None


class LoginFailure(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username_key: str = Field(index=True)
    ip_address: str = Field(index=True)
    attempted_at: datetime = Field(default_factory=utc_now, index=True)


class AuditEvent(SQLModel, table=True):
    """Append-only record for security-sensitive and inventory-changing actions."""
    id: Optional[int] = Field(default=None, primary_key=True)
    actor_user_id: Optional[int] = Field(default=None, foreign_key="app_user.id", index=True)
    actor_username: Optional[str] = Field(default=None, index=True)
    action: str = Field(index=True)
    entity_type: str = Field(index=True)
    entity_id: Optional[int] = Field(default=None, index=True)
    store: Optional[str] = Field(default=None, index=True)
    request_id: Optional[str] = Field(default=None, index=True)
    before_json: Optional[str] = None
    after_json: Optional[str] = None
    detail_json: Optional[str] = None
    ip_address: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now, index=True)
