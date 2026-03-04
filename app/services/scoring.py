"""
Scoring engine for AppZaddy.

Weights:
  User Fit:     60%  (skills 15%, seniority 25%, industry 15%, comp 15%, location 30%)
  Job Quality:  25%  (baseline 80, <25 applicants +20, 25-100 +10, >100 -10, reposted -10, promoted -10)
  Company:      15%  (baseline 80, funding +20, layoffs -20, sentiment ±20)

Recommendation thresholds:
  >= 75  → strong
  50-74  → strategic
  < 50   → avoid
"""

from __future__ import annotations
import re
from typing import Optional
from app.models.job_analysis import Recommendation, ResponseBand


# ── Industry synonym clusters ──────────────────────────────────────────────
# Any variant within a cluster counts as a match for industry alignment.

_INDUSTRY_CLUSTERS: list[list[str]] = [
    ["software", "saas", "technology", "tech", "platform", "cloud", "developer",
     "devtools", "developer tools", "open source", "enterprise software"],
    ["finance", "fintech", "financial", "banking", "investment", "trading",
     "payments", "capital", "wealth", "insurance", "insurtech", "lending"],
    ["healthcare", "health", "medical", "pharma", "pharmaceutical", "biotech",
     "medtech", "clinical", "hospital", "life sciences", "healthtech"],
    ["marketing", "adtech", "advertising", "media", "digital marketing", "martech",
     "content", "pr", "public relations"],
    ["ecommerce", "e-commerce", "retail", "commerce", "marketplace", "consumer"],
    ["cybersecurity", "security", "infosec", "information security", "devsecops"],
    ["data", "analytics", "data science", "machine learning", "ai",
     "artificial intelligence", "ml", "llm", "deep learning"],
    ["education", "edtech", "learning", "training", "e-learning"],
    ["real estate", "proptech", "property", "construction"],
    ["logistics", "supply chain", "transportation", "shipping", "fulfillment"],
    ["hr", "human resources", "hrtech", "recruiting", "talent", "workforce"],
    ["legal", "legaltech", "law", "compliance", "regtech"],
    ["manufacturing", "industrial", "hardware", "robotics", "automation"],
    ["gaming", "game", "esports", "entertainment", "media"],
    ["telecom", "telecommunications", "wireless", "network", "connectivity"],
    ["energy", "cleantech", "renewable", "utilities", "climate", "sustainability"],
    ["government", "public sector", "civic", "govtech"],
    ["nonprofit", "non-profit", "social impact", "ngo"],
    ["travel", "hospitality", "tourism", "mobility", "rideshare"],
    ["food", "foodtech", "restaurant", "delivery", "agtech", "agriculture"],
]


def _industry_keywords(industry: str) -> set[str]:
    """Expand an industry label to all synonyms in its cluster."""
    normalized = industry.lower().strip()
    # Split on common separators to get individual tokens
    tokens = set(re.split(r"[\s/,&]+", normalized))
    tokens.discard("")
    # Expand with full cluster if any token or substring matches a cluster entry
    for cluster in _INDUSTRY_CLUSTERS:
        if any(kw in normalized or normalized in kw for kw in cluster) or \
           any(token in cluster for token in tokens):
            return set(cluster)
    return tokens


# ── Skill alias map ────────────────────────────────────────────────────────
# Maps a normalized skill name to additional forms that count as equivalent.
# Lookup is bidirectional — the reverse entries are built programmatically below.

