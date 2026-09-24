import json
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import select, update

from tesla_common.auth import Principal
from tesla_common.errors import DomainError
from tesla_common.timeutil import utcnow

from ..deps import auth, db
from ..models import Notification

# Through the gateway (internal token + the user's headers), any role: everyone has an inbox.
router = APIRouter(prefix="/notifications", tags=["inbox"])
anyone = auth.principal()


class NotificationOut(BaseModel):
    id: int
    type: str
    payload: dict      # the message exactly as it was pushed (7.4), as JSON, not a string inside a string
    created_at: datetime
    read_at: datetime | None


@router.get("", response_model=list[NotificationOut])
async def catch_up(after_id: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100),
                   p: Principal = Depends(anyone)):
    """What I missed: my messages newer than `after_id`, oldest first. Next page: after_id = the last id here."""
    async with db.ro() as s:
        rows = (await s.execute(select(Notification).where(Notification.user_id == p.user_id,
                                                           Notification.id > after_id)
                                .order_by(Notification.id).limit(limit))).scalars().all()
    return [NotificationOut(id=n.id, type=n.type, payload=json.loads(n.payload), created_at=n.created_at,
                            read_at=n.read_at) for n in rows]


@router.post("/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(notification_id: int, p: Principal = Depends(anyone)):
    """Only my own messages. Someone else's (or a missing one) is 404, so ids can't be probed.
    Reading twice keeps the first time it was read."""
    async with db.rw.begin() as s:
        mine = await s.scalar(select(Notification.id).where(Notification.id == notification_id,
                                                            Notification.user_id == p.user_id))
        if mine is None:
            raise DomainError("NOTIFICATION_NOT_FOUND", "Notification not found", 404)
        await s.execute(update(Notification).where(Notification.id == notification_id,
                                                   Notification.read_at.is_(None)).values(read_at=utcnow()))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
