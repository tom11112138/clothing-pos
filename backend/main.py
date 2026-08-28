import os

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from database import engine, init_db
from models import User
from routers import audit_events, auth, products, skus, stock, orders
from security import get_current_user, hash_password

app = FastAPI(title="线下服装店收银库存管理系统")

# 局域网多终端：收银机/库存机用浏览器访问后端，开放跨域方便调试。
# 上线可把 allow_origins 收紧为内网网段对应的地址。
cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

# 登录相关：开放，不需要令牌
app.include_router(auth.router)
# 账号管理：路由自带店长权限校验
app.include_router(auth.users_router)
# 业务接口：统一要求已登录（携带有效令牌）。
# 其中"删除商品/SKU、退货"等敏感操作在各自路由内再要求店长权限。
_auth = [Depends(get_current_user)]
app.include_router(products.router, dependencies=_auth)
app.include_router(skus.router, dependencies=_auth)
app.include_router(stock.router, dependencies=_auth)
app.include_router(orders.router, dependencies=_auth)
app.include_router(audit_events.router, dependencies=_auth)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.on_event("startup")
def on_startup():
    init_db()
    _seed_default_admin()


def _seed_default_admin():
    """首次启动且无任何用户时，建一个默认店长账号，方便首次登录。
    生产环境请用 ADMIN_USERNAME / ADMIN_PASSWORD 设置强密码，或设置 SEED_ADMIN=false 关闭自动创建。
    """
    if os.getenv("SEED_ADMIN", "true").lower() in {"0", "false", "no"}:
        return
    with Session(engine) as session:
        if session.exec(select(User)).first():
            return
        username = os.getenv("ADMIN_USERNAME", "admin").strip()
        password = os.getenv("ADMIN_PASSWORD", "")
        display_name = os.getenv("ADMIN_NAME", "超级管理员")
        if len(password) < 10:
            raise RuntimeError("首次启动的 ADMIN_PASSWORD 至少需要 10 个字符")
        session.add(User(
            username=username, password_hash=hash_password(password),
            name=display_name, role="manager",
        ))
        session.commit()
        print(f">>> 已创建初始管理员账号：{username}（请妥善保管密码）")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
