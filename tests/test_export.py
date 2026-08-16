"""Tests for the .xls file generation (app/services/export.py) --
verify it's a real, readable Excel file, not just arbitrary bytes."""

import xlrd

from app.services.export import generate_xls


def test_generates_valid_ole2_file():
    """The output starts with the correct OLE2/BIFF file signature."""
    content = generate_xls([("TestRegionA", 300)])
    assert content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def test_headers_and_data_correct():
    """Headers and row data land in the right columns, in order."""
    content = generate_xls([("TestRegionA", 300), ("TestRegionB", 100)])
    workbook = xlrd.open_workbook(file_contents=content)
    sheet = workbook.sheet_by_index(0)

    assert sheet.row_values(0) == ["Region", "Total cases"]
    assert sheet.row_values(1) == ["TestRegionA", 300]
    assert sheet.row_values(2) == ["TestRegionB", 100]
    assert sheet.nrows == 3


def test_empty_data_produces_header_only():
    """No rows in, still a valid file with just the header row."""
    content = generate_xls([])
    workbook = xlrd.open_workbook(file_contents=content)
    sheet = workbook.sheet_by_index(0)
    assert sheet.nrows == 1