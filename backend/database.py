import os
from pathlib import Path

from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine

# Ensure all SQLModel tables are registered even when init_db is called directly.
import models  # noqa: F401, E402

DEFAULT_STORES = ("1号店", "2号店", "3号店", "4号店")


def _load_dotenv() -> None:
    """Load simple KEY=VALUE pairs from backend/.env without adding a dependency."""
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# ============================================================
# 数据库连接
# ------------------------------------------------------------
# 默认 PostgreSQL，适合局域网多终端、多收银台共享同一套库存和账单。
#
# 首次部署：
#   1) 在 PostgreSQL 中创建数据库 clothing_pos。
#   2) 安装依赖：pip install -r requirements.txt
#   3) 按实际账号密码设置 DATABASE_URL：
#      Windows (PowerShell):
#        $env:DATABASE_URL="postgresql+psycopg://postgres:你的密码@127.0.0.1:5432/clothing_pos"
#      Linux/Mac:
#        export DATABASE_URL="postgresql+psycopg://postgres:你的密码@127.0.0.1:5432/clothing_pos"
#      也可以复制 .env.example 为 .env 后修改。
#   4) 正常 uvicorn 启动即可，应用会自动建表。
#
# 如需临时回到 SQLite，可设置 DATABASE_URL=sqlite:///./shop.db。
# ============================================================
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required. Run start.bat to create backend/.env.")

IS_SQLITE = DATABASE_URL.startswith("sqlite")

if IS_SQLITE:
    # SQLite 多线程需关闭 same_thread 检查
    engine = create_engine(
        DATABASE_URL, echo=False,
        connect_args={"check_same_thread": False},
    )
else:
    # PostgreSQL / 其它服务器型数据库：启用连接池 + 断线自检，
    # 适合多门店、多收银台长期并发访问。
    #
    # 连接数配比（重要）：每个 uvicorn worker 进程各有一个独立连接池，
    # 单池最多 pool_size + max_overflow = 5 + 10 = 15 条连接。
    # 即便用 --workers 4 启动：4 × 15 = 60 < PostgreSQL 默认 max_connections(100)，
    # 还留有充足余量（含 DBA/备份等占用），不会顶满。
    # 4 家店的实际并发用不到这么多，这里是安全上限。
    engine = create_engine(
        DATABASE_URL, echo=False,
        pool_pre_ping=True,    # 取连接前先 ping，自动剔除失效连接
        pool_size=10,          # 常驻连接数（单进程），给 4 店多收银台留余量
        max_overflow=20,       # 高峰可临时多开（单进程）
        pool_timeout=10,       # 连接池满时最多等待 10 秒，避免请求一直卡住
        pool_recycle=1800,     # 30 分钟回收一次，避开数据库空闲断连
    )


def init_db() -> None:
    """建表（已存在则跳过）。

    生产环境如需改表结构（加字段等），建议引入 Alembic 做版本化迁移，
    不要依赖 create_all（它不会修改已存在的表）。
    """
    SQLModel.metadata.create_all(engine)
    _ensure_runtime_schema()
    _ensure_store_inventory_rows()


