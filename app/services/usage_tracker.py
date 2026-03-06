"""
Usage tracking and spend-limit enforcement for Anthropic and Google CSE.
"""

from datetime import datetime, timezone, date
from typing import Optional
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models.api_usage import ApiUsage
from app.models.spend_limits import SpendLimit

# ── Pricing constants ────────────────────────────────────────────────────────
# claude-opus-4-6  (update if Anthropic changes pricing)
CLAUDE_INPUT_COST_PER_TOKEN = 15.0 / 1_000_000   # $15 per million input tokens
CLAUDE_OUTPUT_COST_PER_TOKEN = 75.0 / 1_000_000  # $75 per million output tokens

# Google Custom Search JSON API
GOOGLE_CSE_FREE_DAILY_QUERIES = 100
GOOGLE_CSE_COST_PER_QUERY = 5.0 / 1000  # $5 per 1,000 queries beyond free tier


def _today_start() -> datetime:
    """UTC midnight of today as a timezone-aware datetime."""
    d = date.today()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _month_start() -> datetime:
    """UTC start of this calendar month."""
    d = date.today()
    return datetime(d.year, d.month, 1, tzinfo=timezone.utc)


async def check_spend_limit(api_name: str, db: AsyncSession) -> None:
    """
    Raise HTTP 429 if an active spend limit for api_name has been exceeded.
    Checks both daily and monthly USD limits, plus daily query count for google_cse.
    """
    result = await db.execute(
        select(SpendLimit).where(SpendLimit.api_name == api_name, SpendLimit.enabled.is_(True))
    )
    limit = result.scalar_one_or_none()
    if not limit:
        return  # No limit configured → allow

    today = _today_start()
    month = _month_start()

    # Daily USD check
    if limit.daily_limit_usd is not None:
        row = await db.execute(
            select(func.coalesce(func.sum(ApiUsage.cost_usd), 0.0)).where(
                ApiUsage.api_name == api_name,
                ApiUsage.created_at >= today,
            )
        )
        daily_spend = row.scalar()
        if daily_spend >= limit.daily_limit_usd:
            raise HTTPException(
                status_code=429,
                detail=f"Daily spend limit of ${limit.daily_limit_usd:.2f} reached for {api_name}. Resets at midnight UTC.",
            )

    # Monthly USD check
    if limit.monthly_limit_usd is not None:
        row = await db.execute(
            select(func.coalesce(func.sum(ApiUsage.cost_usd), 0.0)).where(
                ApiUsage.api_name == api_name,
                ApiUsage.created_at >= month,
            )
        )
        monthly_spend = row.scalar()
        if monthly_spend >= limit.monthly_limit_usd:
            raise HTTPException(
                status_code=429,
                detail=f"Monthly spend limit of ${limit.monthly_limit_usd:.2f} reached for {api_name}. Resets next month.",
            )

    # Daily query count check (Google CSE only)
    if api_name == "google_cse" and limit.google_daily_query_limit is not None:
        row = await db.execute(
            select(func.coalesce(func.sum(ApiUsage.queries_count), 0)).where(
                ApiUsage.api_name == "google_cse",
                ApiUsage.created_at >= today,
            )
        )
        daily_queries = row.scalar()
        if daily_queries >= limit.google_daily_query_limit:
            raise HTTPException(
                status_code=429,
                detail=f"Daily Google CSE query limit of {limit.google_daily_query_limit} reached. Resets at midnight UTC.",
            )


async def record_usage(
    db: AsyncSession,
    api_name: str,
    operation: str,
    user_id: Optional[uuid.UUID],
    tokens_input: int = 0,
    tokens_output: int = 0,
    cost_usd: float = 0.0,
    queries_count: int = 0,
) -> None:
    """Insert an ApiUsage row. For google_cse, cost is computed from today's running total."""
    if api_name == "google_cse" and queries_count > 0:
        # Compute cost: first 100/day are free
        today = _today_start()
        row = await db.execute(
            select(func.coalesce(func.sum(ApiUsage.queries_count), 0)).where(
                ApiUsage.api_name == "google_cse",
                ApiUsage.created_at >= today,
            )
        )
        existing_today = row.scalar()
        # How many of this batch fall in paid territory
        free_remaining = max(0, GOOGLE_CSE_FREE_DAILY_QUERIES - existing_today)
        paid_queries = max(0, queries_count - free_remaining)
        cost_usd = paid_queries * GOOGLE_CSE_COST_PER_QUERY

    entry = ApiUsage(
        user_id=user_id,
        api_name=api_name,
        operation=operation,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cost_usd=cost_usd,
        queries_count=queries_count,
    )
    db.add(entry)
    # Flush but don't commit — caller owns the transaction
    await db.flush()
