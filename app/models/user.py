import uuid
from datetime import datetime
from sqlalchemy import String, Boolean, Integer, Text, TIMESTAMP, Enum as SAEnum, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship
import enum

from app.db.database import Base


class CompFlexibility(str, enum.Enum):
    strict = "strict"
    moderate = "moderate"
    open = "open"


class RemotePreference(str, enum.Enum):
    remote = "remote"
    hybrid = "hybrid"
    onsite = "onsite"
    open = "open"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, default=datetime.utcnow
    )
    # Encrypted Google OAuth token JSON
    google_oauth_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    sheet_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    preferences: Mapped["UserPreferences | None"] = relationship(
        "UserPreferences", back_populates="user", uselist=False
    )
    resume: Mapped["UserResume | None"] = relationship(
        "UserResume", back_populates="user", uselist=False
    )
    job_analyses: Mapped[list["JobAnalysis"]] = relationship(
        "JobAnalysis", back_populates="user"
    )


class UserPreferences(Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        primary_key=True,
    )
    desired_min_comp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    desired_target_comp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comp_flexibility: Mapped[CompFlexibility] = mapped_column(
        SAEnum(CompFlexibility), default=CompFlexibility.moderate
    )
    remote_preference: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=lambda: ["open"]
    )
    preferred_regions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list
    )
    relocate_willing: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped["User"] = relationship("User", back_populates="preferences")