_SKILL_ALIAS_MAP: dict[str, list[str]] = {
    "javascript":        ["js", "ecmascript", "es6", "vanilla js"],
    "typescript":        ["ts"],
    "python":            ["py"],
    "react":             ["reactjs", "react.js", "react native"],
    "node":              ["nodejs", "node.js"],
    "vue":               ["vuejs", "vue.js"],
    "angular":           ["angularjs"],
    "kubernetes":        ["k8s"],
    "aws":               ["amazon web services"],
    "gcp":               ["google cloud platform", "google cloud"],
    "azure":             ["microsoft azure"],
    "machine learning":  ["ml", "deep learning", "neural network"],
    "artificial intelligence": ["ai", "gen ai", "generative ai", "llm"],
    "postgresql":        ["postgres"],
    "mongodb":           ["mongo"],
    "graphql":           ["graph ql"],
    "dotnet":            [".net", "asp.net"],
    "golang":            ["go lang"],
    "c#":                ["csharp", "c sharp"],
    "c++":               ["cpp"],
    "devops":            ["ci/cd", "cicd", "continuous integration"],
    "elasticsearch":     ["elastic"],
    "redis":             ["redis cache"],
    "terraform":         ["infrastructure as code", "iac"],
    "scikit-learn":      ["sklearn", "scikit learn"],
}

# Build reverse entries so alias → canonical also works
for _canon, _aliases in list(_SKILL_ALIAS_MAP.items()):
    for _alias in _aliases:
        if _alias not in _SKILL_ALIAS_MAP:
            _SKILL_ALIAS_MAP[_alias] = [_canon]


def _normalize_skill(skill: str) -> str:
    """Strip version numbers, .js/.ts suffixes, and extra whitespace."""
    s = skill.lower().strip()
    s = re.sub(r"\s*v?\d+(\.\d+)*\s*$", "", s)   # "Python 3.11" → "python"
    s = re.sub(r"\.(js|ts|py|rb|go|net)\s*$", "", s)  # "react.js" → "react"
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _skill_in_text(skill: str, text: str) -> bool:
    """Return True if skill (or any of its aliases) appears in text."""
    normalized = _normalize_skill(skill)

    # 1. Direct match after normalization
    if normalized in text:
        return True

    # 2. Alias matches
    for alias in _SKILL_ALIAS_MAP.get(normalized, []):
        if alias in text:
            return True

    # 3. For multi-word skills, check the longest meaningful token
    #    (avoids false positives from short words like "go", "r", "c")
    tokens = [t for t in normalized.split() if len(t) >= 5]
    if tokens:
        primary = max(tokens, key=len)
        if primary in text:
            return True

    return False


# ── Sub-score helpers ──────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    return text.lower().strip()


def compute_skills_match(job_description: str, user_skills: list[str]) -> float:
    """Returns 0.0-1.0 based on skill overlap, using alias/cluster expansion."""
    if not user_skills:
        return 0.3
    jd = _normalize(job_description)
    matched = sum(1 for s in user_skills if _skill_in_text(s, jd))
    ratio = matched / len(user_skills)
    # Diminishing returns above 60% overlap
    return min(1.0, 0.6 + ratio * 0.4)


def compute_seniority_alignment(
    job_description: str,
    job_title: str,
    user_seniority: str,
) -> float:
    """
    Infer required seniority from job title/description keywords
    and compare to user's seniority.
    """
    seniority_order = [
        "intern", "junior", "mid", "senior", "staff",
        "principal", "director", "vp", "c_level",
    ]

    def infer_jd_seniority(title: str, desc: str) -> str:
        combined = _normalize(title + " " + desc[:300])
        if any(k in combined for k in ["vp", "vice president", "svp"]):
            return "vp"
        if any(k in combined for k in ["director", "head of"]):
            return "director"
        if any(k in combined for k in ["principal", "distinguished"]):
            return "principal"
        if any(k in combined for k in ["staff engineer", "staff "]):
            return "staff"
        if "senior" in combined or "sr." in combined:
            return "senior"
        if "junior" in combined or "jr." in combined or "entry level" in combined:
            return "junior"
        if "intern" in combined:
            return "intern"
        return "mid"

    required = infer_jd_seniority(job_title, job_description)
    req_idx = seniority_order.index(required) if required in seniority_order else 3
    usr_idx = seniority_order.index(user_seniority) if user_seniority in seniority_order else 3

    diff = abs(req_idx - usr_idx)
    if diff == 0:
        return 1.0
    if diff == 1:
        return 0.75
    if diff == 2:
        return 0.4
    return 0.1


