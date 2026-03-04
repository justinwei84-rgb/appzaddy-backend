import uuid
import enum
from sqlalchemy import Integer, Text, Enum as SAEnum, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class SeniorityLevel(str, enum.Enum):
    intern = "intern"
    junior = "junior"
    mid = "mid"
    senior = "senior"
    staff = "staff"
    principal = "principal"
    director = "director"
    vp = "vp"
    c_level = "c_level"


class UserResume(Base):
    __tablename__ = "user_resumes"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    structured_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    seniority_level: Mapped[SeniorityLevel] = mapped_column(
        SAEnum(SeniorityLevel), nullable=False
    )
    industries: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    years_experience: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped["User"] = relationship("User", back_populates="resume")