def _ensure_runtime_schema() -> None:
    """Small compatibility upgrades for existing databases before Alembic is introduced."""
    with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect == "postgresql":
            conn.execute(text(
                "create unique index if not exists ux_app_user_username_lower "
                "on app_user(lower(username))"
            ))
            conn.execute(text("alter table sku add column if not exists active boolean not null default true"))
            conn.execute(text("create index if not exists ix_sku_active on sku(active)"))
            conn.execute(text("alter table auth_token add column if not exists csrf_hash varchar"))
            conn.execute(text(
                "alter table auth_token add column if not exists last_seen_at timestamp without time zone"
            ))
            conn.execute(text(
                "update auth_token set last_seen_at = coalesce(last_seen_at, created_at, now()) "
                "where last_seen_at is null"
            ))
            conn.execute(text("create index if not exists ix_auth_token_last_seen_at on auth_token(last_seen_at)"))
            conn.execute(text("alter table salesrefund add column if not exists request_hash varchar"))
            conn.execute(text(
                "create unique index if not exists ux_inventory_sku_store "
                "on inventory(sku_id, store)"
            ))
            conn.execute(text("alter table salesorder add column if not exists client_request_id varchar"))
            conn.execute(text("alter table salesorder add column if not exists request_hash varchar"))
            conn.execute(text(
                "alter table salesorder add column if not exists operator_user_id integer references app_user(id)"
            ))
            conn.execute(text("create index if not exists ix_salesorder_operator_user_id on salesorder(operator_user_id)"))
            conn.execute(text(
                "create unique index if not exists ux_salesorder_client_request_id "
                "on salesorder(client_request_id) where client_request_id is not null"
            ))
            conn.execute(text(
                "alter table stocktransferbatch add column if not exists client_request_id varchar"
            ))
            conn.execute(text(
                "create unique index if not exists ux_stocktransferbatch_client_request_id "
                "on stocktransferbatch(client_request_id) where client_request_id is not null"
            ))
            conn.execute(text("alter table stocktransfer add column if not exists client_request_id varchar"))
            conn.execute(text(
                "create unique index if not exists ux_stocktransfer_client_request_id "
                "on stocktransfer(client_request_id) where client_request_id is not null"
            ))
            conn.execute(text(
                "alter table stocklog add column if not exists operator_user_id integer references app_user(id)"
            ))
            conn.execute(text("create index if not exists ix_stocklog_operator_user_id on stocklog(operator_user_id)"))
            conn.execute(text("alter table stocklog add column if not exists client_request_id varchar"))
            conn.execute(text(
                "create unique index if not exists ux_stocklog_client_request_id "
                "on stocklog(client_request_id) where client_request_id is not null"
            ))
            conn.execute(text(
                "alter table stocklog add column if not exists transfer_id integer "
                "references stocktransfer(id)"
            ))
            conn.execute(text("create index if not exists ix_stocklog_transfer_id on stocklog(transfer_id)"))
            conn.execute(text(
                "alter table stocklog add column if not exists transfer_batch_id integer "
                "references stocktransferbatch(id)"
            ))
            conn.execute(text("create index if not exists ix_stocklog_transfer_batch_id on stocklog(transfer_batch_id)"))
            conn.execute(text(
                "alter table stocklog add column if not exists count_id integer references inventorycount(id)"
            ))
            conn.execute(text("create index if not exists ix_stocklog_count_id on stocklog(count_id)"))
            conn.execute(text(
                "create unique index if not exists ux_inventorycount_one_draft_per_store "
                "on inventorycount(store) where status = 'draft'"
            ))
            conn.execute(text("""
                do $$ begin
                    if not exists (
                        select 1 from pg_constraint where conname = 'ck_inventory_quantity_nonnegative'
                    ) then
                        alter table inventory add constraint ck_inventory_quantity_nonnegative
                        check (quantity >= 0) not valid;
                    end if;
                end $$;
            """))
        elif dialect == "sqlite":
            conn.execute(text(
                "create unique index if not exists ux_app_user_username_lower "
                "on app_user(lower(username))"
            ))
            cols = conn.execute(text("pragma table_info(salesorder)")).fetchall()
            names = {row[1] for row in cols}
            log_cols = conn.execute(text("pragma table_info(stocklog)")).fetchall()
            log_names = {row[1] for row in log_cols}
            sku_cols = conn.execute(text("pragma table_info(sku)")).fetchall()
            sku_names = {row[1] for row in sku_cols}
            auth_cols = conn.execute(text("pragma table_info(auth_token)")).fetchall()
            auth_names = {row[1] for row in auth_cols}
            refund_cols = conn.execute(text("pragma table_info(salesrefund)")).fetchall()
            refund_names = {row[1] for row in refund_cols}
            batch_cols = conn.execute(text("pragma table_info(stocktransferbatch)")).fetchall()
            batch_names = {row[1] for row in batch_cols}
            transfer_cols = conn.execute(text("pragma table_info(stocktransfer)")).fetchall()
            transfer_names = {row[1] for row in transfer_cols}
            if "active" not in sku_names:
                conn.execute(text("alter table sku add column active boolean not null default 1"))
            conn.execute(text("create index if not exists ix_sku_active on sku(active)"))
            if "csrf_hash" not in auth_names:
                conn.execute(text("alter table auth_token add column csrf_hash varchar"))
            if "last_seen_at" not in auth_names:
                conn.execute(text("alter table auth_token add column last_seen_at datetime"))
            conn.execute(text(
                "update auth_token set last_seen_at = coalesce(last_seen_at, created_at, CURRENT_TIMESTAMP) "
                "where last_seen_at is null"
            ))
            conn.execute(text("create index if not exists ix_auth_token_last_seen_at on auth_token(last_seen_at)"))
            if "request_hash" not in refund_names:
                conn.execute(text("alter table salesrefund add column request_hash varchar"))
            conn.execute(text(
                "create unique index if not exists ux_inventory_sku_store "
                "on inventory(sku_id, store)"
            ))
            if "client_request_id" not in names:
                conn.execute(text("alter table salesorder add column client_request_id varchar"))
            if "request_hash" not in names:
                conn.execute(text("alter table salesorder add column request_hash varchar"))
            if "operator_user_id" not in names:
                conn.execute(text("alter table salesorder add column operator_user_id integer"))
            conn.execute(text("create index if not exists ix_salesorder_operator_user_id on salesorder(operator_user_id)"))
            if "client_request_id" not in batch_names:
                conn.execute(text("alter table stocktransferbatch add column client_request_id varchar"))
            conn.execute(text(
                "create unique index if not exists ux_stocktransferbatch_client_request_id "
                "on stocktransferbatch(client_request_id) where client_request_id is not null"
            ))
            if "client_request_id" not in transfer_names:
                conn.execute(text("alter table stocktransfer add column client_request_id varchar"))
            conn.execute(text(
                "create unique index if not exists ux_stocktransfer_client_request_id "
                "on stocktransfer(client_request_id) where client_request_id is not null"
            ))
            if "operator_user_id" not in log_names:
                conn.execute(text("alter table stocklog add column operator_user_id integer"))
            conn.execute(text("create index if not exists ix_stocklog_operator_user_id on stocklog(operator_user_id)"))
            if "client_request_id" not in log_names:
                conn.execute(text("alter table stocklog add column client_request_id varchar"))
            conn.execute(text(
                "create unique index if not exists ux_stocklog_client_request_id "
                "on stocklog(client_request_id) where client_request_id is not null"
            ))
            if "transfer_id" not in log_names:
                conn.execute(text("alter table stocklog add column transfer_id integer"))
            conn.execute(text("create index if not exists ix_stocklog_transfer_id on stocklog(transfer_id)"))
            if "transfer_batch_id" not in log_names:
                conn.execute(text("alter table stocklog add column transfer_batch_id integer"))
            conn.execute(text("create index if not exists ix_stocklog_transfer_batch_id on stocklog(transfer_batch_id)"))
            if "count_id" not in log_names:
                conn.execute(text("alter table stocklog add column count_id integer"))
            conn.execute(text("create index if not exists ix_stocklog_count_id on stocklog(count_id)"))
            conn.execute(text(
                "create unique index if not exists ux_inventorycount_one_draft_per_store "
                "on inventorycount(store) where status = 'draft'"
            ))
            conn.execute(text(
                "create unique index if not exists ux_salesorder_client_request_id "
                "on salesorder(client_request_id) where client_request_id is not null"
            ))


