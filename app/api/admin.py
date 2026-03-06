from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text
from pydantic import BaseModel

from app.config import settings
from app.db.database import get_db
from app.models.user import User
from app.models.api_usage import ApiUsage
from app.models.spend_limits import SpendLimit

router = APIRouter()


# ── Auth helper ──────────────────────────────────────────────────────────────

def _check_secret(secret: str):
    if not settings.admin_secret or secret != settings.admin_secret:
        raise HTTPException(status_code=403, detail="Forbidden")


# ── Existing: list users ─────────────────────────────────────────────────────

@router.get("/users")
async def list_users(secret: str, db: AsyncSession = Depends(get_db)):
    _check_secret(secret)
    result = await db.execute(
        select(User.id, User.email, User.created_at).order_by(User.created_at.desc())
    )
    rows = result.all()
    return [{"id": str(r.id), "email": r.email, "created_at": str(r.created_at)} for r in rows]


# ── Stats endpoint ────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats(secret: str, db: AsyncSession = Depends(get_db)):
    _check_secret(secret)

    now_utc = datetime.now(timezone.utc)
    today_start = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc)
    month_start = datetime(now_utc.year, now_utc.month, 1, tzinfo=timezone.utc)
    days_30_ago = today_start - timedelta(days=29)
    days_7_ago = today_start - timedelta(days=6)

    # User.created_at is TIMESTAMP (tz-naive) — strip tzinfo for those comparisons
    today_start_naive = today_start.replace(tzinfo=None)
    month_start_naive = month_start.replace(tzinfo=None)
    days_30_ago_naive = days_30_ago.replace(tzinfo=None)
    days_7_ago_naive = days_7_ago.replace(tzinfo=None)

    # ── User stats ──
    total_users = (await db.execute(select(func.count(User.id)))).scalar()
    new_7d = (await db.execute(
        select(func.count(User.id)).where(User.created_at >= days_7_ago_naive)
    )).scalar()
    new_30d = (await db.execute(
        select(func.count(User.id)).where(User.created_at >= days_30_ago_naive)
    )).scalar()

    # Registrations by day (last 30 days)
    reg_rows = (await db.execute(
        select(
            func.date_trunc("day", User.created_at).label("day"),
            func.count(User.id).label("count"),
        )
        .where(User.created_at >= days_30_ago_naive)
        .group_by(func.date_trunc("day", User.created_at))
        .order_by(func.date_trunc("day", User.created_at))
    )).all()
    registrations = [
        {"date": r.day.strftime("%Y-%m-%d"), "count": r.count}
        for r in reg_rows
    ]

    # ── Today's usage ──
    async def _sum_today(api_name, col):
        return (await db.execute(
            select(func.coalesce(func.sum(col), 0)).where(
                ApiUsage.api_name == api_name,
                ApiUsage.created_at >= today_start,
            )
        )).scalar()

    anthropic_cost_today = await _sum_today("anthropic", ApiUsage.cost_usd)
    anthropic_calls_today = (await db.execute(
        select(func.count(ApiUsage.id)).where(
            ApiUsage.api_name == "anthropic",
            ApiUsage.created_at >= today_start,
        )
    )).scalar()
    google_queries_today = await _sum_today("google_cse", ApiUsage.queries_count)
    google_cost_today = await _sum_today("google_cse", ApiUsage.cost_usd)

    # ── This month's usage ──
    async def _sum_month(api_name, col):
        return (await db.execute(
            select(func.coalesce(func.sum(col), 0)).where(
                ApiUsage.api_name == api_name,
                ApiUsage.created_at >= month_start,
            )
        )).scalar()

    anthropic_cost_month = await _sum_month("anthropic", ApiUsage.cost_usd)
    anthropic_calls_month = (await db.execute(
        select(func.count(ApiUsage.id)).where(
            ApiUsage.api_name == "anthropic",
            ApiUsage.created_at >= month_start,
        )
    )).scalar()
    google_queries_month = await _sum_month("google_cse", ApiUsage.queries_count)
    google_cost_month = await _sum_month("google_cse", ApiUsage.cost_usd)

    # ── All-time totals ──
    anthropic_cost_total = (await db.execute(
        select(func.coalesce(func.sum(ApiUsage.cost_usd), 0)).where(
            ApiUsage.api_name == "anthropic"
        )
    )).scalar()
    google_cost_total = (await db.execute(
        select(func.coalesce(func.sum(ApiUsage.cost_usd), 0)).where(
            ApiUsage.api_name == "google_cse"
        )
    )).scalar()

    # ── By day (last 30 days) ──
    by_day_rows = (await db.execute(
        select(
            func.date_trunc("day", ApiUsage.created_at).label("day"),
            ApiUsage.api_name,
            func.coalesce(func.sum(ApiUsage.cost_usd), 0).label("cost"),
            func.coalesce(func.sum(ApiUsage.queries_count), 0).label("queries"),
            func.count(ApiUsage.id).label("calls"),
        )
        .where(ApiUsage.created_at >= days_30_ago)
        .group_by(func.date_trunc("day", ApiUsage.created_at), ApiUsage.api_name)
        .order_by(func.date_trunc("day", ApiUsage.created_at))
    )).all()

    by_day_map: dict = {}
    for r in by_day_rows:
        d = r.day.strftime("%Y-%m-%d")
        if d not in by_day_map:
            by_day_map[d] = {
                "date": d,
                "anthropic_cost_usd": 0.0,
                "anthropic_calls": 0,
                "google_queries": 0,
                "google_cost_usd": 0.0,
            }
        if r.api_name == "anthropic":
            by_day_map[d]["anthropic_cost_usd"] = round(float(r.cost), 6)
            by_day_map[d]["anthropic_calls"] = r.calls
        elif r.api_name == "google_cse":
            by_day_map[d]["google_queries"] = int(r.queries)
            by_day_map[d]["google_cost_usd"] = round(float(r.cost), 6)
    by_day_30 = list(by_day_map.values())

    # ── By operation ──
    op_rows = (await db.execute(
        select(
            ApiUsage.operation,
            ApiUsage.api_name,
            func.count(ApiUsage.id).label("calls"),
            func.coalesce(func.sum(ApiUsage.cost_usd), 0).label("cost"),
            func.coalesce(func.sum(ApiUsage.tokens_input), 0).label("tokens_in"),
            func.coalesce(func.sum(ApiUsage.tokens_output), 0).label("tokens_out"),
        )
        .group_by(ApiUsage.operation, ApiUsage.api_name)
        .order_by(text("cost DESC"))
    )).all()
    by_operation = [
        {
            "operation": r.operation,
            "api_name": r.api_name,
            "calls": r.calls,
            "cost_usd": round(float(r.cost), 6),
            "tokens_input": int(r.tokens_in),
            "tokens_output": int(r.tokens_out),
        }
        for r in op_rows
    ]

    # ── Recent users (last 50) ──
    user_rows = (await db.execute(
        select(User.email, User.created_at).order_by(User.created_at.desc()).limit(50)
    )).all()
    recent_users = [
        {"email": r.email, "created_at": r.created_at.isoformat() if r.created_at else ""}
        for r in user_rows
    ]

    return {
        "generated_at": now_utc.isoformat(),
        "users": {
            "total": total_users,
            "last_7_days": new_7d,
            "last_30_days": new_30d,
            "registrations_30d": registrations,
            "recent": recent_users,
        },
        "usage": {
            "today": {
                "anthropic_cost_usd": round(float(anthropic_cost_today), 6),
                "anthropic_calls": anthropic_calls_today,
                "google_queries": int(google_queries_today),
                "google_cost_usd": round(float(google_cost_today), 6),
            },
            "this_month": {
                "anthropic_cost_usd": round(float(anthropic_cost_month), 6),
                "anthropic_calls": anthropic_calls_month,
                "google_queries": int(google_queries_month),
                "google_cost_usd": round(float(google_cost_month), 6),
            },
            "total": {
                "anthropic_cost_usd": round(float(anthropic_cost_total), 6),
                "google_cost_usd": round(float(google_cost_total), 6),
                "combined_cost_usd": round(float(anthropic_cost_total + google_cost_total), 6),
            },
            "by_day_30": by_day_30,
            "by_operation": by_operation,
        },
    }


