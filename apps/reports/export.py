"""CSV and PDF rendering of report dictionaries."""
import csv
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _cell(v):
    """Neutralize spreadsheet formula injection in exported text."""
    s = "" if v is None else str(v)
    return "'" + s if s.startswith(FORMULA_PREFIXES) and not s.lstrip("-").replace(".", "", 1).isdigit() else s


def to_csv(report):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([report["title"]])
    w.writerow(["Filters", report["filters"]])
    w.writerow(["Generated", report["generated_at"]])
    for n in report["notes"]:
        w.writerow(["Note", n])
    for sec in report["sections"]:
        w.writerow([])
        w.writerow([sec["title"]])
        if sec["kind"] == "metrics":
            for label, value in sec["metrics"]:
                w.writerow([_cell(label), _cell(value)])
        else:
            w.writerow([_cell(c) for c in sec["columns"]])
            for row in sec["rows"]:
                w.writerow([_cell(c) for c in row])
        if sec.get("note"):
            w.writerow(["Note", sec["note"]])
    return buf.getvalue().encode("utf-8-sig")


def _p(text, style):
    from xml.sax.saxutils import escape

    return Paragraph(escape(str(text if text is not None else "")), style)


def to_pdf(report):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm,
                            bottomMargin=12 * mm, title=report["title"])
    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=7.5, leading=9)
    story = [_p(report["title"], styles["Title"]), _p(f"{report['filters']} · generated {report['generated_at']}", styles["Normal"])]
    for n in report["notes"]:
        story.append(_p(n, small))
    width = landscape(A4)[0] - 24 * mm
    for sec in report["sections"]:
        story += [Spacer(1, 5 * mm), _p(sec["title"], styles["Heading2"])]
        if sec["kind"] == "metrics":
            data = [[_p(k, small), _p(v, small)] for k, v in sec["metrics"]] or [[_p("No data", small), ""]]
            col_widths = [width * 0.35, width * 0.25]
        else:
            data = [[_p(c, small) for c in sec["columns"]]] + [[_p(c, small) for c in row] for row in sec["rows"][:500]]
            if len(data) == 1:
                data.append([_p("No data", small)] + [""] * (len(sec["columns"]) - 1))
            col_widths = [width / len(sec["columns"])] * len(sec["columns"])
        t = Table(data, colWidths=col_widths, repeatRows=0 if sec["kind"] == "metrics" else 1)
        style = [("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")), ("VALIGN", (0, 0), (-1, -1), "TOP")]
        if sec["kind"] == "table":
            style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eefc")))
        t.setStyle(TableStyle(style))
        story.append(t)
        if sec.get("note"):
            story.append(_p(sec["note"], small))
    doc.build(story)
    return buf.getvalue()
