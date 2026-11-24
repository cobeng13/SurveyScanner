import os
from pathlib import Path
import re
import subprocess
import sys
from threading import BoundedSemaphore
import unicodedata

from fastapi import HTTPException
from starlette.datastructures import UploadFile

from .config import ROOT

PDF_SLOTS = BoundedSemaphore(2)


def run_pdf(mode, path, number, *limits):
    if not PDF_SLOTS.acquire(blocking=False):
        raise HTTPException(503, "The scan reader is busy. Try again shortly.")
    try:
        engine_python = os.getenv("SURVEY_ENGINE_PYTHON", sys.executable)
        command = [engine_python, "-m", "webapp.pdf_tool", mode, str(path), str(number), *map(str, limits)]
        result = subprocess.run(command,
                                cwd=ROOT, capture_output=True, timeout=25)
    except subprocess.TimeoutExpired:
        raise HTTPException(422, "The PDF took too long to read. Try a smaller or freshly scanned file.") from None
    except OSError:
        raise HTTPException(503, "The PDF reader is unavailable.") from None
    finally:
        PDF_SLOTS.release()
    if result.returncode != 0:
        message = "The PDF could not be opened or rendered."
        if result.returncode == 2 and result.stderr:
            message = result.stderr.decode("utf-8", errors="replace").strip()[:250]
        raise HTTPException(422, message)
    return result.stdout


def sanitized_name(filename):
    name = Path(filename.replace("\\", "/")).name
    name = unicodedata.normalize("NFKC", name)
    stem = re.sub(r"[^\w .()\-]", "_", name[:-4])[:106].strip(" .")
    return f"{stem or 'scan'}.pdf"


def stage_pdf(upload, path, settings):
    if not isinstance(upload, UploadFile) or not upload.filename or not upload.filename.lower().endswith(".pdf"):
        raise HTTPException(422, "Select a PDF file.")
    size = 0
    with path.open("xb") as output:
        for chunk in iter(lambda: upload.file.read(1024 * 1024), b""):
            size += len(chunk)
            if size > settings.max_bytes:
                raise HTTPException(413, f"The PDF exceeds the {settings.max_mb} MiB upload limit.")
            output.write(chunk)
    if not size:
        raise HTTPException(422, "The PDF is empty.")
    count = int(run_pdf("validate", path, settings.max_pages, settings.max_raster_mp))
    return sanitized_name(upload.filename), size, count
