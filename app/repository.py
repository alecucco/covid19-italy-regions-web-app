"""Data access layer: the aggregate queries behind the main page
(Task #1/#2), sorting (Task #4), and the export (Task #3)."""

import datetime
import enum
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Province, ProvinceCase, Region

MONTHS_EN = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def format_date(d: datetime.date) -> str:
    """Format a date in English (e.g. 'January 8, 2025').

    Written by hand instead of relying on `strftime` with a system
    locale, which isn't always configured for English on Windows.

    Args:
        d (datetime.date): The date to format.

    Returns:
        str: The date as "Month D, YYYY".
    """
    return f"{MONTHS_EN[d.month - 1]} {d.day}, {d.year}"


def get_earliest_available_date(session: Session) -> datetime.date | None:
    """Return the oldest date present in the database.

    Args:
        session (Session): Active database session.

    Returns:
        datetime.date | None: The earliest date, or None if the
            database is empty.
    """
    return session.execute(select(func.min(ProvinceCase.date))).scalar()


def get_latest_available_date(session: Session) -> datetime.date | None:
    """Return the most recent date present in the database.

    Args:
        session (Session): Active database session.

    Returns:
        datetime.date | None: The latest date, or None if the
            database is empty.
    """
    return session.execute(select(func.max(ProvinceCase.date))).scalar()


class SortOrder(str, enum.Enum):
    """Closed whitelist of allowed sort criteria (Task #4).

    An Enum, not a free-form string: a value not listed here can never
    reach a SQL query -- this is the injection defense in practice.
    """

    CASES_DESC = "cases_desc"
    CASES_ASC = "cases_asc"
    NAME_ASC = "name_asc"
    NAME_DESC = "name_desc"


def parse_sort_order(value: str | None) -> SortOrder:
    """Convert a raw URL parameter into a valid SortOrder.

    Any unrecognized value (missing, malformed, or a deliberate
    injection attempt) silently falls back to the default.

    Args:
        value (str | None): The raw `sort` query parameter.

    Returns:
        SortOrder: The matching SortOrder, or SortOrder.CASES_DESC
            if `value` isn't one of the allowed values.
    """
    try:
        return SortOrder(value)
    except ValueError:
        return SortOrder.CASES_DESC


def get_regional_totals(
    session: Session,
    target_date: datetime.date,
    sort_order: SortOrder = SortOrder.CASES_DESC,
) -> list[tuple[str, int]]:
    """Aggregate total cases by region for a given date.

    Args:
        session (Session): Active database session.
        target_date (datetime.date): The date to aggregate cases for.
        sort_order (SortOrder): How to sort the results. Defaults to
            the Task #1 requirement (cases descending, region name
            ascending as tiebreaker).

    Returns:
        list[tuple[str, int]]: (region_name, total_cases) tuples,
            already sorted.
    """
    total_expr = func.sum(ProvinceCase.total_cases)

    match sort_order:
        case SortOrder.CASES_ASC:
            order_by = (total_expr.asc(), Region.region_name.asc())
        case SortOrder.NAME_ASC:
            order_by = (Region.region_name.asc(),)
        case SortOrder.NAME_DESC:
            order_by = (Region.region_name.desc(),)
        case _:  # SortOrder.CASES_DESC, the default
            order_by = (total_expr.desc(), Region.region_name.asc())

    statement = (
        select(Region.region_name, total_expr.label("total"))
        .join(Province, Province.region_code == Region.region_code)
        .join(ProvinceCase, ProvinceCase.province_code == Province.province_code)
        .where(ProvinceCase.date == target_date)
        .group_by(Region.region_code, Region.region_name)
        .order_by(*order_by)
    )
    return [(row.region_name, row.total) for row in session.execute(statement)]


@dataclass
class DateResolution:
    """Outcome of resolving a date/sort request: what to show, any
    errors, whether a fallback was used, and which sort order applies.

    Attributes:
        target_date (datetime.date | None): The resolved date to show.
        results (list[tuple[str, int]]): (region_name, total_cases)
            tuples for `target_date`, sorted by `sort_order`.
        error (str | None): Message if `date_param` was invalid.
        sort_error (str | None): Message if `sort_param` was invalid.
        fallback (bool): True if `target_date` had no data and the
            latest available date was used instead.
        earliest_date (datetime.date | None): Oldest date in the database.
        latest_date (datetime.date | None): Most recent date in the database.
        sort_order (SortOrder): The resolved sort order.
    """

    target_date: datetime.date | None
    results: list[tuple[str, int]]
    error: str | None
    sort_error: str | None
    fallback: bool
    earliest_date: datetime.date | None
    latest_date: datetime.date | None
    sort_order: SortOrder


def resolve_target_date(
    session: Session, date_param: str | None, sort_param: str | None = None
) -> DateResolution:
    """Resolve which date and sort order to show from the user's search
    parameters (Task #2/#4), with validation and fallback.

    Shared between the main page and the export route so both behave
    identically given the same input.

    Args:
        session (Session): Active database session.
        date_param (str | None): Raw `date` query parameter, or None
            if not searched.
        sort_param (str | None): Raw `sort` query parameter, or None
            if not chosen.

    Returns:
        DateResolution: The resolved date, sort order, matching
            results, and any error messages to display.
    """
    earliest_date = get_earliest_available_date(session)
    latest_date = get_latest_available_date(session)

    sort_error = None
    if sort_param and sort_param not in {o.value for o in SortOrder}:
        sort_error = f'"{sort_param}" is not a valid sort order: default applied.'
    sort_order = parse_sort_order(sort_param)

    error = None
    fallback = False

    if date_param:
        try:
            target_date = datetime.date.fromisoformat(date_param)
        except ValueError:
            error = f'"{date_param}" is not a valid date.'
            target_date = latest_date
        else:
            if earliest_date and target_date < earliest_date:
                error = f"No data available before {format_date(earliest_date)}."
                target_date = latest_date
            elif latest_date and target_date > latest_date:
                error = f"No data available after {format_date(latest_date)}."
                target_date = latest_date
    else:
        # No search: Task #1 default behavior (today, with fallback if
        # "today" doesn't have published data yet). If today is already
        # beyond the latest published date, skip straight to it instead
        # of running a query we already know will come back empty.
        target_date = datetime.date.today()
        if latest_date is not None and target_date > latest_date:
            fallback = True
            target_date = latest_date

    results = (
        get_regional_totals(session, target_date, sort_order) if target_date else []
    )

    if not results and not error:
        # If the resolved date falls within the available range (earliest_date
        # to latest_date) but has no data of its own -- a gap in the series --
        # show the most recent available day instead, and state this explicitly.
        fallback = True
        if latest_date is not None:
            target_date = latest_date
            results = get_regional_totals(session, target_date, sort_order)

    return DateResolution(
        target_date=target_date,
        results=results,
        error=error,
        sort_error=sort_error,
        fallback=fallback,
        earliest_date=earliest_date,
        latest_date=latest_date,
        sort_order=sort_order,
    )