def compute_industry_alignment(
    company_name: str,
    job_description: str,
    user_industries: list[str],
) -> float:
    if not user_industries:
        return 0.5
    combined = _normalize(company_name + " " + job_description[:400])
    # Expand each industry to its full synonym cluster before checking
    matched = sum(
        1 for ind in user_industries
        if any(kw in combined for kw in _industry_keywords(ind))
    )
    if matched > 0:
        return min(1.0, 0.5 + matched * 0.25)
    return 0.3


def compute_comp_alignment(
    comp_min: Optional[int],
    comp_max: Optional[int],
    desired_min: Optional[int],
    desired_target: Optional[int],
    flexibility: str,
) -> float:
    if not desired_min or (not comp_min and not comp_max):
        return 0.75  # unknown — neutral

    effective_max = comp_max or comp_min

    if effective_max >= desired_min:
        return 1.0  # meets or exceeds minimum

    gap_ratio = (desired_min - effective_max) / desired_min
    if gap_ratio <= 0.15:
        return 0.75  # within 15% of minimum

    return 0.5  # exceeds tolerance


def compute_location_alignment(
    job_remote: str,
    job_location: str,
    preferred_remote: list[str],
    preferred_regions: list[str],
    relocate_willing: bool,
) -> float:
    # "open" means any work type is acceptable
    if "open" in preferred_remote:
        return 1.0
    # Job matches any of the user's preferred work types
    if job_remote in preferred_remote:
        return 1.0
    return 0.5


def compute_job_quality(
    applicant_count: Optional[int],
    reposted: bool,
    promoted: bool,
    posted_days_ago: Optional[int],
) -> float:
    score = 0.8  # baseline

    if applicant_count is not None:
        if applicant_count < 25:
            score += 0.2
        elif applicant_count < 100:
            score += 0.1
        elif applicant_count > 100:
            score -= 0.1

    if reposted:
        score -= 0.1
    if promoted:
        score -= 0.1
    if posted_days_ago is not None:
        if posted_days_ago > 60:
            score -= 0.1
        elif posted_days_ago <= 3:
            score += 0.1

    return max(0.0, min(1.0, score))


def compute_company_score(
    funding_detected: bool,
    layoff_detected: bool,
    scam_flag: bool,
    sentiment: str,
) -> float:
    if scam_flag:
        return 0.0

    score = 0.8  # baseline
    if funding_detected:
        score += 0.2
    if layoff_detected:
        score -= 0.2
    if sentiment == "positive":
        score += 0.2
    elif sentiment == "negative":
        score -= 0.2

    return max(0.0, min(1.0, score))


# ── Main scoring function ──────────────────────────────────────────────────

