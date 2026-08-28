import os
import sys
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from models import AuthToken, Product, SalesOrder, SalesOrderItem, Sku, StockLog, User


TABLES = [
    Product,
    Sku,
    StockLog,
    SalesOrder,
    SalesOrderItem,
    User,
    AuthToken,
]


def load_dotenv() -> None:
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    load_dotenv()
    sqlite_url = os.getenv("SQLITE_URL", "sqlite:///./shop.db")
    postgres_url = os.getenv("DATABASE_URL")
    if not postgres_url or not postgres_url.startswith("postgresql"):
        print("Please set DATABASE_URL to your PostgreSQL connection string.")
        return 1

    sqlite_engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
    postgres_engine = create_engine(postgres_url, pool_pre_ping=True)
    SQLModel.metadata.create_all(postgres_engine)

    with Session(sqlite_engine) as source, Session(postgres_engine) as target:
        for model in TABLES:
            rows = source.exec(select(model)).all()
            for row in rows:
                target.merge(row)
            target.commit()
            print(f"Migrated {len(rows)} rows: {model.__tablename__}")

    print("Migration finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
