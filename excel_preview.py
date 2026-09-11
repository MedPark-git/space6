"""Render the generated XLSX itself as a browser-viewable SVG image."""
from datetime import date, datetime
from html import escape
from io import BytesIO
import math

from openpyxl import load_workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.utils import get_column_letter

EXCEL_IMAGE_VERSION = '2026-09-11-xlsx-svg-v3'


def _color(color, default):
    if color is None:
        return default
    value = color.rgb if color.type == 'rgb' else None
    if isinstance(value, str) and len(value) in (6, 8):
        return '#' + value[-6:]
    return default


def _display_value(cell):
    value = cell.value
    if value is None:
        return ''
    if isinstance(value, (datetime, date)):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, (int, float)):
        if '"분"' in (cell.number_format or ''):
            return f'{value:g}분'
        return f'{value:g}'
    return str(value)


def _rich_html(cell):
    value = cell.value
    if not isinstance(value, CellRichText):
        return escape(_display_value(cell))
    parts = []
    for run in value:
        if isinstance(run, TextBlock):
            text = escape(run.text)
            if run.font and run.font.b:
                text = '<strong>' + text + '</strong>'
            if run.font and run.font.i:
                text = '<em>' + text + '</em>'
            parts.append(text)
        else:
            parts.append(escape(str(run)))
    return ''.join(parts)


def _column_pixels(width):
    return max(8, math.floor((width or 8.43) * 7 + 5))


def _row_pixels(height):
    return max(2, (height or 15) * 96 / 72)


def _wrap_text(value, width, font_size):
    lines = []
    limit = max(1, width - 10)
    for paragraph in str(value).splitlines() or ['']:
        current, used = '', 0.0
        for char in paragraph:
            char_width = font_size * (1.0 if ord(char) > 255 else 0.56)
            if current and used + char_width > limit:
                lines.append(current); current, used = '', 0.0
            current += char; used += char_width
        lines.append(current)
    return lines or ['']


def render_workbook_svg(content):
    """Translate the first worksheet's actual layout and styles into SVG."""
    book = load_workbook(BytesIO(content), data_only=False, rich_text=True)
    sheet = book.active
    max_row, max_col = sheet.max_row, sheet.max_column
    widths = [_column_pixels(sheet.column_dimensions[get_column_letter(i)].width) for i in range(1, max_col + 1)]
    heights = [_row_pixels(sheet.row_dimensions[i].height) for i in range(1, max_row + 1)]
    xs = [0]
    ys = [0]
    for value in widths:
        xs.append(xs[-1] + value)
    for value in heights:
        ys.append(ys[-1] + value)

    merged = {}
    covered = set()
    for area in sheet.merged_cells.ranges:
        merged[(area.min_row, area.min_col)] = area
        for row in range(area.min_row, area.max_row + 1):
            for col in range(area.min_col, area.max_col + 1):
                if (row, col) != (area.min_row, area.min_col):
                    covered.add((row, col))

    scale = 1.45
    page_width = round(xs[-1] * scale)
    page_height = round(ys[-1] * scale)
    output = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{page_width}" height="{page_height}" viewBox="0 0 {xs[-1]:.2f} {ys[-1]:.2f}">',
        '<rect width="100%" height="100%" fill="#fff"/>',
    ]
    for row in range(1, max_row + 1):
        for col in range(1, max_col + 1):
            if (row, col) in covered:
                continue
            cell = sheet.cell(row, col)
            area = merged.get((row, col))
            end_row = area.max_row if area else row
            end_col = area.max_col if area else col
            x, y = xs[col - 1], ys[row - 1]
            width, height = xs[end_col] - x, ys[end_row] - y
            fill = _color(cell.fill.fgColor, '#ffffff') if cell.fill.fill_type else '#ffffff'
            border = '#b7c9af' if any(getattr(getattr(cell.border, edge), 'style', None) for edge in ('left', 'right', 'top', 'bottom')) else 'none'
            stroke_width = '.7' if border != 'none' else '0'
            output.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{width:.2f}" height="{height:.2f}" fill="{fill}" stroke="{border}" stroke-width="{stroke_width}"/>')
            if cell.value is None:
                continue
            font_size = float(cell.font.sz or 11) * 96 / 72
            font_family = escape(cell.font.name or 'Malgun Gothic')
            color = _color(cell.font.color, '#202820') if cell.font.color and cell.font.color.type == 'rgb' else '#202820'
            weight = '700' if cell.font.b else '400'
            horizontal = cell.alignment.horizontal or 'left'
            vertical = cell.alignment.vertical or 'center'
            justify = {'center': 'center', 'right': 'flex-end'}.get(horizontal, 'flex-start')
            align = {'center': 'center', 'right': 'right'}.get(horizontal, 'left')
            items = {'center': 'center', 'bottom': 'flex-end'}.get(vertical, 'flex-start')
            padding = 3 if width < 80 else 5
            text = _display_value(cell)
            lines = _wrap_text(text, width, font_size)
            line_height = font_size * 1.42
            visible = max(1, int(max(1, height - padding * 2) / line_height))
            lines = lines[:visible]
            block_height = len(lines) * line_height
            start_y = y + padding + font_size
            if vertical == 'center': start_y = y + max(padding + font_size, (height - block_height) / 2 + font_size)
            elif vertical == 'bottom': start_y = y + height - padding - block_height + font_size
            anchor = {'center':'middle','right':'end'}.get(horizontal,'start')
            text_x = {'center':x + width / 2,'right':x + width - padding}.get(horizontal,x + padding)
            output.append(f'<text x="{text_x:.2f}" y="{start_y:.2f}" text-anchor="{anchor}" font-family="{font_family}, Malgun Gothic, sans-serif" font-size="{font_size:.2f}" font-weight="{weight}" fill="{color}">')
            for index, line in enumerate(lines):
                output.append(f'<tspan x="{text_x:.2f}" dy="{0 if index == 0 else line_height:.2f}">{escape(line)}</tspan>')
            output.append('</text>')
    output.append('</svg>')
    return ''.join(output).encode('utf-8')
