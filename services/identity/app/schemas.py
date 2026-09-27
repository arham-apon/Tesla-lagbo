from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Phone = Field(pattern=r"^01[3-9]\d{8}$")


class RegisterIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=100)
    phone: str = Phone
    password: str = Field(min_length=8, max_length=128)
    role: Literal["PASSENGER", "DRIVER"]
    license_number: str | None = Field(default=None, max_length=50)


class LoginIn(BaseModel):
    phone: str = Phone
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    full_name: str
    phone: str
    role: str


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserOut


class VehicleIn(BaseModel):
    nickname: str = Field(min_length=1, max_length=50)
    make: str = Field(max_length=50)
    model: str = Field(max_length=50)
    plate: str = Field(min_length=3, max_length=30)
    seat_capacity: int = Field(ge=1, le=6)


class VehicleOut(VehicleIn):
    model_config = ConfigDict(from_attributes=True)
    id: str


class DriverOut(BaseModel):
    user: UserOut
    license_number: str
    status: Literal["OFFLINE", "ONLINE"]
    vehicle: VehicleOut | None