# ── Spend limits endpoints ────────────────────────────────────────────────────

@router.get("/limits")
async def get_limits(secret: str, db: AsyncSession = Depends(get_db)):
    _check_secret(secret)
    rows = (await db.execute(select(SpendLimit).order_by(SpendLimit.api_name))).scalars().all()
    return [
        {
            "api_name": r.api_name,
            "daily_limit_usd": r.daily_limit_usd,
            "monthly_limit_usd": r.monthly_limit_usd,
            "google_daily_query_limit": r.google_daily_query_limit,
            "enabled": r.enabled,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


class LimitUpdate(BaseModel):
    api_name: str
    daily_limit_usd: Optional[float] = None
    monthly_limit_usd: Optional[float] = None
    google_daily_query_limit: Optional[int] = None
    enabled: bool = True


@router.post("/limits")
async def upsert_limit(req: LimitUpdate, secret: str, db: AsyncSession = Depends(get_db)):
    _check_secret(secret)

    result = await db.execute(
        select(SpendLimit).where(SpendLimit.api_name == req.api_name)
    )
    row = result.scalar_one_or_none()
    if row:
        row.daily_limit_usd = req.daily_limit_usd
        row.monthly_limit_usd = req.monthly_limit_usd
        row.google_daily_query_limit = req.google_daily_query_limit
        row.enabled = req.enabled
        row.updated_at = datetime.now(timezone.utc)
    else:
        row = SpendLimit(
            api_name=req.api_name,
            daily_limit_usd=req.daily_limit_usd,
            monthly_limit_usd=req.monthly_limit_usd,
            google_daily_query_limit=req.google_daily_query_limit,
            enabled=req.enabled,
            updated_at=datetime.now(timezone.utc),
        )
        db.add(row)

    await db.commit()
    return {"status": "ok", "api_name": req.api_name}


# ── HTML Dashboard ────────────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>AppZaddy Admin</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    body { background: #0f172a; color: #e2e8f0; font-family: ui-sans-serif, system-ui, sans-serif; }
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 20px; }
    .stat-val { font-size: 2rem; font-weight: 700; color: #f8fafc; }
    .stat-label { font-size: 0.8rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
    th { text-align: left; padding: 8px 12px; color: #94a3b8; font-weight: 500; border-bottom: 1px solid #334155; }
    td { padding: 8px 12px; border-bottom: 1px solid #1e293b; }
    tr:hover td { background: #263347; }
    input[type=number], input[type=text] {
      background: #0f172a; border: 1px solid #475569; color: #e2e8f0;
      padding: 6px 10px; border-radius: 6px; width: 130px; font-size: 0.85rem;
    }
    input[type=checkbox] { width: 16px; height: 16px; cursor: pointer; }
    .btn { background: #3b82f6; color: white; border: none; padding: 7px 18px;
           border-radius: 6px; cursor: pointer; font-size: 0.85rem; font-weight: 500; }
    .btn:hover { background: #2563eb; }
    .btn-sm { padding: 4px 12px; font-size: 0.8rem; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; }
    .badge-indigo { background: #312e81; color: #a5b4fc; }
    .badge-green  { background: #064e3b; color: #34d399; }
    .section-title { font-size: 1rem; font-weight: 600; color: #cbd5e1; margin-bottom: 14px; }
    #toast { position: fixed; bottom: 24px; right: 24px; background: #22c55e; color: white;
             padding: 10px 20px; border-radius: 8px; display: none; font-size: 0.9rem; z-index: 999; }
  </style>
</head>
<body class="min-h-screen">

<div class="max-w-7xl mx-auto px-6 py-8">

  <!-- Header -->
  <div class="flex items-center justify-between mb-8">
    <div>
      <h1 class="text-2xl font-bold text-white">AppZaddy Admin</h1>
      <p id="genAt" class="text-slate-400 text-sm mt-1"></p>
    </div>
    <button class="btn" onclick="loadAll()">&#8635; Refresh</button>
  </div>

  <!-- Stat Cards -->
  <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
    <div class="card"><div class="stat-label">Total Users</div><div class="stat-val" id="sc-users">—</div></div>
    <div class="card"><div class="stat-label">New (7 days)</div><div class="stat-val" id="sc-new7">—</div></div>
    <div class="card"><div class="stat-label">This Month Cost</div><div class="stat-val" id="sc-monthcost">—</div></div>
    <div class="card"><div class="stat-label">Today API Calls</div><div class="stat-val" id="sc-todaycalls">—</div></div>
  </div>

  <!-- Today breakdown -->
  <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">
    <div class="card">
      <div class="section-title">Today</div>
      <div class="grid grid-cols-2 gap-4 text-sm">
        <div><div class="stat-label">Anthropic Cost</div><div class="text-lg font-semibold text-indigo-400" id="td-acost">—</div></div>
        <div><div class="stat-label">Anthropic Calls</div><div class="text-lg font-semibold text-indigo-400" id="td-acalls">—</div></div>
        <div><div class="stat-label">Google Queries</div><div class="text-lg font-semibold text-emerald-400" id="td-gq">—</div></div>
        <div><div class="stat-label">Google Cost</div><div class="text-lg font-semibold text-emerald-400" id="td-gcost">—</div></div>
      </div>
    </div>
    <div class="card">
      <div class="section-title">This Month</div>
      <div class="grid grid-cols-2 gap-4 text-sm">
        <div><div class="stat-label">Anthropic Cost</div><div class="text-lg font-semibold text-indigo-400" id="mo-acost">—</div></div>
        <div><div class="stat-label">Anthropic Calls</div><div class="text-lg font-semibold text-indigo-400" id="mo-acalls">—</div></div>
        <div><div class="stat-label">Google Queries</div><div class="text-lg font-semibold text-emerald-400" id="mo-gq">—</div></div>
        <div><div class="stat-label">Google Cost</div><div class="text-lg font-semibold text-emerald-400" id="mo-gcost">—</div></div>
      </div>
    </div>
  </div>

  <!-- Charts -->
  <div class="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
    <div class="card">
      <div class="section-title">API Cost — Last 30 Days (USD)</div>
      <canvas id="costChart" height="200"></canvas>
    </div>
    <div class="card">
      <div class="section-title">Google CSE Queries — Last 30 Days</div>
      <canvas id="queryChart" height="200"></canvas>
    </div>
  </div>

  <!-- By Operation -->
  <div class="card mb-8">
    <div class="section-title">Usage by Operation (All Time)</div>
    <table>
      <thead><tr><th>Operation</th><th>API</th><th>Calls</th><th>Tokens In</th><th>Tokens Out</th><th>Cost (USD)</th></tr></thead>
      <tbody id="opTable"></tbody>
    </table>
  </div>

  <!-- Spend Limits -->
  <div class="card mb-8">
    <div class="section-title">Spend Limits</div>
    <p class="text-slate-400 text-sm mb-4">Set to blank to remove a limit. Changes take effect immediately on all new API calls.</p>
    <div id="limitsForm"></div>
  </div>

  <!-- User Registrations -->
  <div class="card mb-8">
    <div class="section-title">Recent Registrations (last 50)</div>
    <table>
      <thead><tr><th>Email</th><th>Registered</th></tr></thead>
      <tbody id="userTable"></tbody>
    </table>
  </div>

</div>

<div id="toast">Saved!</div>

<script>
const secret = new URLSearchParams(location.search).get('secret') || '';

let costChart = null, queryChart = null;

async function apiFetch(path) {
  const sep = path.includes('?') ? '&' : '?';
  const r = await fetch(path + sep + 'secret=' + encodeURIComponent(secret));
  if (!r.ok) { const t = await r.text(); throw new Error(t); }
  return r.json();
}

async function loadAll() {
  try {
    const [stats, limits] = await Promise.all([apiFetch('/admin/stats'), apiFetch('/admin/limits')]);
    renderStats(stats);
    renderLimits(limits, stats);
  } catch(e) { alert('Error: ' + e.message); }
}

function fmt$(v) { return '$' + Number(v).toFixed(4); }
function fmtDate(iso) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleString('en-US', {month:'short',day:'numeric',year:'numeric',hour:'2-digit',minute:'2-digit'}); }
  catch { return iso; }
}

function renderStats(s) {
  document.getElementById('genAt').textContent = 'Last updated: ' + fmtDate(s.generated_at);
  document.getElementById('sc-users').textContent = s.users.total;
  document.getElementById('sc-new7').textContent = s.users.last_7_days;
  const monthCost = s.usage.this_month.anthropic_cost_usd + s.usage.this_month.google_cost_usd;
  document.getElementById('sc-monthcost').textContent = fmt$(monthCost);
  document.getElementById('sc-todaycalls').textContent =
    s.usage.today.anthropic_calls + s.usage.today.google_queries;

  // Today breakdown
  document.getElementById('td-acost').textContent = fmt$(s.usage.today.anthropic_cost_usd);
  document.getElementById('td-acalls').textContent = s.usage.today.anthropic_calls;
  document.getElementById('td-gq').textContent = s.usage.today.google_queries;
  document.getElementById('td-gcost').textContent = fmt$(s.usage.today.google_cost_usd);

  // Month breakdown
  document.getElementById('mo-acost').textContent = fmt$(s.usage.this_month.anthropic_cost_usd);
  document.getElementById('mo-acalls').textContent = s.usage.this_month.anthropic_calls;
  document.getElementById('mo-gq').textContent = s.usage.this_month.google_queries;
  document.getElementById('mo-gcost').textContent = fmt$(s.usage.this_month.google_cost_usd);

  // Cost chart
  const days = s.usage.by_day_30;
  const labels = days.map(d => d.date.slice(5));
  if (costChart) costChart.destroy();
  costChart = new Chart(document.getElementById('costChart'), {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Anthropic', data: days.map(d => d.anthropic_cost_usd), backgroundColor: '#6366f1', stack: 'a' },
        { label: 'Google CSE', data: days.map(d => d.google_cost_usd), backgroundColor: '#10b981', stack: 'a' },
      ]
    },
    options: { responsive: true,
      plugins: { legend: { labels: { color: '#94a3b8' } } },
      scales: {
        x: { ticks: { color: '#64748b' }, grid: { color: '#1e293b' } },
        y: { ticks: { color: '#64748b', callback: v => '$'+Number(v).toFixed(4) }, grid: { color: '#334155' } }
      }
    }
  });

  if (queryChart) queryChart.destroy();
  queryChart = new Chart(document.getElementById('queryChart'), {
    type: 'bar',
    data: { labels, datasets: [{ label: 'Queries', data: days.map(d => d.google_queries), backgroundColor: '#0ea5e9' }] },
    options: { responsive: true,
      plugins: { legend: { labels: { color: '#94a3b8' } } },
      scales: {
        x: { ticks: { color: '#64748b' }, grid: { color: '#1e293b' } },
        y: { ticks: { color: '#64748b' }, grid: { color: '#334155' } }
      }
    }
  });

  // Operation table
  const opTb = document.getElementById('opTable');
  let totalCost = 0;
  opTb.innerHTML = s.usage.by_operation.map(r => {
    totalCost += r.cost_usd;
    const badge = r.api_name === 'anthropic'
      ? '<span class="badge badge-indigo">anthropic</span>'
      : '<span class="badge badge-green">google_cse</span>';
    return `<tr>
      <td>${r.operation}</td><td>${badge}</td><td>${r.calls}</td>
      <td>${Number(r.tokens_input).toLocaleString()}</td>
      <td>${Number(r.tokens_output).toLocaleString()}</td>
      <td>${fmt$(r.cost_usd)}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" style="color:#64748b;text-align:center;padding:20px">No usage data yet</td></tr>';
  opTb.innerHTML += `<tr style="background:#1e3a5f;font-weight:600">
    <td colspan="5" style="color:#94a3b8">All-time combined cost</td>
    <td>${fmt$(s.usage.total.combined_cost_usd)}</td>
  </tr>`;

  // User table
  const uTb = document.getElementById('userTable');
  uTb.innerHTML = s.users.recent.map(u =>
    `<tr><td>${u.email}</td><td style="color:#64748b">${fmtDate(u.created_at)}</td></tr>`
  ).join('') || '<tr><td colspan="2" style="color:#64748b;text-align:center;padding:20px">No users yet</td></tr>';
}

function renderLimits(limits, stats) {
  const map = {};
  limits.forEach(l => map[l.api_name] = l);

  const apis = [
    { key: 'anthropic', label: 'Anthropic (Claude)', todayInfo: `Today: ${fmt$(stats.usage.today.anthropic_cost_usd)} / ${stats.usage.today.anthropic_calls} calls`, showQueries: false },
    { key: 'google_cse', label: 'Google Custom Search', todayInfo: `Today: ${stats.usage.today.google_queries} queries (100 free/day)`, showQueries: true },
  ];

  document.getElementById('limitsForm').innerHTML = apis.map(a => {
    const l = map[a.key] || { daily_limit_usd: null, monthly_limit_usd: null, google_daily_query_limit: null, enabled: true };
    return `
    <div class="mb-6 pb-6 border-b border-slate-700 last:border-0 last:mb-0 last:pb-0">
      <div class="flex flex-wrap items-center gap-3 mb-3">
        <span class="font-semibold text-white">${a.label}</span>
        <span class="text-slate-400 text-xs">${a.todayInfo}</span>
        <label class="ml-auto flex items-center gap-2 text-sm text-slate-300 cursor-pointer">
          <input type="checkbox" id="${a.key}-enabled" ${l.enabled ? 'checked' : ''}> Limits enabled
        </label>
      </div>
      <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div>
          <div class="text-slate-400 text-xs mb-1">Daily USD limit</div>
          <input type="number" id="${a.key}-daily" step="0.01" min="0" placeholder="No limit" value="${l.daily_limit_usd ?? ''}">
        </div>
        <div>
          <div class="text-slate-400 text-xs mb-1">Monthly USD limit</div>
          <input type="number" id="${a.key}-monthly" step="0.01" min="0" placeholder="No limit" value="${l.monthly_limit_usd ?? ''}">
        </div>
        ${a.showQueries ? `<div>
          <div class="text-slate-400 text-xs mb-1">Daily query cap <span class="text-slate-500">(first 100 free)</span></div>
          <input type="number" id="${a.key}-queries" step="1" min="0" placeholder="No limit" value="${l.google_daily_query_limit ?? ''}">
        </div>` : '<div></div>'}
      </div>
      <button class="btn btn-sm mt-3" onclick="saveLimit('${a.key}')">Save ${a.label} Limits</button>
    </div>`;
  }).join('');
}

async function saveLimit(api) {
  const daily   = document.getElementById(api + '-daily')?.value;
  const monthly = document.getElementById(api + '-monthly')?.value;
  const queries = document.getElementById(api + '-queries')?.value;
  const enabled = document.getElementById(api + '-enabled')?.checked ?? true;

  const body = {
    api_name: api,
    daily_limit_usd: daily ? parseFloat(daily) : null,
    monthly_limit_usd: monthly ? parseFloat(monthly) : null,
    google_daily_query_limit: queries ? parseInt(queries) : null,
    enabled,
  };

  try {
    const r = await fetch('/admin/limits?secret=' + encodeURIComponent(secret), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text());
    const t = document.getElementById('toast');
    t.style.display = 'block';
    setTimeout(() => t.style.display = 'none', 2500);
  } catch(e) { alert('Error saving: ' + e.message); }
}

loadAll();
</script>
</body>
</html>"""


@router.get("/dashboard", response_class=HTMLResponse)
async def admin_dashboard(secret: str):
    _check_secret(secret)
    return HTMLResponse(content=_DASHBOARD_HTML)
