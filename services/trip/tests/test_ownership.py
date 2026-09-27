"""5.5 / 5.6 (plan 8.5): nobody can act on someone else's ride. Refusals are 404, not 403, so Rafiq can't even
learn that Nusrat's ride id exists. First at the function level, then through HTTP."""
import pytest

from tesla_common.errors import DomainError

from app.lifecycle import transition
from app.models import Pool, RideOffer, RideRequest
from conftest import (AS_JASHIM, AS_KARIM, AS_NUSRAT, AS_RAFIQ, JASHIM, KARIM, add, as_user, bullet_with_nusrat,
                      ride, shift)


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


# ---- 5.6: the same rules through HTTP (plan 8.5) ----------------------------------------------------------------

async def test_http_rafiq_cannot_see_or_cancel_nusrats_ride(api, db):
    _, nusrat = await bullet_with_nusrat(db)
    for method, path in [("get", f"/rides/{nusrat}"), ("post", f"/rides/{nusrat}/cancel")]:
        extra = {"json": {}} if method == "post" else {}
        resp = await getattr(api, method)(path, headers=as_user(AS_RAFIQ), **extra)
        assert (resp.status_code, resp.json()["error"]["code"]) == (404, "RIDE_NOT_FOUND")


async def test_http_other_driver_gets_404_on_every_move(api, db):
    _, nusrat = await bullet_with_nusrat(db)
    await add(db, shift(KARIM, driver_name="Karim"))
    for action, body in [("arrive", None), ("start", None), ("complete", None), ("cancel", {"reason": "x"})]:
        resp = await api.post(f"/driver/rides/{nusrat}/{action}", headers=as_user(AS_KARIM), json=body)
        assert (resp.status_code, resp.json()["error"]["code"]) == (404, "RIDE_NOT_FOUND"), action


async def test_http_rafiqs_list_never_shows_nusrats_rides(api, db):
    await bullet_with_nusrat(db)
    assert (await api.get("/rides", headers=as_user(AS_RAFIQ))).json() == []


async def test_http_offers_are_private_to_each_driver(api, db):
    r = ride()
    await add(db, shift(), shift(KARIM, driver_name="Karim"), r,
              RideOffer(ride_id=r.id, driver_id=JASHIM, distance_m=800))
    assert (await api.get("/driver/offers", headers=as_user(AS_KARIM))).json() == []
    resp = await api.post(f"/driver/offers/{r.id}/accept", headers=as_user(AS_KARIM))
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "OFFER_NOT_FOUND")  # not his offer


async def test_http_a_driver_cannot_decline_someone_elses_offer(api, db):
    r = ride()
    await add(db, shift(), shift(KARIM, driver_name="Karim"), r,
              RideOffer(ride_id=r.id, driver_id=JASHIM, distance_m=800))
    resp = await api.post(f"/driver/offers/{r.id}/decline", headers=as_user(AS_KARIM))
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "OFFER_NOT_FOUND")
    assert [o["ride_id"] for o in (await api.get("/driver/offers", headers=as_user(AS_JASHIM))).json()] == [r.id]
