from fastapi import APIRouter, Depends

from tesla_common.errors import DomainError

from ..deps import auth, db
from ..models import User
from ..schemas import UserOut

router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(auth.require_internal())])


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(user_id: str):
    async with db.ro() as s:
        user = await s.get(User, user_id)
    if user is None:
        raise DomainError("USER_NOT_FOUND", "User not found", 404)
    return user