def compute_full_score(
    job_data: dict,
    user_resume: dict,
    user_prefs: dict,
    company_research: dict,
) -> dict:
    """
    Returns a dict with all scores and the final recommendation.

    job_data keys: job_title, company_name, job_description,
        applicant_count, reposted_flag, promoted_flag, compensation_min,
        compensation_max, location_text, remote_indicator, posted_days_ago

    user_resume keys: skills, titles, seniority_level, industries,
        years_experience

    user_prefs keys: desired_min_comp, desired_target_comp, comp_flexibility,
        remote_preference, preferred_regions, relocate_willing

    company_research keys: funding_detected, layoff_detected, scam_flag,
        sentiment, summary
    """

    skills = user_resume.get("skills", [])
    # Resume JSON stores this as "inferred_seniority_level" (from Claude parser output)
    seniority = user_resume.get("inferred_seniority_level", user_resume.get("seniority_level", "mid"))
    industries = user_resume.get("industries", [])

    jd = job_data.get("job_description", "")
    job_title = job_data.get("job_title", "")
    company = job_data.get("company_name", "")

    # ── User Fit (60%) ────────────────────────────────────────────────────
    skills_score = compute_skills_match(jd, skills)
    seniority_score = compute_seniority_alignment(jd, job_title, seniority)
    industry_score = compute_industry_alignment(company, jd, industries)

    comp_min = job_data.get("compensation_min")
    comp_max = job_data.get("compensation_max")
    desired_min = user_prefs.get("desired_min_comp")
    desired_target = user_prefs.get("desired_target_comp")
    flexibility = user_prefs.get("comp_flexibility", "moderate")
    comp_score = compute_comp_alignment(
        comp_min, comp_max, desired_min, desired_target, flexibility
    )

    remote = job_data.get("remote_indicator", "unknown")
    location = job_data.get("location_text", "")
    pref_remote = user_prefs.get("remote_preference", "open")
    pref_regions = user_prefs.get("preferred_regions", [])
    relocate = user_prefs.get("relocate_willing", False)
    location_score = compute_location_alignment(
        remote, location, pref_remote, pref_regions, relocate
    )

    # Seniority override: reduce penalty when comp is well-aligned
    if comp_score >= 0.7 and seniority_score < 0.5:
        seniority_score = seniority_score + (0.5 - seniority_score) * 0.5

    user_fit = (
        skills_score * 0.15
        + seniority_score * 0.25
        + industry_score * 0.15
        + comp_score * 0.15
        + location_score * 0.30
    )

    # ── Job Quality (25%) ─────────────────────────────────────────────────
    jq = compute_job_quality(
        applicant_count=job_data.get("applicant_count"),
        reposted=job_data.get("reposted_flag", False),
        promoted=job_data.get("promoted_flag", False),
        posted_days_ago=job_data.get("posted_days_ago"),
    )

    # ── Company Score (15%) ───────────────────────────────────────────────
    cs = compute_company_score(
        funding_detected=company_research.get("funding_detected", False),
        layoff_detected=company_research.get("layoff_detected", False),
        scam_flag=company_research.get("scam_flag", False),
        sentiment=company_research.get("sentiment", "neutral"),
    )

    # ── Weighted total ────────────────────────────────────────────────────
    total = (user_fit * 0.60 + jq * 0.25 + cs * 0.15) * 100

    # ── Compensation cap ──────────────────────────────────────────────────
    comp_cap_applies = False
    if desired_min and comp_max is not None:
        if comp_max < desired_min * 0.90:  # 10% tolerance
            comp_cap_applies = True

    # ── Determine recommendation ──────────────────────────────────────────
    if comp_cap_applies:
        recommendation = (
            Recommendation.strategic if total >= 50 else Recommendation.avoid
        )
    elif total >= 75:
        recommendation = Recommendation.strong
    elif total >= 50:
        recommendation = Recommendation.strategic
    else:
        recommendation = Recommendation.avoid

    # ── Response likelihood ───────────────────────────────────────────────
    if total >= 75:
        band = ResponseBand.high
        percent = min(95, int(60 + total * 0.4))
    elif total >= 50:
        band = ResponseBand.medium
        percent = int(25 + total * 0.5)
    else:
        band = ResponseBand.low
        percent = max(5, int(total * 0.4))

    return {
        "fit_score": int(user_fit * 100),
        "job_quality_score": int(jq * 100),
        "company_score": int(cs * 100),
        "total_score": int(total),
        "recommendation": recommendation,
        "response_band": band,
        "response_percent": percent,
        "comp_cap_applies": comp_cap_applies,
        # Sub-scores for breakdown
        "sub_scores": {
            "skills_match": round(skills_score, 2),
            "seniority_alignment": round(seniority_score, 2),
            "industry_alignment": round(industry_score, 2),
            "comp_alignment": round(comp_score, 2),
            "location_alignment": round(location_score, 2),
        },
    }
