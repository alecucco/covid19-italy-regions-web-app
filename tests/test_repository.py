"""Tests for the aggregation, sorting, and date resolution logic
(app/repository.py) -- directly against the database session, without
going through HTTP."""

from app.repository import (
    SortOrder,
    get_earliest_available_date,
    get_latest_available_date,
    get_regional_totals,
    resolve_target_date,
)
from tests.conftest import TEST_DATE_LATEST, TEST_DATE_OLD


def test_date_range(db_session):
    """Earliest/latest available date match the seeded fixture data."""
    assert get_earliest_available_date(db_session) == TEST_DATE_OLD
    assert get_latest_available_date(db_session) == TEST_DATE_LATEST


def test_aggregation_sums_correctly(db_session):
    """Cases are summed correctly across provinces within a region."""
    results = dict(get_regional_totals(db_session, TEST_DATE_LATEST))
    assert results == {"TestRegionA": 300, "TestRegionB": 100, "TestRegionZ": 200}


def test_sort_cases_desc_default(db_session):
    """Default sort order is total cases, descending."""
    results = get_regional_totals(db_session, TEST_DATE_LATEST)
    names = [name for name, _ in results]
    assert names == ["TestRegionA", "TestRegionZ", "TestRegionB"]


def test_sort_cases_asc(db_session):
    """Sort by total cases, ascending."""
    results = get_regional_totals(db_session, TEST_DATE_LATEST, SortOrder.CASES_ASC)
    names = [name for name, _ in results]
    assert names == ["TestRegionB", "TestRegionZ", "TestRegionA"]


def test_sort_name_asc(db_session):
    """Sort by region name, A-Z."""
    results = get_regional_totals(db_session, TEST_DATE_LATEST, SortOrder.NAME_ASC)
    names = [name for name, _ in results]
    assert names == ["TestRegionA", "TestRegionB", "TestRegionZ"]


def test_sort_name_desc(db_session):
    """Sort by region name, Z-A."""
    results = get_regional_totals(db_session, TEST_DATE_LATEST, SortOrder.NAME_DESC)
    names = [name for name, _ in results]
    assert names == ["TestRegionZ", "TestRegionB", "TestRegionA"]


def test_resolve_valid_date(db_session):
    """A valid date resolves with no error and no fallback."""
    result = resolve_target_date(db_session, TEST_DATE_OLD.isoformat())
    assert result.error is None
    assert result.fallback is False
    assert result.target_date == TEST_DATE_OLD


def test_resolve_date_before_range(db_session):
    """A date before the earliest available falls back to the latest
    day, with an error message."""
    result = resolve_target_date(db_session, "2019-01-01")
    assert result.error is not None
    assert "before" in result.error
    # Still falls back to the latest available day, doesn't leave the page empty.
    assert result.target_date == TEST_DATE_LATEST
    assert len(result.results) == 3


def test_resolve_date_after_range(db_session):
    """A date after the latest available produces an error message."""
    result = resolve_target_date(db_session, "2099-01-01")
    assert result.error is not None
    assert "after" in result.error


def test_resolve_malformed_date(db_session):
    """A malformed date string produces a clear error message."""
    result = resolve_target_date(db_session, "not-a-date")
    assert result.error is not None
    assert "not a valid date" in result.error


def test_resolve_invalid_sort_falls_back_safely(db_session):
    """An unrecognized sort value falls back to the default, with an
    error message."""
    result = resolve_target_date(db_session, TEST_DATE_LATEST.isoformat(), "not_a_real_sort")
    assert result.sort_error is not None
    assert result.sort_order == SortOrder.CASES_DESC
    assert len(result.results) == 3


def test_resolve_sql_injection_attempt_on_sort(db_session):
    """A real injection attempt on the sort parameter must never reach
    SQL: it should just fall back to the default, with no errors."""
    payload = "x; DROP TABLE regions;--"
    result = resolve_target_date(db_session, TEST_DATE_LATEST.isoformat(), payload)
    assert result.sort_order == SortOrder.CASES_DESC
    assert len(result.results) == 3
    # The tables must still be there: a follow-up query must still work.
    assert get_regional_totals(db_session, TEST_DATE_LATEST) != []