"""5.5 (plan 8.5): nobody can act on someone else's ride. Refusals are 404, not 403, so Rafiq can't even learn
that Nusrat's ride id exists. The HTTP-level checks come with the routers in 5.6."""
import pytest

from tesla_common.errors import DomainError

from app.lifecycle import transition
from app.models import Pool, RideRequest
from conftest import AS_JASHIM, AS_KARIM, AS_NUSRAT, AS_RAFIQ, KARIM, add, bullet_with_nusrat, ride, shift


async def not_found(db, ride_id, target, who):
    with pytest.raises(DomainError) as e:
        await transition(db.rw, ride_id, target, who, "x")
    assert (e.value.code, e.value.status) == ("RIDE_NOT_FOUND", 404)


async def test_rafiq_cannot_cancel_nusrats_ride(db):
    pool_id, nusrat = await bullet_with_nusrat(db)
    await not_found(db, nusrat, "CANCELLED", AS_RAFIQ)
    async with db.ro() as s:
        assert (await s.get(RideRequest, nusrat)).status == "MATCHED"
        assert (await s.get(Pool, pool_id)).occupied_seats == 1


async def test_another_driver_cannot_touch_jashims_riders(db):
    _, nusrat = await bullet_with_nusrat(db)
    await add(db, shift(KARIM, driver_name="Karim"))
    for target in ("DRIVER_ARRIVED", "CANCELLED"):
        await not_found(db, nusrat, target, AS_KARIM)


async def test_a_driver_cannot_act_on_a_ride_before_it_is_in_his_pool(db):
    r = ride()
    await add(db, shift(), r)
    await not_found(db, r.id, "CANCELLED", AS_JASHIM)


async def test_same_answer_as_a_ride_that_does_not_exist(db):
    _, nusrat = await bullet_with_nusrat(db)
    with pytest.raises(DomainError) as other:
        await transition(db.rw, nusrat, "CANCELLED", AS_RAFIQ)
    with pytest.raises(DomainError) as missing:
        await transition(db.rw, "no-such-ride", "CANCELLED", AS_RAFIQ)
    assert (other.value.code, other.value.status, other.value.message) == \
           (missing.value.code, missing.value.status, missing.value.message)


async def test_owners_can(db):
    _, nusrat = await bullet_with_nusrat(db)
    await transition(db.rw, nusrat, "DRIVER_ARRIVED", AS_JASHIM)
    r = ride(passenger_id="passenger-x", passenger_name="X")
    await add(db, r)
    await transition(db.rw, r.id, "CANCELLED", AS_NUSRAT.model_copy(update={"user_id": "passenger-x"}))
