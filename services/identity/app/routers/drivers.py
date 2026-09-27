from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from tesla_common.auth import Principal
from tesla_common.errors import DomainError
from tesla_common.events import emit

from ..deps import auth, db, trip_client
from ..models import Driver, Outbox, User, Vehicle
from ..schemas import DriverOut, UserOut, VehicleIn, VehicleOut

router = APIRouter(prefix="/drivers/me", tags=["drivers"])
PRODUCER = "identity-service"


async def _load(s, user_id: str) -> tuple[User, Driver]:
    row = (await s.execute(select(User, Driver).join(Driver, Driver.user_id == User.id)
                           .where(User.id == user_id))).first()
    if row is None:
        raise DomainError("DRIVER_NOT_FOUND", "Driver profile not found", 404)
    return row[0], row[1]


def _to_out(user: User, driver: Driver) -> DriverOut:
    return DriverOut(user=UserOut.model_validate(user), license_number=driver.license_number, status=driver.status,
                     vehicle=VehicleOut.model_validate(driver.vehicle) if driver.vehicle else None)


@router.get("", response_model=DriverOut)
async def get_me(p: Principal = Depends(auth.role("DRIVER"))):
    async with db.ro() as s:
        user, driver = await _load(s, p.user_id)
    return _to_out(user, driver)


@router.put("/vehicle", response_model=VehicleOut)
async def put_vehicle(body: VehicleIn, p: Principal = Depends(auth.role("DRIVER"))):
    async with db.rw.begin() as s:
        _, driver = await _load(s, p.user_id)
        if driver.status == "ONLINE":
            # Trip and Matching were told this seat capacity when he went online.
            raise DomainError("DRIVER_ONLINE", "Go offline before changing your vehicle", 409)
        if await s.scalar(select(Vehicle.id).where(Vehicle.plate == body.plate, Vehicle.driver_id != driver.user_id)):
            raise DomainError("PLATE_TAKEN", "Plate already registered to another driver", 409)
        vehicle = driver.vehicle
        if vehicle is None:
            vehicle = Vehicle(driver_id=driver.user_id, **body.model_dump())
            s.add(vehicle)
        else:
            for field, value in body.model_dump().items():
                setattr(vehicle, field, value)
        await s.flush()
    return vehicle


@router.post("/online", response_model=DriverOut)
async def go_online(p: Principal = Depends(auth.role("DRIVER"))):
    async with db.rw.begin() as s:
        user, driver = await _load(s, p.user_id)
        if driver.vehicle is None:
            raise DomainError("NO_VEHICLE", "Register a vehicle first", 409)
        if driver.status != "ONLINE":
            driver.status = "ONLINE"
            v = driver.vehicle
            emit(s, Outbox, PRODUCER, "identity.driver.online", {
                "driver_id": user.id, "driver_name": user.full_name, "vehicle_id": v.id,
                "vehicle_nickname": v.nickname, "plate": v.plate, "seat_capacity": v.seat_capacity,
            })
    return _to_out(user, driver)


@router.post("/offline", response_model=DriverOut)
async def go_offline(request: Request, p: Principal = Depends(auth.role("DRIVER"))):
    resp = await trip_client.request("GET", f"/internal/drivers/{p.user_id}/live-pool",
                                     request_id=request.headers.get("x-request-id"), retries=1)
    # ServiceClient only raises on 5xx; a 401/404 must not read as "no pool" and let him go offline.
    if resp.status_code != 200:
        raise DomainError("UPSTREAM_ERROR", f"trip returned {resp.status_code}", 503)
    if resp.json().get("pool_id"):
        raise DomainError("DRIVER_HAS_LIVE_POOL", "Finish or cancel your current pool first", 409)
    async with db.rw.begin() as s:
        user, driver = await _load(s, p.user_id)
        if driver.status != "OFFLINE":
            driver.status = "OFFLINE"
            emit(s, Outbox, PRODUCER, "identity.driver.offline", {"driver_id": user.id})
    return _to_out(user, driver)
