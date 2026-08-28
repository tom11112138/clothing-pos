import os
from pathlib import Path
from urllib.parse import urlparse


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
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url.startswith("postgresql"):
        print("DATABASE_URL must start with postgresql+psycopg://")
        return 1

    try:
        import psycopg
    except ImportError:
        print("Missing psycopg. Run start.bat again to install dependencies.")
        return 1

    psycopg_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        with psycopg.connect(psycopg_url, connect_timeout=5) as conn:
            conn.execute("select 1").fetchone()
        parsed = urlparse(psycopg_url)
        print(f"Database connection OK: {parsed.hostname}:{parsed.port}{parsed.path}")
        return 0
    except Exception as exc:
        print("")
        print("Could not connect to PostgreSQL.")
        print("Most common causes:")
        print("1. PostgreSQL is not running.")
        print("2. The database username or password in backend\\.env is wrong.")
        print("3. The PostgreSQL user does not exist.")
        print("4. Port 5432 is not the PostgreSQL port.")
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
