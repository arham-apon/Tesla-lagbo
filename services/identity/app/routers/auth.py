import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from tesla_common.auth import Principal
from tesla_common.errors import DomainError
from tesla_common.timeutil import new_id

from ..config import settings
from ..deps import auth, db, private_key, redis
from ..models import Driver, User
from ..schemas import LoginIn, RegisterIn, TokenOut, UserOut
from ..tokens import issue_token

# register/login are public for users, but must still come through the gateway.
router = APIRouter(tags=["auth"], dependencies=[Depends(auth.require_internal())])
ph = PasswordHasher()
# Checked when the phone is unknown, so "no such user" costs the same time as "wrong password".
_DUMMY_HASH = ph.hash(new_id())


@router.post("/auth/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn):
    if body.role == "DRIVER" and not body.license_number:
        raise DomainError("LICENSE_REQUIRED", "Drivers must provide a license_number", 422)
    # argon2 is slow on purpose: hash in a thread, and before taking the SQLite write lock.
    password_hash = await run_in_threadpool(ph.hash, body.password)
    async with db.rw.begin() as s:
        if await s.scalar(select(User.id).where(User.phone == body.phone)):
            raise DomainError("PHONE_TAKEN", "Phone number already registered", 409)
        if body.role == "DRIVER" and await s.scalar(
                select(Driver.user_id).where(Driver.license_number == body.license_number)):
            raise DomainError("LICENSE_TAKEN", "License number already registered", 409)
        user = User(id=new_id(), full_name=body.full_name, phone=body.phone,
                    password_hash=password_hash, role=body.role)
        s.add(user)
        if body.role == "DRIVER":
            s.add(Driver(user_id=user.id, license_number=body.license_number))
    return user


@router.post("/auth/login", response_model=TokenOut)
async def login(body: LoginIn):
    async with db.ro() as s:
        user = await s.scalar(select(User).where(User.phone == body.phone))
    try:
        await run_in_threadpool(ph.verify, user.password_hash if user else _DUMMY_HASH, body.password)
    except (VerificationError, InvalidHashError):
        user = None
    if user is None:
        raise DomainError("INVALID_CREDENTIALS", "Wrong phone number or password", 401)
    token, _ = issue_token(private_key(), user.id, user.role, user.full_name, settings.JWT_TTL_SECONDS)
    return TokenOut(access_token=token, expires_in=settings.JWT_TTL_SECONDS, user=UserOut.model_validate(user))


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(_: Principal = Depends(auth.principal()),
                 x_token_jti: str = Header(default=""), x_token_exp: str = Header(default="")):
    # The gateway forwards the verified token's jti/exp (Part 2.4); revoke it until it would expire anyway.
    if not x_token_jti or not x_token_exp.isdigit():
        raise DomainError("TOKEN_CONTEXT_MISSING", "X-Token-Jti and X-Token-Exp are required", 400)
    remaining = int(x_token_exp) - int(time.time())
    if remaining > 0:
        await redis.set(f"auth:revoked:{x_token_jti}", 1, ex=remaining)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/users/me", response_model=UserOut)
async def me(p: Principal = Depends(auth.principal())):
    async with db.ro() as s:
        user = await s.get(User, p.user_id)
    if user is None:
        raise DomainError("USER_NOT_FOUND", "User not found", 404)
    return user
