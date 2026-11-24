from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .config import Bundle, PROGRAMS, RATINGS, SEMESTERS, STATUSES, Settings
from .database import Database, batch_dir, elapsed, now
from .reporting import METADATA, METHOD, archive_native, csv_export, effective, report, xlsx_export
from .security import Access, SecurityMiddleware
from .uploads import run_pdf, stage_pdf
from .worker import attempt_dir, live_counts

WEB = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(WEB / "templates"))
LOG = logging.getLogger("survey.web")
ACTIVE = ("Queued", "Processing")
REVIEW_FILTERS = ("Unreviewed", "Reviewed", "Excluded", "Corrected")
INVALID_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def create_app(settings=None):
    settings = settings or Settings()
    settings.check_web()
    access = Access(settings)

    @asynccontextmanager
    async def lifespan(app):
        app.state.db = Database(settings.data)
        yield

    app = FastAPI(title="Course Feedback Survey Processor", lifespan=lifespan, root_path=settings.root_path,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SecurityMiddleware, access=access, settings=settings)
    app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")

    def view(request, template, *, status_code=200, **context):
        return TEMPLATES.TemplateResponse(request, template, {
            "path": settings.root_path, "csrf": access.issue(request.state.owner),
            "programs": PROGRAMS, "semesters": SEMESTERS, "statuses": STATUSES,
            "reviews": REVIEW_FILTERS, "ratings": RATINGS, **context,
        }, status_code=status_code)

    def redirect(path):
        return RedirectResponse(settings.root_path + path, status_code=303)

    def check_origin(request):
        expected = settings.public_origin or str(request.base_url).rstrip("/")
        if not settings.public_origin and settings.root_path:
            expected = expected.removesuffix(settings.root_path)
        origin = request.headers.get("origin")
        if not origin:
            referrer = urlsplit(request.headers.get("referer", ""))
            origin = f"{referrer.scheme}://{referrer.netloc}"
        if origin != expected or request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "This action must be submitted from the survey application.")

    def check_csrf(request, token):
        if not access.check(request.state.owner, token):
            raise HTTPException(403, "This form expired. Reload the page and try again.")

    def record(request, conn, batch_id):
        batch = request.app.state.db.batch(conn, request.state.owner, batch_id)
        if batch is None:
            raise HTTPException(404, "Survey batch not found.")
        return batch

    def snapshot(request, batch_id):
        db = request.app.state.db
        with db.connect() as conn:
            conn.execute("BEGIN")
            batch = record(request, conn, batch_id)
            pages, edits = db.pages(conn, batch_id)
        bundle = settings.bundle if batch["template"] == settings.bundle.version else Bundle(batch["template"])
        return batch, pages, edits, bundle

    def new_form(request, status_code=200, **context):
        return view(request, "new.html", status_code=status_code, bundle=settings.bundle,
                    limits=settings, **context)

    @app.exception_handler(HTTPException)
    def http_error(request, exc):
        if request.url.path.endswith("/uploads/inspect") or "/status/" in request.url.path:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return view(request, "error.html", status_code=exc.status_code, message=exc.detail)

    @app.get("/health")
    def health(request: Request):
        with request.app.state.db.connect() as conn:
            conn.execute("SELECT 1 FROM batches LIMIT 1")
        return {"status": "ok", "template": settings.bundle.version,
                "omrchecker": settings.bundle.manifest["engine_revision"]}

    @app.get("/")
    def home(request: Request):
        return new_form(request)

    @app.get("/help")
    def help_page(request: Request):
        return view(request, "help.html", limits=settings, method=METHOD)

    @app.get("/questionnaire.pdf")
    def questionnaire():
        return FileResponse(settings.bundle.path / "questionnaire.pdf", media_type="application/pdf",
                            filename="LPU-Course-Feedback-Survey.pdf")

    @app.post("/uploads/inspect")
    async def inspect_upload(request: Request):
        check_origin(request)
        check_csrf(request, request.headers.get("x-csrf-token"))
        async with request.form(max_files=1, max_fields=1) as form:
            with TemporaryDirectory(prefix="survey-upload-") as directory:
                filename, size, count = await run_in_threadpool(
                    stage_pdf, form.get("scan"), Path(directory) / "scan.pdf", settings,
                )
        return {"filename": filename, "size": size, "pages": count}

    @app.post("/batches")
    async def create_batch(request: Request):
        check_origin(request)
        async with request.form(max_files=1, max_fields=24, max_part_size=8192) as form:
            check_csrf(request, form.get("csrf"))
            try:
                metadata = read_metadata(form, settings.bundle)
                batch_id = await run_in_threadpool(
                    submit_batch, request.app.state.db, settings, request.state.owner, metadata, form.get("scan"),
                )
            except HTTPException as exc:
                return new_form(request, status_code=exc.status_code, error=exc.detail, values=form)
        return redirect(f"/batches/{batch_id}")

    @app.get("/history")
    def history(request: Request):
        query = request.query_params
        filters = {key: query.get(key, "")[:160] for key in
                   ("program", "course", "academic_year", "semester", "status", "review")}
        clauses = ["b.owner=?", "b.deleted=0"]
        args = [request.state.owner]
        for key in ("program", "academic_year", "semester", "status"):
            if filters[key]:
                expr = "b.status" if key == "status" else f"json_extract(b.metadata,'$.{key}')"
                clauses.append(f"{expr}=? COLLATE NOCASE")
                args.append(filters[key])
        if filters["course"]:
            clauses.append("""(instr(lower(json_extract(b.metadata,'$.title')), lower(?)) > 0
                               OR instr(lower(json_extract(b.metadata,'$.course_code')), lower(?)) > 0)""")
            args.extend([filters["course"]] * 2)
        if filters["review"] in REVIEW_FILTERS:
            if filters["review"] == "Corrected":
                clauses.append("EXISTS(SELECT 1 FROM changes c WHERE c.batch_id=b.id)")
            else:
                clauses.append("EXISTS(SELECT 1 FROM pages p WHERE p.batch_id=b.id AND p.review=?)")
                args.append(filters["review"])
        where = " AND ".join(clauses)
        with request.app.state.db.connect() as conn:
            batches = [dict(row, metadata=json.loads(row["metadata"])) for row in conn.execute(
                f"""SELECT b.*,
                  (SELECT count(*) FROM pages p WHERE p.batch_id=b.id AND p.review='Unreviewed') AS pending,
                  (SELECT count(*) FROM pages p WHERE p.batch_id=b.id AND p.review='Reviewed') AS reviewed
                  FROM batches b WHERE {where} ORDER BY b.created DESC, b.id DESC""", args)]
        return view(request, "history.html", batches=batches, filters=filters, active=ACTIVE)

    @app.get("/batches/{batch_id}")
    def batch_result(request: Request, batch_id: str):
        batch, pages, _, bundle = snapshot(request, batch_id)
        return view(request, "batch.html", batch=batch, pages=pages, summary=report(batch, pages, bundle),
                    method=METHOD, active=ACTIVE, metadata_labels=METADATA, latest_has_csv=bool(
                        native_files(settings, batch, "Results", "Results_*.csv")))

    @app.get("/status/{batch_id}")
    def job_status(request: Request, batch_id: str):
        batch, pages, _, _ = snapshot(request, batch_id)
        if batch["status"] == "Processing":
            processed, successful, failed = live_counts(settings.data, batch) or (None, None, None)
        elif batch["status"] in ("Queued", "Interrupted") or not pages:
            processed = successful = failed = None
        else:
            processed = len(pages)
            failed = sum(page["status"] == "Failed" for page in pages)
            successful = processed - failed
        return {"status": batch["status"], "total": batch["total"],
                "processed": processed, "successful": successful, "failed": failed,
                "elapsed_seconds": elapsed(batch), "message": batch["message"]}

    @app.get("/batches/{batch_id}/review")
    def review_page(request: Request, batch_id: str, page: int | None = None, pending: bool = False):
        batch, pages, _, bundle = snapshot(request, batch_id)
        filtered = [row for row in pages if not pending or row["review"] == "Unreviewed"]
        if page is not None:
            current = next((row for row in filtered if row["page"] == page), None)
            if current is None:
                raise HTTPException(404, "Questionnaire page not found in this view.")
        else:
            current = filtered[0] if filtered else None
        if current:
            index = filtered.index(current)
            previous = filtered[index - 1]["page"] if index else None
            following = filtered[index + 1]["page"] if index + 1 < len(filtered) else None
        else:
            previous = following = None
        fields = bundle.active_fields(batch["metadata"]["cilos"])
        return view(request, "review.html", batch=batch, p=current, fields=fields, bundle=bundle,
                    effective=effective, previous=previous, following=following, pending=pending,
                    available=filtered)

    @app.post("/batches/{batch_id}/review/{page}")
    async def save_review(request: Request, batch_id: str, page: int):
        check_origin(request)
        async with request.form(max_files=0, max_fields=48, max_part_size=8192) as form:
            check_csrf(request, form.get("csrf"))
            next_page = await run_in_threadpool(
                update_review, request, batch_id, page, form, settings, record,
            )
        return redirect(next_page)

    @app.get("/batches/{batch_id}/file/{kind}")
    def batch_file(request: Request, batch_id: str, kind: str, page: int | None = None):
        batch, pages, edits, bundle = snapshot(request, batch_id)
        folder = batch_dir(settings.data, batch_id)
        original = folder / "original" / "scan.pdf"
        if kind == "original":
            return download(original, "application/pdf", batch["filename"])
        if kind == "scan":
            if page is None or not 1 <= page <= batch["total"]:
                raise HTTPException(404, "Scan page not found.")
            return Response(run_pdf("preview", original, page), media_type="image/png")
        if kind == "overlay":
            relative = next((row["overlay"] for row in pages if row["page"] == page), None)
            if relative:
                path = (folder / relative).resolve()
                if path.is_relative_to(folder):
                    return download(path, "image/png")
        if kind == "machine" and batch["status"] not in ACTIVE:
            files = native_files(settings, batch, "Results", "Results_*.csv")
            if len(files) == 1:
                return download(files[0], "text/csv", f"{batch_id}-machine.csv")
            if files:
                return attachment(archive_native(files, folder), "application/zip", f"{batch_id}-machine.zip")
        if kind == "audit" and batch["status"] not in ACTIVE:
            files = sorted(folder.glob("attempt-*/output/*/*.csv"))
            if files:
                return attachment(archive_native(files, folder), "application/zip", f"{batch_id}-machine-audit.zip")
        if kind in {"csv", "xlsx"} and batch["status"] not in ACTIVE:
            summary = report(batch, pages, bundle)
            if kind == "csv":
                data = csv_export(batch, pages, bundle, summary)
                media = "text/csv"
            else:
                data = xlsx_export(batch, pages, edits, bundle, summary)
                media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            suffix = "provisional" if summary["provisional"] else "finalized"
            filename = f"{batch_id}-{suffix}.{kind}"
            persist_export(folder, batch, pages, filename, data)
            return attachment(data, media, filename)
        raise HTTPException(404, "This file is not available.")

    @app.post("/batches/{batch_id}/retry")
    async def retry(request: Request, batch_id: str):
        await batch_action(request, batch_id, "retry")
        return redirect(f"/batches/{batch_id}")

    @app.post("/batches/{batch_id}/delete")
    async def delete(request: Request, batch_id: str):
        await batch_action(request, batch_id, "delete")
        return redirect("/history")

    async def batch_action(request, batch_id, action):
        check_origin(request)
        async with request.form(max_files=0, max_fields=2) as form:
            check_csrf(request, form.get("csrf"))
            if action == "delete" and form.get("confirm") != "yes":
                raise HTTPException(422, "Confirm permanent deletion.")
            await run_in_threadpool(change_batch, request, batch_id, action, record)

    return app


