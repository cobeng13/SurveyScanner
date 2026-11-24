from collections import Counter
import csv
from io import BytesIO, StringIO
from statistics import mean
from zipfile import ZipFile

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .config import RATINGS
from .csvutil import spreadsheet_value
from .database import elapsed

METADATA = {
    "program": "Program", "course_code": "Course code", "title": "Course title",
    "year_level": "Year level", "section": "Section", "semester": "Semester",
    "academic_year": "Academic year", "faculty": "Faculty name",
}
METHOD = ("Item percentages divide each answer count by valid responses to that item. "
          "Section means pool all valid fixed-item ratings in the section. The overall mean pools "
          "all valid ratings across the 33 fixed items. Each valid answer has equal weight. "
          "Missing ratings are omitted from means. CILO percentages use valid responses to that CILO.")
SWATCH = "174D77"


def effective(page, key, bundle):
    value = page["corrections"].get(key, page["machine"].get(key))
    return value if value in bundle.choices(key) else None


def included(page):
    return page["status"] != "Failed" and page["review"] != "Excluded"


def report(batch, pages, bundle):
    records = [page for page in pages if included(page)]
    active = bundle.active_fields(batch["metadata"]["cilos"])
    items = []
    section_ratings = {}
    for key in active:
        field = bundle.fields[key]
        choices = bundle.choices(key)
        is_rating = key.startswith("Q")
        values = [effective(page, key, bundle) for page in records]
        counts = Counter(value for value in values if value is not None)
        valid = counts.total()
        ratings = [int(value) for value in values if value is not None] if is_rating else []
        if is_rating:
            section_ratings.setdefault(field["section"], []).extend(ratings)
        items.append({
            "key": key, "statement": field["statement"] or batch["metadata"]["statements"].get(key),
            "valid": valid, "missing": len(records) - valid, "mean": mean(ratings) if ratings else None,
            "frequencies": [(RATINGS.get(value, value), counts[value], counts[value] / valid if valid else None)
                            for value in choices],
        })
    pooled = [value for ratings in section_ratings.values() for value in ratings]
    pending_fields = [
        {key for key in active if key in page["warnings"] and key not in page["corrections"]}
        for page in records
    ]
    unresolved = sum(map(bool, pending_fields))
    missing = sum(item["missing"] for item in items)
    failed = sum(page["status"] == "Failed" for page in pages)
    unreviewed = sum(page["review"] == "Unreviewed" for page in records)
    return {
        "items": items, "sections": [(key, mean(values) if values else None)
                                      for key, values in section_ratings.items()],
        "overall": mean(pooled) if pooled else None,
        "unreviewed": unreviewed, "unresolved": unresolved,
        "provisional": (batch["status"] not in ("Completed", "Completed with errors")
                        or bool(unreviewed or unresolved)),
        "recognized": len(pages) - failed, "included": len(records), "failed": failed,
        "reviewed": sum(page["review"] == "Reviewed" for page in pages),
        "corrected": sum(bool(page["corrections"]) for page in pages),
        "excluded": len(pages) - len(records),
        "blank": missing - sum(map(len, pending_fields)),
        "ambiguous_responses": sum(sum(key in page["warnings"] for key in active) for page in records),
        "ambiguous_pages": sum(page["status"] == "Multiple marks" for page in pages),
        "duration": elapsed(batch),
    }


def response_rows(batch, pages, bundle, summary):
    yield ["Batch ID", "Respondent ID", "Source PDF filename", "Source PDF page number",
           *METADATA.values(), *bundle.fields, "Processing status", "Review status",
           "Manually corrected", "Includes unreviewed questionnaires"]
    active = set(bundle.active_fields(batch["metadata"]["cilos"]))
    for page in pages:
        if included(page):
            values = [effective(page, key, bundle) if key in active else None for key in bundle.fields]
            values = [int(value) if key.startswith("Q") and value is not None else value
                      for key, value in zip(bundle.fields, values)]
            yield [batch["id"], page["id"], batch["filename"], page["page"],
                   *(batch["metadata"][key] for key in METADATA), *values,
                   page["status"], page["review"], "Yes" if page["corrections"] else "No",
                   "Yes" if summary["unreviewed"] else "No"]


def csv_export(batch, pages, bundle, summary):
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    for row in response_rows(batch, pages, bundle, summary):
        writer.writerow([spreadsheet_value(value) for value in row])
    return stream.getvalue().encode("utf-8-sig")


