from datetime import datetime, timedelta
from typing import Optional
import secrets
import string

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from pydantic import BaseModel, EmailStr
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings
from app.db.database import get_db
from app.models.user import User, UserPreferences

router = APIRouter()
security = HTTPBearer()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Schemas ───────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UpdatePreferencesRequest(BaseModel):
    desired_min_comp: Optional[int] = None
    desired_target_comp: Optional[int] = None
    comp_flexibility: str = "moderate"
    remote_preference: list[str] = ["open"]
    preferred_regions: list[str] = []
    relocate_willing: bool = False


class UpdateSheetRequest(BaseModel):
    sheet_id: str
    google_oauth_token: dict  # raw token from extension OAuth flow


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    reset_code: str
    new_password: str


# ── Helpers ───────────────────────────────────────────────────────────────

def create_access_token(user_id: str) -> str:
    expire = datetime.utcnow() + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    return jwt.encode(
        {"sub": user_id, "exp": expire},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        user_id: str = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    result = await db.execute(
        select(User)
        .options(selectinload(User.resume), selectinload(User.preferences))
        .where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.post("/register", response_model=TokenResponse)
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == req.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    user = User(
        email=req.email,
        password_hash=pwd_context.hash(req.password),
    )
    db.add(user)
    await db.flush()

    prefs = UserPreferences(user_id=user.id)
    db.add(prefs)
    await db.commit()

    return TokenResponse(access_token=create_access_token(str(user.id)))


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == req.email))
    user = result.scalar_one_or_none()

    if not user or not pwd_context.verify(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return TokenResponse(access_token=create_access_token(str(user.id)))


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    prefs = current_user.preferences
    skills: list[str] = []
    industries: list[str] = []
    if current_user.resume:
        skills = current_user.resume.structured_json.get("skills", [])
        industries = current_user.resume.structured_json.get("industries", [])
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "has_resume": current_user.resume is not None,
        "has_sheet": bool(current_user.sheet_id),
        "sheet_id": current_user.sheet_id,
        "desired_min_comp": prefs.desired_min_comp if prefs else None,
        "remote_preference": prefs.remote_preference if prefs else ["open"],
        "skills": skills,
        "industries": industries,
    }


@router.post("/preferences")
async def update_preferences(
    req: UpdatePreferencesRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    prefs = current_user.preferences
    if not prefs:
        prefs = UserPreferences(user_id=current_user.id)
        db.add(prefs)

    prefs.desired_min_comp = req.desired_min_comp
    prefs.desired_target_comp = req.desired_target_comp
    prefs.comp_flexibility = req.comp_flexibility
    prefs.remote_preference = req.remote_preference
    prefs.preferred_regions = req.preferred_regions
    prefs.relocate_willing = req.relocate_willing
    await db.commit()
    return {"status": "ok"}


@router.get("/check-sheet")
async def check_sheet(current_user: User = Depends(get_current_user)):
    """Ping the Sheets API to verify the stored token is still valid."""
    if not current_user.google_oauth_token or not current_user.sheet_id:
        return {"connected": False, "valid": False, "sheet_id": None}

    from app.services.google_sheets import decrypt_token
    import httpx

    try:
        token_data = decrypt_token(current_user.google_oauth_token)
        access_token = token_data.get("access_token", "")
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                f"https://sheets.googleapis.com/v4/spreadsheets/{current_user.sheet_id}/values/A1:A1",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        return {
            "connected": True,
            "valid": resp.status_code != 401,
            "sheet_id": current_user.sheet_id,
        }
    except Exception:
        return {"connected": True, "valid": False, "sheet_id": current_user.sheet_id}


@router.post("/connect-google")
async def connect_google(
    req: UpdateSheetRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services.google_sheets import encrypt_token

    current_user.google_oauth_token = encrypt_token(req.google_oauth_token)
    current_user.sheet_id = req.sheet_id
    await db.commit()
    return {"status": "ok"}


_RESET_CODE_TTL = 900  # 15 minutes


@router.post("/forgot-password")
async def forgot_password(req: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == req.email))
    user = result.scalar_one_or_none()

    if user:
        code = "".join(secrets.choice(string.digits) for _ in range(6))
        from app.services.redis_client import get_redis
        redis = await get_redis()
        await redis.set(f"pwd_reset:{req.email}", code, ex=_RESET_CODE_TTL)

        print(f"\n{'='*52}", flush=True)
        print(f"  APPZADDY PASSWORD RESET", flush=True)
        print(f"  Email : {req.email}", flush=True)
        print(f"  Code  : {code}", flush=True)
        print(f"  Valid for 15 minutes", flush=True)
        print(f"{'='*52}\n", flush=True)

    # Always return success to avoid leaking which emails are registered
    return {"message": "If that email is registered, a reset code was generated. Check the server terminal."}


@router.post("/reset-password")
async def reset_password(req: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    from app.services.redis_client import get_redis
    redis = await get_redis()

    stored = await redis.get(f"pwd_reset:{req.email}")
    if not stored or stored != req.reset_code:
        raise HTTPException(status_code=400, detail="Invalid or expired reset code")

    result = await db.execute(select(User).where(User.email == req.email))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired reset code")

    user.password_hash = pwd_context.hash(req.new_password)
    await db.commit()
    await redis.delete(f"pwd_reset:{req.email}")
    return {"message": "Password updated successfully"}
