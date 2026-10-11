import hashlib
import json

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from models import ManagementOperation


def begin_operation(session, request_id, action, actor, payload):
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                      separators=(",", ":"), default=str).encode()).hexdigest()

    def replay():
        saved = session.exec(select(ManagementOperation).where(
            ManagementOperation.client_request_id == request_id
        ).execution_options(populate_existing=True)).first()
        if saved:
            if (saved.operator_user_id != actor.id or saved.action != action or saved.request_hash != digest):
                raise HTTPException(409, "该请求编号已用于其他管理操作，不能更改内容后重试")
            if not saved.result_json:
                raise HTTPException(409, "操作尚未完成，请稍后重试")
            return json.loads(saved.result_json)
        return None

    result = replay()
    if result is not None:
        return None, result
    operation = ManagementOperation(client_request_id=request_id, operator_user_id=actor.id,
                                    action=action, request_hash=digest)
    session.add(operation)
    try:
        # Reserve the request before taking business row locks; commit with its effects.
        session.flush()
    except IntegrityError:
        session.rollback()
        result = replay()
        if result is not None:
            return None, result
        raise HTTPException(409, "请求提交冲突，请重试确认")
    return operation, None


def finish_operation(session, operation, result):
    operation.result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
    session.add(operation)
