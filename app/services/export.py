"""Generate the .xls export (Task #3): two columns, Region and
Total cases, in a single sheet."""

import io

import xlwt


def generate_xls(rows: list[tuple[str, int]]) -> bytes:
    """Build an .xls file with a header row and one row per region.

    Args:
        rows (list[tuple[str, int]]): (region_name, total_cases) pairs,
            already sorted -- written to the sheet in this order.

    Returns:
        bytes: The raw file content, ready to send as an HTTP response.
    """
    workbook = xlwt.Workbook(encoding="utf-8")
    sheet = workbook.add_sheet("Total cases by region")

    header_style = xlwt.easyxf("font: bold on")
    sheet.write(0, 0, "Region", header_style)
    sheet.write(0, 1, "Total cases", header_style)

    for row_index, (region_name, total_cases) in enumerate(rows, start=1):
        sheet.write(row_index, 0, region_name)
        sheet.write(row_index, 1, total_cases)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()