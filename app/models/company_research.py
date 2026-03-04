import uuid
from datetime import datetime
import enum
from sqlalchemy import String, Boolean, Text, TIMESTAMP, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class CompanySentiment(str, enum.Enum):
    positive = "positive"
    neutral = "neutral"
    negative = "negative"


class CompanyResearchCache(Base):
    __tablename__ = "company_research_cache"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_name_normalized: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True, unique=True
    )
    funding_detected: Mapped[bool] = mapped_column(Boolean, default=False)
    layoff_detected: Mapped[bool] = mapped_column(Boolean, default=False)
    scam_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    sentiment: Mapped[CompanySentiment] = mapped_column(
        SAEnum(CompanySentiment), default=CompanySentiment.neutral
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, default=datetime.utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
