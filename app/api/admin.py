from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.db.database import get_db
from app.models.user import User

router = APIRouter()


def _check_secret(secret: str):
    if not settings.admin_secret or secret != settings.admin_secret:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.get("/users")
async def list_users(secret: str, db: AsyncSession = Depends(get_db)):
    _check_secret(secret)
    result = await db.execute(
        select(User.id, User.email, User.created_at).order_by(User.created_at.desc())
    )
    rows = result.all()
    return [{"id": str(r.id), "email": r.email, "created_at": str(r.created_at)} for r in rows]
