import json
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from pydantic import BaseModel

from app.db.database import get_db
from app.models.user import User
from app.models.job_analysis import JobAnalysis, SavedJob
from app.models.company_research import CompanyResearchCache
from app.api.auth import get_current_user
from app.services import redis_client
from app.services.scoring import compute_full_score
from app.services.google_search import search_company, normalize_company_name
from app.services.claude_service import summarize_company_research, generate_scoring_narrative
from app.services.google_sheets import save_job_to_sheet
from app.services.usage_tracker import check_spend_limit, record_usage
from app.config import settings

router = APIRouter()


# ── Request / Response schemas ─────────────────────────────────────────────

class JobData(BaseModel):
    job_title: str
    company_name: str
    job_description: str
    job_url: str = ""
    applicant_count: Optional[int] = None
    posted_days_ago: Optional[int] = None
    reposted_flag: bool = False
    promoted_flag: bool = False
    compensation_min: Optional[int] = None
    compensation_max: Optional[int] = None
    location_text: str = ""
    remote_indicator: str = "unknown"  # remote|hybrid|onsite|unknown
    company_linkedin_url: str = ""
    employee_count_text: Optional[str] = None


class AnalyzeRequest(BaseModel):
    job_data: JobData


class SaveJobRequest(BaseModel):
    job_analysis_id: str


# ── Company research (cached) ──────────────────────────────────────────────

