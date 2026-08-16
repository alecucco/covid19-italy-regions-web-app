"""End-to-end tests for the HTTP routes (app/main.py), through the
FastAPI TestClient -- the same checks we made by hand dozens of times
during development, now fixed as a repeatable suite."""

import re

import xlrd

from tests.conftest import TEST_DATE_LATEST, TEST_DATE_OLD


def _table_rows(html: str):
    """Extract the (region, cases) pairs from the results table's HTML."""
    return re.findall(r"<tr>\s*<td>([^<]+)</td>\s*<td>([^<]+)</td>\s*</tr>", html)


def test_home_default_falls_back_to_latest(client):
    """Without a 'date' parameter, real today has no fake data -> falls
    back to the latest available day and says so explicitly."""
    response = client.get("/")
    assert response.status_code == 200
    assert "No more recent data available" in response.text
    assert len(_table_rows(response.text)) == 3


def test_home_valid_date_search(client):
    """A valid, explicit date search returns that date's results."""
    response = client.get(f"/?date={TEST_DATE_OLD.isoformat()}")
    assert response.status_code == 200
    assert "No more recent data available" not in response.text
    rows = _table_rows(response.text)
    assert len(rows) == 3


def test_home_date_out_of_range_shows_error(client):
    """A date before the earliest available shows a clear error message."""
    response = client.get("/?date=2019-01-01")
    assert response.status_code == 200
    assert "No data available before" in response.text


def test_home_malformed_date_does_not_crash(client):
    """A malformed date string doesn't break the page."""
    response = client.get("/?date=not-a-date")
    assert response.status_code == 200
    assert len(_table_rows(response.text)) == 3


def test_home_sort_by_name(client):
    """The sort parameter actually changes the row order."""
    response = client.get(f"/?date={TEST_DATE_LATEST.isoformat()}&sort=name_asc")
    rows = _table_rows(response.text)
    names = [n for n, _ in rows]
    assert names == ["TestRegionA", "TestRegionB", "TestRegionZ"]


def test_home_invalid_sort_shows_error_and_safe_default(client):
    """An unrecognized sort value shows an error and still renders the page."""
    response = client.get(f"/?date={TEST_DATE_LATEST.isoformat()}&sort=ciao")
    assert response.status_code == 200
    assert "not a valid sort order" in response.text


def test_home_xss_payload_never_reflected_unescaped(client):
    """Security check: a script payload must only ever appear escaped
    by autoescaping, never as executable markup."""
    payload = "<script>alert(1)</script>"
    response = client.get(f"/?date={TEST_DATE_LATEST.isoformat()}&sort={payload}")
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_home_sql_injection_attempt_does_not_break_app(client):
    """Security check: an injection attempt on 'sort' doesn't break the
    app or the database for subsequent requests."""
    payload = "x; DROP TABLE regions;--"
    response = client.get(f"/?date={TEST_DATE_LATEST.isoformat()}&sort={payload}")
    assert response.status_code == 200
    # The tables must still be there: a follow-up request must still work.
    follow_up = client.get(f"/?date={TEST_DATE_LATEST.isoformat()}")
    assert len(_table_rows(follow_up.text)) == 3


def test_export_returns_valid_xls(client):
    """The export route returns a real, correctly-labeled .xls download."""
    response = client.get(f"/export?date={TEST_DATE_LATEST.isoformat()}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.ms-excel"
    assert "attachment" in response.headers["content-disposition"]

    workbook = xlrd.open_workbook(file_contents=response.content)
    sheet = workbook.sheet_by_index(0)
    assert sheet.nrows == 4  # header + 3 regions


def test_export_matches_page_for_same_parameters(client):
    """The consistency we used to verify by hand: the page and the
    export, given the same parameters, must show exactly the same data."""
    page = client.get(f"/?date={TEST_DATE_LATEST.isoformat()}&sort=name_asc")
    export = client.get(f"/export?date={TEST_DATE_LATEST.isoformat()}&sort=name_asc")

    page_rows = _table_rows(page.text)
    workbook = xlrd.open_workbook(file_contents=export.content)
    sheet = workbook.sheet_by_index(0)
    export_rows = [(sheet.cell_value(i, 0), int(sheet.cell_value(i, 1))) for i in range(1, sheet.nrows)]

    page_rows_parsed = [(name, int(cases.replace(",", ""))) for name, cases in page_rows]
    assert page_rows_parsed == export_rows