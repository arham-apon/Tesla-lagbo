"""7.3: the inbox row: what's stored, what's refused, and the order a phone reads it back in."""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Notification, ProcessedEvent
from conftest import NUSRAT, RAFIQ, add


def note(user_id=NUSRAT, type_="ride.matched", **payload) -> Notification:
    return Notification(user_id=user_id, type=type_,
                        payload=json.dumps({"type": type_, "event_id": "e-1", "data": payload or {"ride_id": "r"}}))


async def test_a_saved_message(db):
    n = note(driver_name="Jashim", vehicle_nickname="Bullet")
    await add(db, n)
    assert (n.id, n.read_at, n.created_at is not None) == (1, None, True)
    assert json.loads(n.payload)["data"] == {"driver_name": "Jashim", "vehicle_nickname": "Bullet"}


@pytest.mark.parametrize("payload", ["not json", "", "{'single': 'quotes'}", '{"cut off": '])
async def test_payload_must_be_json(db, payload):
    with pytest.raises(IntegrityError):
        await add(db, Notification(user_id=NUSRAT, type="ride.matched", payload=payload))


async def test_needs_a_person(db):
    with pytest.raises(IntegrityError):
        await add(db, note(user_id=""))


async def test_ids_only_go_up(db):
    """Catch-up asks for "after the last id I saw", so ids must grow in the order messages were saved."""
    await add(db, *[note(NUSRAT if i % 2 else RAFIQ, ride_id=f"r{i}") for i in range(6)])
    async with db.ro() as s:
        mine = (await s.execute(select(Notification.id).where(Notification.user_id == NUSRAT)
                                .order_by(Notification.id))).scalars().all()
        after = (await s.execute(select(Notification.id).where(Notification.user_id == NUSRAT,
                                                               Notification.id > mine[0]))).scalars().all()
    assert mine == sorted(mine) and after == mine[1:]


async def test_each_event_is_recorded_once(db):
    await add(db, ProcessedEvent(event_id="e-1"))
    with pytest.raises(IntegrityError):
        await add(db, ProcessedEvent(event_id="e-1"))
