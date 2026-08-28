import json
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from database import get_session
from models import AuditEvent, User
from security import require_manager
from time_utils import utc_iso

router = APIRouter(prefix="/api/audit-events", tags=["audit"])


@router.get("")
def list_audit_events(
    action: Optional[str] = None,
    store: Optional[str] = None,
    limit: int = Query(default=200, ge=1, le=500),
    manager: User = Depends(require_manager),
    session: Session = Depends(get_session),
):
    stmt = select(AuditEvent)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if store:
        stmt = stmt.where(AuditEvent.store == store)
    rows = session.exec(stmt.order_by(AuditEvent.id.desc()).limit(limit)).all()

    def decode(value: Optional[str]):
        if not value:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    return [{
        "id": row.id,
        "actor_user_id": row.actor_user_id,
        "actor_username": row.actor_username,
        "action": row.action,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "store": row.store,
        "request_id": row.request_id,
        "before": decode(row.before_json),
        "after": decode(row.after_json),
        "detail": decode(row.detail_json),
        "ip_address": row.ip_address,
        "created_at": utc_iso(row.created_at),
    } for row in rows]