def read_metadata(form, bundle):
    metadata = {}
    for key, label in METADATA.items():
        value = str(form.get(key, "")).strip()
        if len(value) > 160 or INVALID_CONTROL.search(value) or any(c in value for c in "\r\n\t"):
            raise HTTPException(422, f"Enter a valid {label.lower()} of up to 160 characters.")
        if key in ("program", "title", "semester", "academic_year") and not value:
            raise HTTPException(422, f"{label} is required.")
        metadata[key] = value
    if metadata["program"] not in PROGRAMS or metadata["semester"] not in SEMESTERS:
        raise HTTPException(422, "Choose a listed program and semester.")
    count = str(form.get("cilos", ""))
    if not count.isascii() or not count.isdigit() or len(count) > 2 or int(count) > len(bundle.cilos):
        raise HTTPException(422, f"Choose between 0 and {len(bundle.cilos)} applicable CILOs.")
    metadata["cilos"] = int(count)
    statements = {}
    for key in bundle.cilos[:metadata["cilos"]]:
        value = str(form.get(key, "")).strip()
        if len(value) > 1500 or INVALID_CONTROL.search(value):
            raise HTTPException(422, f"Enter a valid statement of up to 1,500 characters for {key}.")
        if value:
            statements[key] = value
    metadata["statements"] = statements
    return metadata


