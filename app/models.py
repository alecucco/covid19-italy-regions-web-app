"""SQLAlchemy ORM models: regions, provinces, and daily case counts."""

import datetime

from sqlalchemy import CheckConstraint, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Region(Base):
    """An Italian region (or autonomous province treated as one).

    `region_code` uses the codes from the official regional dataset.
    The per-province file agrees until 25/06/2020, then switches to a
    single ambiguous code for both Trentino-Alto Adige provinces --
    only the regional codes stay unambiguous for the full history.
    """

    __tablename__ = "regions"

    region_code: Mapped[int] = mapped_column(primary_key=True)
    region_name: Mapped[str] = mapped_column(unique=True)
    nuts_1_code: Mapped[str | None]
    nuts_2_code: Mapped[str | None]

    provinces: Mapped[list["Province"]] = relationship(back_populates="region")


class Province(Base):
    """An Italian province, including the two per-region pseudo-entries
    ('out of region' and 'under definition') published by the source."""

    __tablename__ = "provinces"

    province_code: Mapped[int] = mapped_column(primary_key=True)
    province_name: Mapped[str]
    province_abbreviation: Mapped[str | None]
    region_code: Mapped[int] = mapped_column(ForeignKey("regions.region_code"))
    latitude: Mapped[float | None]
    longitude: Mapped[float | None]
    nuts_3_code: Mapped[str | None]

    region: Mapped["Region"] = relationship(back_populates="provinces")
    cases: Mapped[list["ProvinceCase"]] = relationship(back_populates="province")


class ProvinceCase(Base):
    """Cumulative case count for one province on one date.

    Primary key is (date, province_code), not a surrogate id: it's the
    real natural key, and it makes re-running ingestion idempotent.
    """

    __tablename__ = "province_cases"
    __table_args__ = (
        CheckConstraint("total_cases >= 0", name="ck_total_cases_non_negative"),
    )

    date: Mapped[datetime.date] = mapped_column(primary_key=True)
    province_code: Mapped[int] = mapped_column(
        ForeignKey("provinces.province_code"), primary_key=True
    )
    total_cases: Mapped[int]
    notes: Mapped[str | None]

    province: Mapped["Province"] = relationship(back_populates="cases")