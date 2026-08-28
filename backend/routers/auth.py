from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func
from sqlmodel import Session, select

from database import DEFAULT_STORES, get_session
from audit import add_audit_event
from models import User
from schemas import LoginRequest, UserCreate, UserUpdate
from security import (check_login_allowed, clear_login_failures, get_current_user,
                      clear_auth_cookies, hash_password, issue_token,
                      password_needs_rehash, record_login_failure,
                      require_manager, revoke_user_tokens, set_auth_cookies,
                      verify_password)

router = APIRouter(prefix="/api/auth", tags=["auth"])
users_router = APIRouter(prefix="/api/users", tags=["users"],
                         dependencies=[Depends(require_manager)])


def _user_view(u: User) -> dict:
    return {"id": u.id, "username": u.username, "name": u.name,
            "role": u.role, "store": u.store, "active": u.active}


# ============ 登录 / 登出 / 当前用户 ============
@router.post("/login")
def login(data: LoginRequest, request: Request, response: Response,
          session: Session = Depends(get_session)):
    login_key = check_login_allowed(data.username, request, session)
    username = data.username.strip()
    user = session.exec(
        select(User).where(func.lower(User.username) == username.lower())
    ).first()
    if not user or not verify_password(data.password, user.password_hash):
        record_login_failure(login_key, session)
        add_audit_event(
            session, action="auth.login_failed", entity_type="user",
            detail={"username": username}, ip_address=login_key[1],
        )
        session.commit()
        raise HTTPException(401, "用户名或密码错误")
    if not user.active:
        raise HTTPException(403, "账号已停用")
    if user.role == "staff" and user.store not in DEFAULT_STORES:
        raise HTTPException(403, "店员账号未绑定有效门店，请联系店长")
    clear_login_failures(login_key, session)
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(data.password)
        session.add(user)
    raw_token, csrf_token, _ = issue_token(session, user)
    add_audit_event(
        session, action="auth.login", entity_type="user", entity_id=user.id,
        actor=user, store=user.store, ip_address=login_key[1],
    )
    session.commit()
    set_auth_cookies(response, raw_token, csrf_token)
    return {"user": _user_view(user)}


@router.post("/logout")
def logout(request: Request, response: Response,
           user: User = Depends(get_current_user),
           session: Session = Depends(get_session)):
    row = getattr(request.state, "auth_token", None)
    if row:
        session.delete(row)
    add_audit_event(
        session, action="auth.logout", entity_type="user", entity_id=user.id,
        actor=user, store=user.store,
    )
    session.commit()
    clear_auth_cookies(response)
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return _user_view(user)


# ============ 账号管理（店长） ============
@users_router.post("")
def create_user(data: UserCreate, manager: User = Depends(require_manager),
                session: Session = Depends(get_session)):
    if data.role not in ("staff", "manager"):
        raise HTTPException(400, "角色只能是 staff 或 manager")
    if not data.password:
        raise HTTPException(400, "密码不能为空")
    _validate_user_store(data.role, data.store)
    username = data.username.strip()
    user = User(username=username, password_hash=hash_password(data.password),
                name=data.name, role=data.role, store=data.store)
    session.add(user)
    try:
        session.flush()
        add_audit_event(
            session, action="user.create", entity_type="user", entity_id=user.id,
            actor=manager, store=user.store,
            after={"username": user.username, "name": user.name, "role": user.role,
                   "store": user.store, "active": user.active},
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(400, f"用户名 {data.username} 已存在")
    session.refresh(user)
    return _user_view(user)


@users_router.get("")
def list_users(session: Session = Depends(get_session)):
    users = session.exec(select(User).order_by(User.id)).all()
    return [_user_view(u) for u in users]


@users_router.put("/{user_id}")
def update_user(user_id: int, data: UserUpdate,
                manager: User = Depends(require_manager),
                session: Session = Depends(get_session)):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    before = {"name": user.name, "role": user.role, "store": user.store, "active": user.active}
    next_role = data.role if data.role is not None else user.role
    next_store = data.store if data.store is not None else user.store
    _validate_user_store(next_role, next_store)
    if data.role is not None:
        if data.role not in ("staff", "manager"):
            raise HTTPException(400, "角色只能是 staff 或 manager")
        # 防止把最后一个启用的店长降级/停用，导致没人能管账号
        if user.role == "manager" and data.role != "manager":
            _guard_last_manager(session, user_id)
        user.role = data.role
    if data.active is not None:
        if user.role == "manager" and not data.active:
            _guard_last_manager(session, user_id)
        user.active = data.active
    if data.name is not None:
        user.name = data.name
    if data.store is not None:
        user.store = data.store
    if data.password:
        user.password_hash = hash_password(data.password)
        revoke_user_tokens(session, user.id)
    if data.active is False:
        revoke_user_tokens(session, user.id)
    session.add(user)
    add_audit_event(
        session, action="user.update", entity_type="user", entity_id=user.id,
        actor=manager, store=user.store, before=before,
        after={"name": user.name, "role": user.role, "store": user.store,
               "active": user.active, "password_changed": bool(data.password)},
    )
    session.commit()
    session.refresh(user)
    return _user_view(user)


@users_router.delete("/{user_id}")
def delete_user(user_id: int, manager: User = Depends(require_manager),
                session: Session = Depends(get_session)):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    if user.id == manager.id:
        raise HTTPException(400, "不能删除当前登录的自己")
    if user.role == "manager":
        _guard_last_manager(session, user_id)
    user.active = False
    revoke_user_tokens(session, user.id)
    session.add(user)
    add_audit_event(
        session, action="user.disable", entity_type="user", entity_id=user.id,
        actor=manager, store=user.store,
        before={"active": True}, after={"active": False},
    )
    session.commit()
    return {"ok": True, "message": "账号已停用，历史记录已保留"}


def _guard_last_manager(session: Session, excluding_id: int):
    others = session.exec(
        select(User).where(User.role == "manager", User.active == True,  # noqa: E712
                           User.id != excluding_id)
    ).first()
    if not others:
        raise HTTPException(400, "至少要保留一个启用的店长账号")


def _validate_user_store(role: str, store: Optional[str]) -> None:
    if store is not None and store not in DEFAULT_STORES:
        raise HTTPException(400, "所属门店必须是 1号店 到 4号店之一")
    if role == "staff" and store not in DEFAULT_STORES:
        raise HTTPException(400, "店员账号必须绑定所属门店")
