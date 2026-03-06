"""
Claude API integration for AppZaddy.

Uses claude-opus-4-6 for:
  - Resume parsing (JSON prompt → parse response)
  - Company research summarization
  - Scoring narrative generation

Each function returns (result, ClaudeUsage) so callers can record token usage.
"""

import json
import re
from dataclasses import dataclass
from pydantic import BaseModel
from typing import List, Tuple
import anthropic

from app.config import settings

client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

MODEL = "claude-opus-4-6"

# Pricing for claude-opus-4-6  (update here if Anthropic changes rates)
_INPUT_COST_PER_TOKEN = 15.0 / 1_000_000   # $15 per million input tokens
_OUTPUT_COST_PER_TOKEN = 75.0 / 1_000_000  # $75 per million output tokens


@dataclass
class ClaudeUsage:
    input_tokens: int
    output_tokens: int
    cost_usd: float


def _extract_json(text: str) -> str:
    """Strip markdown code fences if Claude wraps the JSON."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        return m.group(1).strip()
    return text


def _compute_usage(response) -> ClaudeUsage:
    inp = response.usage.input_tokens
    out = response.usage.output_tokens
    cost = inp * _INPUT_COST_PER_TOKEN + out * _OUTPUT_COST_PER_TOKEN
    return ClaudeUsage(input_tokens=inp, output_tokens=out, cost_usd=cost)


# ── Pydantic schemas ────────────────────────────────────────────────────────

class ResumeStructured(BaseModel):
    skills: List[str]
    titles: List[str]
    inferred_seniority_level: str  # intern | junior | mid | senior | staff | principal | director | vp | c_level
    industries: List[str]
    years_experience: int
    user_location: str


class CompanyResearchResult(BaseModel):
    funding_detected: bool
    layoff_detected: bool
    scam_flag: bool
    sentiment: str  # positive | neutral | negative
    industry: str = ""  # e.g. "Software / Technology", "Healthcare", etc.
    summary: str


class ScoringNarrative(BaseModel):
    summary_text: str
    top_drivers: List[str]
    red_flags: List[str]


# ── Resume parsing ──────────────────────────────────────────────────────────

async def parse_resume_text(text: str) -> Tuple[ResumeStructured, ClaudeUsage]:
    """Extract structured information from raw resume text using Claude."""

    system = (
        "You are a precise resume parser. Extract structured information from the "
        "provided resume text. Return ONLY valid JSON — no explanation, no markdown fences — "
        "matching exactly this schema:\n"
        '{"skills": ["..."], "titles": ["..."], "inferred_seniority_level": "...", '
        '"industries": ["..."], "years_experience": 0, "user_location": "..."}\n'
        "For inferred_seniority_level use exactly one of: "
        "intern, junior, mid, senior, staff, principal, director, vp, c_level."
    )

    response = await client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=system,
        messages=[
            {
                "role": "user",
                "content": f"Parse this resume:\n\n{text}",
            }
        ],
    )

    raw = response.content[0].text
    data = json.loads(_extract_json(raw))
    return ResumeStructured(**data), _compute_usage(response)


# ── Company research summarization ─────────────────────────────────────────

async def summarize_company_research(
    company_name: str, snippets: List[str]
) -> Tuple[CompanyResearchResult, ClaudeUsage]:
    """Given raw search snippets about a company, produce a structured summary."""

    combined = "\n".join(f"- {s}" for s in snippets[:20])

    system = (
        "You are a business intelligence analyst helping job seekers evaluate companies. "
        "Analyze the search results and return ONLY valid JSON — no explanation, no markdown — "
        "matching exactly this schema:\n"
        '{"funding_detected": true/false, "layoff_detected": true/false, '
        '"scam_flag": true/false, "sentiment": "positive|neutral|negative", '
        '"industry": "short industry label e.g. Software / Technology", '
        '"summary": "2-4 sentences focused on job-seeker concerns"}\n\n'
        "Guidelines:\n"
        "- scam_flag: set to true only if there is affirmative evidence of fraud, deceptive practices, "
        "fake job postings, or overwhelmingly negative sentiment suggesting misconduct. "
        "Lack of online presence alone is NOT sufficient to flag as a scam — many legitimate small or "
        "stealth companies have limited public footprint.\n"
        "- sentiment: base this on the overall tone of reviews, news, and public perception. "
        "Consistently negative employee reviews, misconduct allegations, or patterns of complaints "
        "should result in negative sentiment, which may also correlate with scam_flag being true.\n"
        "- layoff_detected: set to true only if there is clear evidence of recent layoffs or workforce reductions."
    )

    response = await client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Company: {company_name}\n\n"
                    f"Search result snippets:\n{combined}\n\n"
                    "Analyze and return JSON."
                ),
            }
        ],
    )

    raw = response.content[0].text
    data = json.loads(_extract_json(raw))
    return CompanyResearchResult(**data), _compute_usage(response)


# ── Scoring narrative ───────────────────────────────────────────────────────

async def generate_scoring_narrative(
    job_title: str,
    company_name: str,
    recommendation: str,
    fit_score: int,
    job_quality_score: int,
    company_score: int,
    response_percent: int,
    user_skills: List[str],
    job_description_snippet: str,
    company_summary: str,
) -> Tuple[ScoringNarrative, ClaudeUsage]:
    """Generate a human-readable summary and key drivers for the analysis result."""

    system = (
        "You are a career coach giving honest, direct advice about a job opportunity. "
        "Be specific, concise, and actionable. No fluff. "
        "Return ONLY valid JSON — no explanation, no markdown — matching exactly this schema:\n"
        '{"summary_text": "...", "top_drivers": ["...", "..."], "red_flags": ["..."]}'
    )

    context = (
        f"Job: {job_title} at {company_name}\n"
        f"Recommendation: {recommendation.upper()}\n"
        f"Fit Score: {fit_score}/100\n"
        f"Job Quality Score: {job_quality_score}/100\n"
        f"Company Score: {company_score}/100\n"
        f"Estimated Response Rate: {response_percent}%\n\n"
        f"User Skills: {', '.join(user_skills[:10])}\n\n"
        f"Job Description (excerpt): {job_description_snippet[:500]}\n\n"
        f"Company Intelligence: {company_summary}\n\n"
        "Provide:\n"
        "- summary_text: 2-3 sentence honest assessment\n"
        "- top_drivers: 2-4 specific reasons this is a good/bad fit\n"
        "- red_flags: 0-3 specific concerns (empty list if none)"
    )

    response = await client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": context}],
    )

    raw = response.content[0].text
    data = json.loads(_extract_json(raw))
    return ScoringNarrative(**data), _compute_usage(response)