async def get_or_create_company_research(
    company_name: str,
    db: AsyncSession,
    user_id: Optional[uuid.UUID] = None,
) -> dict:
    """
    Check Redis → Postgres → run search. Cache at both layers.
    Records Google CSE and Anthropic usage when a fresh fetch is needed.
    """
    # Skip lookup entirely for empty/unknown company names to avoid
    # cache collisions where all "unknown" jobs share the same key.
    if not company_name.strip():
        return {
            "funding_detected": False,
            "layoff_detected": False,
            "scam_flag": False,
            "sentiment": "neutral",
            "industry": "",
            "summary": "No company name available for research.",
        }

    normalized = normalize_company_name(company_name)
    cache_key = f"company_research:{normalized}"

    # 1. Redis fast path (no API calls → no usage to record)
    redis = await redis_client.get_redis()
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    # 2. Postgres cache (no API calls → no usage to record)
    result = await db.execute(
        select(CompanyResearchCache).where(
            CompanyResearchCache.company_name_normalized == normalized,
            CompanyResearchCache.expires_at > datetime.utcnow(),
        )
    )
    row = result.scalar_one_or_none()
    if row:
        data = {
            "funding_detected": row.funding_detected,
            "layoff_detected": row.layoff_detected,
            "scam_flag": row.scam_flag,
            "sentiment": row.sentiment.value,
            "industry": "",  # not cached in postgres — will populate on next fresh fetch
            "summary": row.summary,
        }
        await redis.set(
            cache_key,
            json.dumps(data),
            ex=settings.company_research_ttl_seconds,
        )
        return data

    # 3. Run fresh research — check limits before each external call
    await check_spend_limit("google_cse", db)
    snippets, queries_made = await search_company(company_name)
    await record_usage(
        db,
        api_name="google_cse",
        operation="company_search",
        user_id=user_id,
        queries_count=queries_made,
    )

    await check_spend_limit("anthropic", db)
    research, claude_usage = await summarize_company_research(company_name, snippets)
    await record_usage(
        db,
        api_name="anthropic",
        operation="company_research",
        user_id=user_id,
        tokens_input=claude_usage.input_tokens,
        tokens_output=claude_usage.output_tokens,
        cost_usd=claude_usage.cost_usd,
    )

    expires_at = datetime.utcnow() + timedelta(
        seconds=settings.company_research_ttl_seconds
    )

    # Check if record exists (race condition guard) — use upsert-style
    existing = await db.execute(
        select(CompanyResearchCache).where(
            CompanyResearchCache.company_name_normalized == normalized
        )
    )
    cache_row = existing.scalar_one_or_none()
    if cache_row:
        cache_row.funding_detected = research.funding_detected
        cache_row.layoff_detected = research.layoff_detected
        cache_row.scam_flag = research.scam_flag
        cache_row.sentiment = research.sentiment
        cache_row.summary = research.summary
        cache_row.expires_at = expires_at
    else:
        cache_row = CompanyResearchCache(
            company_name_normalized=normalized,
            funding_detected=research.funding_detected,
            layoff_detected=research.layoff_detected,
            scam_flag=research.scam_flag,
            sentiment=research.sentiment,
            summary=research.summary,
            expires_at=expires_at,
        )
        db.add(cache_row)

    await db.commit()

    data = {
        "funding_detected": research.funding_detected,
        "layoff_detected": research.layoff_detected,
        "scam_flag": research.scam_flag,
        "sentiment": research.sentiment,
        "industry": research.industry,
        "summary": research.summary,
    }
    await redis.set(
        cache_key,
        json.dumps(data),
        ex=settings.company_research_ttl_seconds,
    )
    return data


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/analyze")
async def analyze_job(
    req: AnalyzeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.resume:
        raise HTTPException(
            status_code=422,
            detail="Upload your resume first via POST /resume/upload",
        )

    resume_data = current_user.resume.structured_json
    prefs_obj = current_user.preferences
    prefs = {}
    if prefs_obj:
        prefs = {
            "desired_min_comp": prefs_obj.desired_min_comp,
            "desired_target_comp": prefs_obj.desired_target_comp,
            "comp_flexibility": prefs_obj.comp_flexibility.value
            if prefs_obj.comp_flexibility
            else "moderate",
            "remote_preference": prefs_obj.remote_preference or ["open"],
            "preferred_regions": prefs_obj.preferred_regions or [],
            "relocate_willing": prefs_obj.relocate_willing,
        }

    job = req.job_data
    job_dict = job.model_dump()

    # Get company intelligence (cached; records usage internally when fresh)
    company_research = await get_or_create_company_research(
        job.company_name, db, user_id=current_user.id
    )

    # Compute scores
    scores = compute_full_score(job_dict, resume_data, prefs, company_research)

    # Generate narrative via Claude — check limit first
    await check_spend_limit("anthropic", db)
    narrative, narrative_usage = await generate_scoring_narrative(
        job_title=job.job_title,
        company_name=job.company_name,
        recommendation=scores["recommendation"].value,
        fit_score=scores["fit_score"],
        job_quality_score=scores["job_quality_score"],
        company_score=scores["company_score"],
        response_percent=scores["response_percent"],
        user_skills=resume_data.get("skills", []),
        job_description_snippet=job.job_description,
        company_summary=company_research["summary"],
    )
    await record_usage(
        db,
        api_name="anthropic",
        operation="scoring_narrative",
        user_id=current_user.id,
        tokens_input=narrative_usage.input_tokens,
        tokens_output=narrative_usage.output_tokens,
        cost_usd=narrative_usage.cost_usd,
    )

    # Persist analysis
    analysis = JobAnalysis(
        user_id=current_user.id,
        company_name=job.company_name,
        job_title=job.job_title,
        job_url=job.job_url,
        recommendation=scores["recommendation"],
        response_band=scores["response_band"],
        response_percent=scores["response_percent"],
        fit_score=scores["fit_score"],
        job_quality_score=scores["job_quality_score"],
        company_score=scores["company_score"],
        summary_text=narrative.summary_text,
        industry=company_research.get("industry", ""),
        location_text=job.location_text,
        remote_indicator=job.remote_indicator,
        compensation_min=job.compensation_min,
        compensation_max=job.compensation_max,
    )
    db.add(analysis)
    await db.commit()

    return {
        "analysis_id": str(analysis.id),
        "job_title": job.job_title,
        "company_name": job.company_name,
        "industry": company_research.get("industry", ""),
        "recommendation": scores["recommendation"].value,
        "total_score": scores["total_score"],
        "fit_score": scores["fit_score"],
        "job_quality_score": scores["job_quality_score"],
        "company_score": scores["company_score"],
        "response_band": scores["response_band"].value,
        "response_percent": scores["response_percent"],
        "summary_text": narrative.summary_text,
        "top_drivers": narrative.top_drivers,
        "red_flags": narrative.red_flags,
        "company_highlights": {
            "funding_detected": company_research["funding_detected"],
            "layoff_detected": company_research["layoff_detected"],
            "scam_flag": company_research["scam_flag"],
            "sentiment": company_research["sentiment"],
            "summary": company_research["summary"],
        },
        "sub_scores": scores["sub_scores"],
        "comp_cap_applies": scores["comp_cap_applies"],
    }


@router.post("/save")
async def save_job(
    req: SaveJobRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Load the analysis with its saved_job relationship eagerly
    result = await db.execute(
        select(JobAnalysis)
        .options(selectinload(JobAnalysis.saved_job))
        .where(
            JobAnalysis.id == req.job_analysis_id,
            JobAnalysis.user_id == current_user.id,
        )
    )
    analysis = result.scalar_one_or_none()
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")

    if analysis.saved_job:
        return {"status": "already_saved"}

    # Persist SavedJob record
    saved = SavedJob(
        user_id=current_user.id,
        job_analysis_id=analysis.id,
    )
    db.add(saved)
    await db.flush()

    # Write to Google Sheet if connected
    sheets_ok = False
    if current_user.google_oauth_token and current_user.sheet_id:
        job_dict = {
            "company_name": analysis.company_name,
            "job_title": analysis.job_title,
            "job_url": analysis.job_url,
            "industry": analysis.industry or "",
            "location_text": analysis.location_text or "",
            "remote_indicator": analysis.remote_indicator or "",
            "compensation_min": analysis.compensation_min,
            "compensation_max": analysis.compensation_max,
        }
        total = int(
            analysis.fit_score * 0.60
            + analysis.job_quality_score * 0.25
            + analysis.company_score * 0.15
        )
        analysis_dict = {
            "recommendation": analysis.recommendation.value,
            "total_score": total,
            "fit_score": analysis.fit_score,
            "job_quality_score": analysis.job_quality_score,
            "company_score": analysis.company_score,
            "response_percent": analysis.response_percent,
            "response_band": analysis.response_band.value,
            "summary_text": analysis.summary_text,
        }
        sheets_ok, sheet_error = await save_job_to_sheet(
            encrypted_token=current_user.google_oauth_token,
            sheet_id=current_user.sheet_id,
            job_data=job_dict,
            analysis=analysis_dict,
        )
    else:
        sheet_error = ""

    await db.commit()
    return {
        "status": "saved",
        "saved_id": str(saved.id),
        "sheet_updated": sheets_ok,
        "sheet_error": sheet_error,
    }


@router.get("/history")
async def job_history(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(JobAnalysis)
        .where(JobAnalysis.user_id == current_user.id)
        .order_by(JobAnalysis.created_at.desc())
        .limit(50)
    )
    analyses = result.scalars().all()
    return [
        {
            "id": str(a.id),
            "company_name": a.company_name,
            "job_title": a.job_title,
            "recommendation": a.recommendation.value,
            "fit_score": a.fit_score,
            "created_at": a.created_at.isoformat(),
            "saved": a.saved_job is not None,
        }
        for a in analyses
    ]
