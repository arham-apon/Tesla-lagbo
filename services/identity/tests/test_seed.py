"""3.6 step 7: seed is idempotent, uses the fixed ids, and the demo password works."""
from sqlalchemy import func, select

from app.models import Driver, User, Vehicle
from app.seed import BULLET, USERS, seed
from conftest import GATEWAY


async def counts(db) -> tuple[int, int, int]:
    async with db.ro() as s:
        return tuple([await s.scalar(select(func.count()).select_from(m)) for m in (User, Driver, Vehicle)])


async def test_seed_creates_cast_with_fixed_ids(db):
    assert await seed(db) == ["Jashim", "Nusrat", "Rafiq", "Shirin"]
    assert await counts(db) == (4, 1, 1)
    async with db.ro() as s:
        for user_id, name, phone, role in USERS:
            u = await s.get(User, user_id)
            assert (u.full_name, u.phone, u.role) == (name, phone, role)
        bullet = await s.get(Vehicle, BULLET["id"])
    assert (bullet.nickname, bullet.plate, bullet.seat_capacity, bullet.driver_id) == \
           ("Bullet", "DHAKA-TESLA-11", 3, "11111111-1111-4111-8111-111111111111")


async def test_seed_twice_changes_nothing(db):
    await seed(db)
    assert await seed(db) == []
    assert await counts(db) == (4, 1, 1)


async def test_seed_skips_phone_already_registered(db, api):
    await api.post("/auth/register", headers=GATEWAY, json={"full_name": "Real Nusrat", "phone": "01711000002",
                                                           "password": "her-own-pw", "role": "PASSENGER"})
    assert await seed(db) == ["Jashim", "Rafiq", "Shirin"]
    async with db.ro() as s:
        assert (await s.scalar(select(User).where(User.phone == "01711000002"))).full_name == "Real Nusrat"


async def test_demo_password_logs_in(db, api):
    await seed(db)
    resp = await api.post("/auth/login", json={"phone": "01711000002", "password": "Pool@1234"}, headers=GATEWAY)
    assert resp.status_code == 200 and resp.json()["user"]["id"] == "22222222-2222-4222-8222-222222222222"


async def test_seeded_driver_is_offline_with_bullet(db, api):
    await seed(db)
    j = GATEWAY | {"X-User-Id": "11111111-1111-4111-8111-111111111111", "X-User-Role": "DRIVER"}
    body = (await api.get("/drivers/me", headers=j)).json()
    assert body["status"] == "OFFLINE" and body["vehicle"]["seat_capacity"] == 3
