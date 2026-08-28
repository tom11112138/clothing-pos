import json
from typing import Any, Optional

from sqlmodel import Session

from models import AuditEvent, User


def add_audit_event(
    session: Session,
    *,
    action: str,
    entity_type: str,
    actor: Optional[User] = None,
    entity_id: Optional[int] = None,
    store: Optional[str] = None,
    request_id: Optional[str] = None,
    before: Any = None,
    after: Any = None,
    detail: Any = None,
    ip_address: Optional[str] = None,
) -> None:
    def encode(value: Any) -> Optional[str]:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    session.add(AuditEvent(
        actor_user_id=actor.id if actor else None,
        actor_username=actor.username if actor else None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        store=store,
        request_id=request_id,
        before_json=encode(before),
        after_json=encode(after),
        detail_json=encode(detail),
        ip_address=ip_address,
    ))
