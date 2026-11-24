"""Process an uploaded survey and return a CSV or Word summary without creating a batch record."""

import csv
from io import StringIO
import json
import logging
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore, Event
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from .config import ROOT, Settings
from .csvutil import spreadsheet_value
from .programs import PROGRAM_CHAIRS
from .summary_report import SEMESTERS, generate_report, read_metadata
from .uploads import stage_pdf
from .worker import normalize, run_engine

WEB = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=WEB / "templates")
LOG = logging.getLogger("survey.stateless")
SUMMARY_TEMPLATE = ROOT / "Course_feedback_summary.docx"


class UploadLimit:
    def __init__(self, app, max_bytes):
        self.app = app
        self.limit = max_bytes + 128 * 1024

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        length = Headers(scope=scope).get("content-length")
        if length and length.isascii() and length.isdecimal() and (len(length) > 12 or int(length) > self.limit):
            await JSONResponse({"detail": "The PDF exceeds the configured upload limit."},
                               status_code=413)(scope, receive, send)
            return

        received = 0

        async def limited_receive():
            nonlocal received
            event = await receive()
            received += len(event.get("body", b""))
            if received > self.limit:
                raise StarletteHTTPException(413, "The PDF exceeds the configured upload limit.")
            return event

        await self.app(scope, limited_receive, send)


def create_app(settings=None):
    settings = settings or Settings()
    slots = BoundedSemaphore(settings.concurrency)
    app = FastAPI(title="Course Feedback Survey Processor", root_path=settings.root_path,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(UploadLimit, max_bytes=settings.max_bytes)
    app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")

    def page(request, status=200, error=None):
        return TEMPLATES.TemplateResponse(request, "stateless.html", {
            "path": settings.root_path, "limits": settings, "error": error,
            "program_chairs": PROGRAM_CHAIRS, "semesters": SEMESTERS,
        }, status_code=status, headers={"Cache-Control": "no-store"})

    @app.exception_handler(StarletteHTTPException)
    def problem(request, exc):
        return page(request, exc.status_code, exc.detail)

    @app.get("/health")
    def health():
        return {"status": "ok", "template": settings.bundle.version}

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return page(request)

    @app.get("/questionnaire.pdf")
    def questionnaire():
        return FileResponse(settings.bundle.path / "questionnaire.pdf", media_type="application/pdf",
                            filename="LPU-Course-Feedback-Survey.pdf")

    @app.post("/process")
    async def process_scan(request: Request):
        origin = request.headers.get("origin")
        if origin:
            if settings.public_origin:
                same_origin = origin == settings.public_origin
            else:
                try:
                    parsed = urlsplit(origin)
                    same_origin = (parsed.scheme in {"http", "https"}
                                   and parsed.netloc == request.headers.get("host")
                                   and not (parsed.path or parsed.query or parsed.fragment))
                except ValueError:
                    same_origin = False
            if not same_origin or request.headers.get("sec-fetch-site") == "cross-site":
                raise HTTPException(403, "Submit scans from the survey page.")
        if not slots.acquire(blocking=False):
            raise HTTPException(503, "Another scan is processing. Try again shortly.")
        try:
            async with request.form(max_files=1, max_fields=5, max_part_size=2048) as form:
                output = form.get("output", "csv")
                if output not in ("csv", "summary"):
                    raise HTTPException(422, "Choose CSV or Word summary.")
                metadata = read_metadata(form) if output == "summary" else None
                result = await run_in_threadpool(process_upload, settings, form.get("scan"), metadata)
        finally:
            slots.release()
        media_type, filename = ("text/csv", "survey-responses.csv")
        if metadata is not None:
            media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            filename = "course-feedback-summary.docx"
        return Response(result, media_type=media_type, headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        })

    return app


def process_upload(settings, upload, metadata=None):
    with TemporaryDirectory(prefix="survey-scan-") as directory:
        folder = Path(directory)
        staged = folder / "input"
        staged.mkdir()
        filename, _, total = stage_pdf(upload, staged / "scan.pdf", settings)
        for name in ("template.json", "config.json", "lpu_fiducial.png"):
            shutil.copyfile(settings.bundle.path / name, staged / name)
        output = folder / "output"
        try:
            status = run_engine(settings, staged, output, folder / "recognition.log", Event())
        except TimeoutError:
            raise HTTPException(504, "Recognition timed out. Try a smaller PDF.") from None
        if status:
            LOG.error("survey_recognition_failed exit=%s", status)
            raise HTTPException(422, "Recognition could not finish. Check the scan and try again.")

        page_results = normalize({"id": "upload", "total": total}, settings.bundle, output, folder)
        if metadata is not None:
            return generate_report(SUMMARY_TEMPLATE, settings.bundle, page_results, metadata)
        return format_csv(filename, page_results, settings.bundle)


def format_csv(filename, pages, bundle):
    result = StringIO(newline="")
    writer = csv.writer(result)
    writer.writerow(["Source PDF filename", "Source PDF page number", *bundle.fields,
                     "Processing status", "Recognition warnings"])
    for _, page, _, status, raw, warnings, _, _ in pages:
        machine = json.loads(raw)
        values = [machine.get(key) if machine.get(key) in bundle.choices(key) else None
                  for key in bundle.fields]
        notes = " | ".join(message if key == "_page" else f"{key}: {message}"
                           for key, message in json.loads(warnings).items())
        writer.writerow([spreadsheet_value(filename), page, *values, status, notes])
    return result.getvalue().encode("utf-8-sig")
