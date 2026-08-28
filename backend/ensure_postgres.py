import os
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse


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


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def build_maintenance_url(database_url: str) -> tuple[str, str]:
    database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    parsed = urlparse(database_url)
    db_name = parsed.path.lstrip("/")
    if not db_name:
        raise ValueError("DATABASE_URL must include a database name.")
    maintenance = parsed._replace(path="/postgres")
    return urlunparse(maintenance), db_name


def main() -> int:
    load_dotenv()
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url.startswith("postgresql"):
        print("DATABASE_URL is not PostgreSQL; skipped database creation.")
        return 0

    try:
        import psycopg
    except ImportError:
        print("Missing psycopg. Run: pip install -r requirements.txt")
        return 1

    maintenance_url, db_name = build_maintenance_url(database_url)
    try:
        with psycopg.connect(maintenance_url, autocommit=True) as conn:
            exists = conn.execute(
                "select 1 from pg_database where datname = %s",
                (db_name,),
            ).fetchone()
            if exists:
                print(f"PostgreSQL database already exists: {db_name}")
                return 0
            conn.execute(f"create database {quote_ident(db_name)}")
            print(f"Created PostgreSQL database: {db_name}")
            return 0
    except Exception as exc:
        print("")
        print("Could not prepare PostgreSQL automatically.")
        print("Please check that PostgreSQL is installed, running, and that DATABASE_URL in .env is correct.")
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
