"""Shared Excel formatting utilities for all report modules."""

from __future__ import annotations

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet



HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
HEADER_FILL = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
BOLD_FONT = Font(bold=True)

# Status colors
RED_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
DARK_RED_FILL = PatternFill(start_color="C00000", end_color="C00000", fill_type="solid")
DARK_RED_FONT = Font(color="FFFFFF", bold=True)
ORANGE_FILL = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
GREEN_FILL = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
LIGHT_GREEN_FILL = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
THIN_BORDER = Border(bottom=Side(style="thin", color="D9D9D9"))



BW_MAX = 800    # Mbps – practical NIC limit
BW_RED = 650    # Mbps – near saturation
BW_ORANGE = 500  # Mbps – high utilization
BW_GREEN = 200   # Mbps – normal


def classify_bandwidth(mbps: float | None) -> str:
    """Classify traffic into a tier string."""
    if mbps is None:
        return ""
    if mbps >= BW_RED:
        return "CRITICAL"
    if mbps >= BW_ORANGE:
        return "HIGH"
    if mbps >= BW_GREEN:
        return "NORMAL"
    return "LOW"


def bandwidth_fill(mbps: float | None) -> tuple[PatternFill | None, Font | None]:
    """Return (fill, font) for a traffic value."""
    if mbps is None:
        return None, None
    if mbps >= BW_MAX:
        return DARK_RED_FILL, DARK_RED_FONT
    if mbps >= BW_RED:
        return RED_FILL, None
    if mbps >= BW_ORANGE:
        return ORANGE_FILL, None
    if mbps >= BW_GREEN:
        return GREEN_FILL, None
    return LIGHT_GREEN_FILL, None


def cpu_fill(pct: float | None) -> PatternFill | None:
    """Return fill for a CPU usage value."""
    if pct is None:
        return None
    if pct >= 80:
        return RED_FILL
    if pct >= 50:
        return ORANGE_FILL
    if pct < 10:
        return GREEN_FILL
    return None



def write_headers(ws: Worksheet, headers: list[str]) -> None:
    """Write styled header row."""
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center")


def auto_width(ws: Worksheet, headers: list[str], sample_rows: int = 50) -> None:
    """Auto-size columns based on header and first N data rows."""
    for col in range(1, len(headers) + 1):
        max_len = len(str(ws.cell(1, col).value or ""))
        for row in range(2, min(ws.max_row + 1, sample_rows + 2)):
            val = ws.cell(row, col).value
            if val is not None:
                max_len = max(max_len, len(str(val)))
        ws.column_dimensions[get_column_letter(col)].width = min(max_len + 3, 45)


def finalize_sheet(ws: Worksheet, headers: list[str], row_count: int) -> None:
    """Apply filters, freeze panes, and auto-width."""
    last_col = get_column_letter(len(headers))
    ws.auto_filter.ref = f"A1:{last_col}{row_count + 1}"
    ws.freeze_panes = "A2"
    auto_width(ws, headers)


def write_data_rows(
    ws: Worksheet,
    rows: list[dict],
    headers: list[str],
) -> None:
    """Write data rows with borders and row numbers."""
    for idx, r in enumerate(rows, 2):
        ws.cell(row=idx, column=1, value=idx - 1)
        for col, key in enumerate(headers[1:], 2):
            cell = ws.cell(row=idx, column=col, value=r.get(key, ""))
            cell.border = THIN_BORDER
