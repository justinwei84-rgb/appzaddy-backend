import uuid
from datetime import datetime
import enum
from sqlalchemy import String, Integer, Text, TIMESTAMP, Enum as SAEnum, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class Recommendation(str, enum.Enum):
    strong = "strong"
    strategic = "strategic"
    avoid = "avoid"


class ResponseBand(str, enum.Enum):
    high = "high"
    medium = "medium"
    low = "low"


class JobAnalysis(Base):
    __tablename__ = "job_analyses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    job_title: Mapped[str] = mapped_column(String(255), nullable=False)
    job_url: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation: Mapped[Recommendation] = mapped_column(
        SAEnum(Recommendation), nullable=False
    )
    response_band: Mapped[ResponseBand] = mapped_column(
        SAEnum(ResponseBand), nullable=False
    )
    response_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    fit_score: Mapped[int] = mapped_column(Integer, nullable=False)
    job_quality_score: Mapped[int] = mapped_column(Integer, nullable=False)
    company_score: Mapped[int] = mapped_column(Integer, nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    industry: Mapped[str] = mapped_column(String(255), nullable=True, default="")
    location_text: Mapped[str] = mapped_column(String(255), nullable=True, default="")
    remote_indicator: Mapped[str] = mapped_column(String(50), nullable=True, default="unknown")
    compensation_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compensation_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, default=datetime.utcnow
    )

    user: Mapped["User"] = relationship("User", back_populates="job_analyses")
    saved_job: Mapped["SavedJob | None"] = relationship(
        "SavedJob", back_populates="job_analysis", uselist=False
    )


class SavedJob(Base):
    __tablename__ = "saved_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    job_analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("job_analyses.id"),
        nullable=False,
        unique=True,
    )
    saved_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, default=datetime.utcnow
    )

    job_analysis: Mapped["JobAnalysis"] = relationship(
        "JobAnalysis", back_populates="saved_job"
    )
