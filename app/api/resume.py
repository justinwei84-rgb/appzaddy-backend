from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db.database import get_db
from app.models.user import User
from app.models.resume import UserResume, SeniorityLevel
from app.api.auth import get_current_user
from app.services.resume_parser import extract_resume_text
from app.services.claude_service import parse_resume_text
from app.services.usage_tracker import check_spend_limit, record_usage

router = APIRouter()


class AddSkillsRequest(BaseModel):
    skills: list[str]


class AddIndustriesRequest(BaseModel):
    industries: list[str]


@router.post("/upload")
async def upload_resume(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if file.size and file.size > 10 * 1024 * 1024:  # 10 MB limit
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")

    # 1. Extract raw text
    text = await extract_resume_text(file)

    # 2. Check spend limit before calling Claude
    await check_spend_limit("anthropic", db)

    # 3. Parse with Claude
    structured, claude_usage = await parse_resume_text(text)

    # 4. Record usage
    await record_usage(
        db,
        api_name="anthropic",
        operation="resume_parse",
        user_id=current_user.id,
        tokens_input=claude_usage.input_tokens,
        tokens_output=claude_usage.output_tokens,
        cost_usd=claude_usage.cost_usd,
    )

    # 5. Map seniority
    try:
        seniority = SeniorityLevel(structured.inferred_seniority_level)
    except ValueError:
        seniority = SeniorityLevel.mid

    # 6. Upsert UserResume
    resume = current_user.resume
    if not resume:
        resume = UserResume(user_id=current_user.id)
        db.add(resume)

    resume.structured_json = structured.model_dump()
    resume.seniority_level = seniority
    resume.industries = structured.industries
    resume.years_experience = structured.years_experience

    await db.commit()

    return {
        "status": "ok",
        "seniority_level": seniority.value,
        "skills_count": len(structured.skills),
        "industries": structured.industries,
        "years_experience": structured.years_experience,
    }


@router.post("/skills")
async def add_skills(
    req: AddSkillsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    resume = current_user.resume
    if not resume:
        raise HTTPException(status_code=404, detail="No resume uploaded yet")

    existing: list[str] = resume.structured_json.get("skills", [])
    existing_lower = {s.lower() for s in existing}

    new_skills = [
        s.strip()
        for s in req.skills
        if s.strip() and s.strip().lower() not in existing_lower
    ]

    updated = existing + new_skills
    resume.structured_json = {**resume.structured_json, "skills": updated}
    # Flag the JSONB column as modified so SQLAlchemy flushes it
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(resume, "structured_json")
    await db.commit()

    return {"skills": updated}


@router.post("/industries")
async def add_industries(
    req: AddIndustriesRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    resume = current_user.resume
    if not resume:
        raise HTTPException(status_code=404, detail="No resume uploaded yet")

    existing: list[str] = resume.structured_json.get("industries", [])
    existing_lower = {i.lower() for i in existing}

    new_industries = [
        i.strip()
        for i in req.industries
        if i.strip() and i.strip().lower() not in existing_lower
    ]

    updated = existing + new_industries
    resume.structured_json = {**resume.structured_json, "industries": updated}
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(resume, "structured_json")
    await db.commit()

    return {"industries": updated}
