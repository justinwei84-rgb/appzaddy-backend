from datetime import datetime

from sqlalchemy import Column, String, Integer, Float, Boolean, DateTime

from app.db.database import Base


class SpendLimit(Base):
    __tablename__ = "spend_limits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # "anthropic" or "google_cse"
    api_name = Column(String(50), unique=True, nullable=False)

    daily_limit_usd = Column(Float, nullable=True)
    monthly_limit_usd = Column(Float, nullable=True)
    google_daily_query_limit = Column(Integer, nullable=True)

    enabled = Column(Boolean, default=True, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