def submit_batch(db, settings, owner, metadata, upload):
    batch_id = uuid4().hex
    folder = batch_dir(settings.data, batch_id)
    original = folder / "original"
    original.mkdir(parents=True, exist_ok=False)
    try:
        filename, size, count = stage_pdf(upload, original / "scan.pdf", settings)
        db.submit(batch_id, owner, settings.bundle.version, filename, size, count, metadata)
    except Exception:
        shutil.rmtree(folder)
        raise
    LOG.info("batch_queued batch=%s pages=%s", batch_id, count)
    return batch_id


def update_review(request, batch_id, page, form, settings, record):
    db = request.app.state.db
    decision = form.get("decision")
    if decision not in ("Reviewed", "Unreviewed", "Excluded"):
        raise HTTPException(422, "Select a review decision.")
    with db.connect(write=True) as conn:
        batch = record(request, conn, batch_id)
        rows, _ = db.pages(conn, batch_id)
        current = next((row for row in rows if row["page"] == page), None)
        if not current or current["status"] == "Failed":
            raise HTTPException(404, "This page is unavailable for review.")
        if str(current["version"]) != form.get("version"):
            raise HTTPException(409, "The questionnaire changed in another session. Reload and try again.")
        bundle = settings.bundle if batch["template"] == settings.bundle.version else Bundle(batch["template"])
        updates = []
        reviewed_at = now()
        for key in bundle.active_fields(batch["metadata"]["cilos"]):
            selection = form.get(key, "__keep")
            if selection == "__keep":
                if decision == "Reviewed" and key in current["warnings"] and key not in current["corrections"]:
                    raise HTTPException(422, f"Resolve or confirm a blank response for {key} before marking it reviewed.")
                continue
            value = None if selection == "__blank" else selection
            if value is not None and value not in bundle.choices(key):
                raise HTTPException(422, f"Invalid response for {key}.")
            if value == effective(current, key, bundle) and (key not in current["warnings"]
                    or key in current["corrections"]):
                continue
            updates.append((batch_id, page, key, value, reviewed_at, request.state.owner))
        conn.executemany(
            """INSERT INTO changes(batch_id,page,field,value,changed_at,reviewer)
               VALUES (?,?,?,?,?,?)""", updates,
        )
        conn.execute("""UPDATE pages SET review=?, reviewed_at=?, reviewer=?, version=version+1
                        WHERE batch_id=? AND page=?""",
                     (decision, reviewed_at, request.state.owner, batch_id, page))
    if form.get("pending") == "1":
        if decision == "Unreviewed":
            return f"/batches/{batch_id}/review?pending=true&page={page}"
        following = [row["page"] for row in rows if row["page"] != page and row["review"] == "Unreviewed"]
        next_page = next((number for number in following if number > page), following[0] if following else None)
        suffix = f"&page={next_page}" if next_page else ""
        return f"/batches/{batch_id}/review?pending=true{suffix}"
    return f"/batches/{batch_id}/review?page={page}"