def add_sheet(book, name, rows):
    sheet = book.create_sheet(name)
    for row in rows:
        sheet.append(row)
        for cell in sheet[sheet.max_row]:
            if cell.data_type == "f":
                cell.data_type = "s"
    sheet.freeze_panes = "A2"
    for cell in sheet[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = PatternFill("solid", fgColor=SWATCH)
    for col in sheet.columns:
        width = min(60, max(13, max(len(str(cell.value or "")) for cell in col) + 2))
        sheet.column_dimensions[get_column_letter(col[0].column)].width = width
        if width == 60:
            for cell in col:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
    return sheet


def xlsx_export(batch, pages, edits, bundle, summary):
    book = Workbook()
    book.remove(book.active)
    responses = add_sheet(book, "Responses", response_rows(batch, pages, bundle, summary))
    responses.auto_filter.ref = responses.dimensions
    active = set(bundle.active_fields(batch["metadata"]["cilos"]))
    mapping = [
        ["ID", "Section", "Official statement", "Entered CILO statement", "Response scale", "Applicable"],
        *([key, field["section"], field["statement"], batch["metadata"]["statements"].get(key),
           " | ".join(f"{value} — {RATINGS[value]}" if value in RATINGS else value
                      for value in bundle.choices(key)), "Yes" if key in active else "No"]
          for key, field in bundle.fields.items()),
    ]
    add_sheet(book, "Question Mapping", mapping)
    aggregate = [["Group / ID", "Statement", "Response", "Count", "Valid", "Missing", "Percent", "Mean"]]
    aggregate.extend([label, None, None, None, None, None, None, score]
                     for label, score in [("Overall", summary["overall"]), *summary["sections"]])
    for item in summary["items"]:
        for choice, count, ratio in item["frequencies"]:
            aggregate.append([item["key"], item["statement"], choice, count,
                              item["valid"], item["missing"], ratio, item["mean"]])
    sheet = add_sheet(book, "Summary", aggregate)
    for cell in sheet["G"][1:]:
        cell.number_format = "0.0%"
    for cell in sheet["H"][1:]:
        cell.number_format = "0.00"
    info = [
        ["Property", "Value"], ["Batch ID", batch["id"]], ["Template", batch["template"]],
        ["Engine revision", bundle.manifest["engine_revision"]],
        *([label, batch["metadata"][key]] for key, label in METADATA.items()),
        ["Applicable CILOs", batch["metadata"]["cilos"]], ["Source PDF", batch["filename"]],
        ["Source bytes", batch["size"]], ["Source pages", batch["total"]],
        ["Status", batch["status"]], ["Submitted UTC", batch["created"]],
        ["Started UTC", batch["started"]], ["Finished UTC", batch["finished"]],
        ["Processing duration in seconds", summary["duration"]],
        ["Summary status", "Provisional" if summary["provisional"] else "Finalized"],
        ["Includes unreviewed questionnaires", "Yes" if summary["unreviewed"] else "No"],
        ["Unreviewed questionnaires included", summary["unreviewed"]],
        ["Failed or excluded pages", summary["excluded"]], ["Aggregation method", METHOD],
    ]
    add_sheet(book, "Batch Information", info)
    page_index = {page["page"]: page for page in pages}
    log = [["Page", "Respondent ID", "Question", "Original machine value", "Corrected value", "UTC time", "Reviewer"]]
    log.extend([edit["page"], page_index[edit["page"]]["id"], edit["field"],
                page_index[edit["page"]]["machine"].get(edit["field"]), edit["value"],
                edit["changed_at"], edit["reviewer"]] for edit in edits)
    add_sheet(book, "Review Log", log)
    exceptions = [["Page", "Respondent ID", "Processing status", "Review status", "Question", "Warning"]]
    for page in pages:
        for key, warning in page["warnings"].items():
            if key == "_page" or (key in active and key not in page["corrections"]):
                exceptions.append([page["page"], page["id"], page["status"], page["review"],
                                   None if key == "_page" else key, warning])
        if page["review"] == "Excluded" and not page["warnings"]:
            exceptions.append([page["page"], page["id"], page["status"], page["review"], None,
                               "Excluded during review."])
    if batch["message"]:
        exceptions.append([None, None, batch["status"], None, None, batch["message"]])
    add_sheet(book, "Processing Exceptions", exceptions)
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def archive_native(files, base):
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for path in files:
            archive.write(path, path.relative_to(base).as_posix())
    return output.getvalue()
