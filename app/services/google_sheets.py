"""
Google Sheets integration for saving jobs.
Uses the user's stored OAuth token to write rows via direct HTTP calls.
"""

import json
import httpx
from cryptography.fernet import Fernet

from app.config import settings

SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"

HEADERS_ROW = [
    "Date", "Company", "Industry", "Job Title", "Location", "Pay Band",
    "Recommendation", "Total Score", "Fit Score", "Job Quality", "Company Score",
    "Response %", "Response Band", "Job URL", "Summary",
]


def _get_fernet() -> Fernet:
    key = settings.encryption_key
    if not key:
        raise ValueError("ENCRYPTION_KEY not configured")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_token(token_data: dict) -> str:
    f = _get_fernet()
    return f.encrypt(json.dumps(token_data).encode()).decode()


def decrypt_token(encrypted: str) -> dict:
    f = _get_fernet()
    return json.loads(f.decrypt(encrypted.encode()).decode())


async def _sheets_get(client: httpx.AsyncClient, access_token: str, sheet_id: str, range_: str) -> dict:
    resp = await client.get(
        f"{SHEETS_BASE}/{sheet_id}/values/{range_}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if resp.status_code == 401:
        raise ValueError("Google token expired — reconnect Google Sheet from the extension popup.")
    resp.raise_for_status()
    return resp.json()


async def _sheets_append(client: httpx.AsyncClient, access_token: str, sheet_id: str, range_: str, values: list) -> None:
    resp = await client.post(
        f"{SHEETS_BASE}/{sheet_id}/values/{range_}:append",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"valueInputOption": "USER_ENTERED"},
        json={"values": values},
    )
    if resp.status_code == 401:
        raise ValueError("Google token expired — reconnect Google Sheet from the extension popup.")
    resp.raise_for_status()


async def save_job_to_sheet(
    encrypted_token: str,
    sheet_id: str,
    job_data: dict,
    analysis: dict,
) -> tuple[bool, str]:
    """
    Append a job analysis row to the user's Google Sheet.
    Returns (True, "") on success or (False, error_message) on failure.
    """
    try:
        token_data = decrypt_token(encrypted_token)
        access_token = token_data.get("access_token")
        if not access_token:
            return False, "No access token stored — reconnect Google Sheet."

        from datetime import datetime

        # Build Pay Band string
        comp_min = job_data.get("compensation_min")
        comp_max = job_data.get("compensation_max")
        if comp_min and comp_max:
            pay_band = f"${comp_min // 1000}k – ${comp_max // 1000}k"
        elif comp_min:
            pay_band = f"${comp_min // 1000}k+"
        else:
            pay_band = ""

        # Build Location string — only annotate remote/hybrid, not onsite
        location = job_data.get("location_text", "")
        remote = job_data.get("remote_indicator", "")
        if remote in ("remote", "hybrid"):
            label = remote.capitalize()
            location_display = f"{location} ({label})" if location else label
        else:
            location_display = location

        row = [
            datetime.utcnow().strftime("%Y-%m-%d"),
            job_data.get("company_name", ""),
            job_data.get("industry", ""),
            job_data.get("job_title", ""),
            location_display,
            pay_band,
            analysis.get("recommendation", "").upper(),
            analysis.get("total_score", 0),
            analysis.get("fit_score", 0),
            analysis.get("job_quality_score", 0),
            analysis.get("company_score", 0),
            f"{analysis.get('response_percent', 0)}%",
            analysis.get("response_band", ""),
            job_data.get("job_url", ""),
            analysis.get("summary_text", ""),
        ]

        async with httpx.AsyncClient(timeout=15) as client:
            # Write header row if sheet is empty
            data = await _sheets_get(client, access_token, sheet_id, "A1:Z1")
            if not data.get("values"):
                await _sheets_append(client, access_token, sheet_id, "A1", [HEADERS_ROW])

            # Append the job row (15 columns: A through O)
            await _sheets_append(client, access_token, sheet_id, "A:O", [row])

        return True, ""
    except Exception as e:
        import traceback
        print(f"Google Sheets error: {e}\n{traceback.format_exc()}")
        return False, str(e)