def change_batch(request, batch_id, action, record):
    db = request.app.state.db
    with db.connect(write=True) as conn:
        batch = record(request, conn, batch_id)
        if action == "retry":
            if batch["status"] not in ("Failed", "Interrupted"):
                raise HTTPException(409, "Only failed or interrupted batches can be retried.")
            conn.execute("""UPDATE batches SET status='Queued', started=NULL, finished=NULL, message=NULL
                            WHERE id=?""", (batch_id,))
            conn.execute("DELETE FROM pages WHERE batch_id=?", (batch_id,))
        else:
            if batch["status"] == "Processing":
                raise HTTPException(409, "Wait for processing to finish before deleting this batch.")
            conn.execute("UPDATE batches SET deleted=1 WHERE id=?", (batch_id,))


def native_files(settings, batch, subdir, pattern):
    return sorted((attempt_dir(settings.data, batch) / "output" / subdir).glob(pattern))


def persist_export(folder, batch, pages, filename, data):
    revision = sum(page["version"] for page in pages)
    exports = folder / "exports"
    try:
        exports.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=exports) as directory:
            temp = Path(directory) / filename
            temp.write_bytes(data)
            os.replace(temp, exports / f"{batch['attempt']}-{revision}-{filename}")
    except OSError:
        LOG.warning("export_storage_unavailable batch=%s", batch["id"])


def download(path, media, filename=None):
    if not path.is_file():
        raise HTTPException(404, "This file is not available.")
    return FileResponse(path, media_type=media, filename=filename)


def attachment(content, media, filename):
    return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
