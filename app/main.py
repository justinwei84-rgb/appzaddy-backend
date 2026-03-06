from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from sqlalchemy import text

from app.config import settings
from app.db.database import engine, Base
from app.api import auth, resume, jobs, admin
from app.services.redis_client import get_redis


async def _run_migrations(conn):
    """Run any pending schema migrations."""
    # Migrate remote_preference from enum to TEXT[] if needed
    result = await conn.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'user_preferences' AND column_name = 'remote_preference'"
        )
    )
    row = result.fetchone()
    if row and row[0] != "ARRAY":
        await conn.execute(
            text("ALTER TABLE user_preferences DROP COLUMN remote_preference")
        )
        await conn.execute(
            text(
                "ALTER TABLE user_preferences "
                "ADD COLUMN remote_preference TEXT[] DEFAULT ARRAY['open']::TEXT[]"
            )
        )

    # Add google_oauth_token and sheet_id to users if missing
    for col, definition in [
        ("google_oauth_token", "TEXT"),
        ("sheet_id", "VARCHAR(255)"),
    ]:
        result = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = :col"
            ),
            {"col": col},
        )
        if not result.fetchone():
            await conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {definition}"))

    # Add industry/location/comp columns to job_analyses if missing
    for col, definition in [
        ("industry", "VARCHAR(255) DEFAULT ''"),
        ("location_text", "VARCHAR(255) DEFAULT ''"),
        ("remote_indicator", "VARCHAR(50) DEFAULT 'unknown'"),
        ("compensation_min", "INTEGER"),
        ("compensation_max", "INTEGER"),
    ]:
        result = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'job_analyses' AND column_name = :col"
            ),
            {"col": col},
        )
        if not result.fetchone():
            await conn.execute(text(f"ALTER TABLE job_analyses ADD COLUMN {col} {definition}"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create tables then run migrations
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _run_migrations(conn)
    yield
    # Shutdown
    await engine.dispose()


app = FastAPI(
    title="AppZaddy API",
    description="LinkedIn job analyzer backend",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — allow Chrome extension origins
_cors_origins = [o.strip() for o in settings.cors_origins_str.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=r"chrome-extension://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(resume.router, prefix="/resume", tags=["resume"])
app.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])


@app.get("/health")
async def health():
    return {"status": "ok"}