def _ensure_store_inventory_rows() -> None:
    """Create missing per-store inventory rows for every SKU.

    Existing Sku.quantity is treated as the old shared stock and is placed in 1号店
    the first time a SKU is migrated. Other stores start at 0.
    """
    with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect == "postgresql":
            for store in DEFAULT_STORES:
                qty_expr = "sku.quantity" if store == DEFAULT_STORES[0] else "0"
                conn.execute(
                    text(
                        "insert into inventory (sku_id, store, quantity, safety_stock, updated_at) "
                        f"select sku.id, CAST(:store AS varchar), {qty_expr}, sku.safety_stock, now() "
                        "from sku "
                        "where not exists ("
                        "  select 1 from inventory "
                        "  where inventory.sku_id = sku.id and inventory.store = CAST(:store AS varchar)"
                        ")"
                    ),
                    {"store": store},
                )
        elif dialect == "sqlite":
            for store in DEFAULT_STORES:
                qty_expr = "sku.quantity" if store == DEFAULT_STORES[0] else "0"
                conn.execute(
                    text(
                        "insert into inventory (sku_id, store, quantity, safety_stock, updated_at) "
                        f"select sku.id, :store, {qty_expr}, sku.safety_stock, CURRENT_TIMESTAMP "
                        "from sku "
                        "where not exists ("
                        "  select 1 from inventory "
                        "  where inventory.sku_id = sku.id and inventory.store = :store"
                        ")"
                    ),
                    {"store": store},
                )


def get_session():
    """FastAPI 依赖注入用的会话生成器。"""
    with Session(engine) as session:
        yield session
