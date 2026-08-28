from typing import Optional, List

from sqlmodel import Field, SQLModel


# ---------- Product ----------
class ProductCreate(SQLModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    category: Optional[str] = Field(default=None, max_length=80)
    brand: Optional[str] = Field(default=None, max_length=80)
    tag_price: float = Field(default=0, ge=0)
    image_url: Optional[str] = Field(default=None, max_length=500)


class ProductUpdate(SQLModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    category: Optional[str] = Field(default=None, max_length=80)
    brand: Optional[str] = Field(default=None, max_length=80)
    tag_price: Optional[float] = Field(default=None, ge=0)
    image_url: Optional[str] = Field(default=None, max_length=500)


# ---------- SKU ----------
class SkuCreate(SQLModel):
    color: Optional[str] = Field(default=None, max_length=80)
    size: Optional[str] = Field(default=None, max_length=40)
    barcode: Optional[str] = Field(default=None, max_length=80)  # 不传则自动生成
    price: float = Field(default=0, ge=0)
    cost: float = Field(default=0, ge=0)
    quantity: int = Field(default=0, ge=0)
    safety_stock: int = Field(default=0, ge=0)
    active: bool = True


class SkuBatchCreate(SQLModel):
    """批量生成 SKU：颜色 × 尺码 的笛卡尔积。服装店录款的刚需。"""
    colors: List[str] = Field(min_length=1, max_length=50)
    sizes: List[str] = Field(min_length=1, max_length=50)
    price: float = Field(default=0, ge=0)
    cost: float = Field(default=0, ge=0)
    quantity: int = Field(default=0, ge=0)
    safety_stock: int = Field(default=0, ge=0)


class SkuUpdate(SQLModel):
    color: Optional[str] = Field(default=None, max_length=80)
    size: Optional[str] = Field(default=None, max_length=40)
    price: Optional[float] = Field(default=None, ge=0)
    cost: Optional[float] = Field(default=None, ge=0)
    safety_stock: Optional[int] = Field(default=None, ge=0)
    active: Optional[bool] = None


# ---------- 库存变动 / 收银 ----------
class StockMove(SQLModel):
    sku_id: int
    qty: int = Field(gt=0)             # 正整数，方向由接口决定
    note: Optional[str] = Field(default=None, max_length=500)
    client_request_id: str = Field(min_length=16, max_length=80)
    store: Optional[str] = None        # 操作门店


class StockTransfer(SQLModel):
    sku_id: int
    qty: int = Field(gt=0)
    from_store: str = Field(min_length=1)
    to_store: str = Field(min_length=1)
    note: Optional[str] = Field(default=None, max_length=500)
    client_request_id: str = Field(min_length=16, max_length=80)


class TransferReceive(SQLModel):
    note: Optional[str] = Field(default=None, max_length=500)


class TransferCancel(SQLModel):
    note: Optional[str] = Field(default=None, max_length=500)


class TransferBatchItemCreate(SQLModel):
    sku_id: int
    qty: int = Field(gt=0)


class TransferBatchCreate(SQLModel):
    from_store: str = Field(min_length=1)
    to_store: str = Field(min_length=1)
    items: List[TransferBatchItemCreate] = Field(min_length=1, max_length=500)
    note: Optional[str] = Field(default=None, max_length=500)
    client_request_id: str = Field(min_length=16, max_length=80)


class TransferBatchReceiveItem(SQLModel):
    item_id: int
    received_qty: int = Field(default=0, ge=0)
    rejected_qty: int = Field(default=0, ge=0)


class TransferBatchReceive(SQLModel):
    items: List[TransferBatchReceiveItem] = Field(min_length=1, max_length=500)
    note: Optional[str] = Field(default=None, max_length=500)


class InventoryCountCreate(SQLModel):
    store: str = Field(min_length=1)
    note: Optional[str] = Field(default=None, max_length=500)


class InventoryCountLineUpdate(SQLModel):
    actual_qty: int = Field(ge=0)


class InventoryCountFinish(SQLModel):
    note: Optional[str] = Field(default=None, max_length=500)


class SaleItem(SQLModel):
    barcode: Optional[str] = Field(default=None, max_length=80)
    sku_id: Optional[int] = None       # 或直接用 sku_id
    qty: int = Field(default=1, gt=0)


class SaleCreate(SQLModel):
    items: List[SaleItem] = Field(min_length=1, max_length=100)
    client_request_id: str = Field(min_length=16, max_length=80)
    store: Optional[str] = None        # 收银门店


# ---------- 退货 ----------
class RefundCreate(SQLModel):
    """按单退货：传要退的明细（条码或 sku_id + 数量）。整单退就把每行都按可退数量传。"""
    items: List[SaleItem] = Field(min_length=1, max_length=100)
    client_request_id: str = Field(min_length=16, max_length=80)
    store: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=500)


# ---------- 鉴权 / 账号 ----------
class LoginRequest(SQLModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserCreate(SQLModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=10, max_length=128)
    name: Optional[str] = Field(default=None, max_length=80)
    role: str = "staff"                # staff / manager
    store: Optional[str] = None


class UserUpdate(SQLModel):
    name: Optional[str] = Field(default=None, max_length=80)
    role: Optional[str] = None
    store: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=10, max_length=128)
