import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import csv
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from threading import Event
import time
from uuid import uuid4

from .config import Bundle, ROOT, Settings
from .database import Database, batch_dir

LOG = logging.getLogger("survey.worker")


class Interrupted(Exception):
    pass


@contextmanager
def worker_lock(path):
    """Hold an exclusive, OS-released lock across SQLite job recovery."""
    with path.open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if not lock.tell():
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def run_engine(settings, input_dir, output_dir, log_path, stop):
    env = dict(os.environ, MPLBACKEND="Agg", MPLCONFIGDIR=str(log_path.parent / "matplotlib"),
               OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1")
    engine_python = os.getenv("SURVEY_ENGINE_PYTHON", sys.executable)
    cmd = [engine_python, "-m", "webapp.headless", "-i", str(input_dir), "-o", str(output_dir)]
    with log_path.open("wb") as log:
        child = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + settings.timeout
        try:
            while child.poll() is None:
                if stop.wait(0.25):
                    raise Interrupted
                if time.monotonic() >= deadline:
                    raise TimeoutError("Recognition reached its time limit.")
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
    return child.returncode


def read_engine(output, live=False):
    indexed = {}
    groups = (
        ("Machine recognized", sorted((output / "Results").glob("Results_*.csv"))),
        ("Multiple marks", [output / "Manual" / "MultiMarkedFiles.csv"]),
        ("Failed", [output / "Manual" / "ErrorFiles.csv"]),
    )
    for status, paths in groups:
        for path in paths:
            if not path.is_file():
                continue
            with path.open(encoding="utf-8-sig", newline="") as stream:
                lines = stream if not live else [line for line in stream if line.endswith("\n")]
                for row in csv.DictReader(lines):
                    if None in row or any(value is None for value in row.values()):
                        continue
                    key = row["file_id"]
                    if key in indexed:
                        indexed[key] = ("Failed", {})
                    else:
                        indexed[key] = (status, row)
    return indexed


def page_name(page, total):
    return "scan.png" if total == 1 else f"scan_p{page}.png"


def attempt_dir(data, batch):
    return batch_dir(data, batch["id"]) / f"attempt-{batch['attempt']}"


def live_counts(data, batch):
    try:
        indexed = read_engine(attempt_dir(data, batch) / "output", live=True)
    except (OSError, UnicodeError, csv.Error):
        return None
    statuses = [indexed[key][0] for page in range(1, batch["total"] + 1)
                if (key := page_name(page, batch["total"])) in indexed]
    failed = statuses.count("Failed")
    return len(statuses), len(statuses) - failed, failed


def normalize(batch, bundle, output, folder):
    indexed = read_engine(output)
    pages = []
    for page in range(1, batch["total"] + 1):
        file_id = page_name(page, batch["total"])
        status, raw = indexed.get(file_id, ("Failed", {}))
        warnings = {}
        machine = {}
        if status == "Failed":
            warnings["_page"] = ("OMRChecker could not align or read this page."
                                 if raw else "No unique recognition result was produced for this page.")
        else:
            machine = {key: raw.get(key) or None for key in bundle.fields}
            for key, value in machine.items():
                if value is not None and value not in bundle.choices(key):
                    warnings[key] = ("Multiple marks detected." if status == "Multiple marks"
                                     else "Unexpected machine value.")
            if status == "Multiple marks":
                warnings["_page"] = "OMRChecker detected multiple marks on this page."
            elif warnings:
                status = "Recognition warning"
        overlays = output / "CheckedOMRs"
        overlay = overlays / file_id
        if not overlay.is_file():
            overlay = overlays / "_MULTI_" / file_id
        relative = overlay.relative_to(folder).as_posix() if overlay.is_file() else None
        pages.append((batch["id"], page, uuid4().hex, status, json.dumps(machine),
                      json.dumps(warnings), relative, "Excluded" if status == "Failed" else "Unreviewed"))
    return pages


def clean_temporary(run):
    for path in (run / "input", run / "logs" / "matplotlib"):
        if path.exists() and not path.is_symlink():
            shutil.rmtree(path)


def process(db, settings, batch, stop):
    batch_id = batch["id"]
    folder = batch_dir(settings.data, batch_id)
    run = attempt_dir(settings.data, batch)
    try:
        bundle = settings.bundle if batch["template"] == settings.bundle.version else Bundle(batch["template"])
        staged = run / "input"
        staged.mkdir(parents=True)
        logs = run / "logs"
        logs.mkdir()
        for filename in ("template.json", "config.json", "lpu_fiducial.png"):
            shutil.copyfile(bundle.path / filename, staged / filename)
        shutil.copyfile(folder / "original" / "scan.pdf", staged / "scan.pdf")
        if stop.is_set():
            raise Interrupted
        output = run / "output"
        code = run_engine(settings, staged, output, logs / "omrchecker.log", stop)
        pages = normalize(batch, bundle, output, folder)
        successful = sum(page[3] != "Failed" for page in pages)
        if not successful:
            status, info = "Failed", "No pages could be recognized. Check scan quality and page orientation."
        elif code or any(page[3] != "Machine recognized" for page in pages):
            status = "Completed with errors"
            info = "Recognition reported exceptions. Inspect the page warnings and review the affected records."
        else:
            status, info = "Completed", None
        db.finish(batch, status, info, pages)
        LOG.info("recognition_finished batch=%s attempt=%s status=%s exit=%s",
                 batch_id, batch["attempt"], status, code)
    except Interrupted:
        db.finish(batch, "Interrupted", "The worker stopped during recognition. Retry the original scan.")
        LOG.info("recognition_interrupted batch=%s attempt=%s", batch_id, batch["attempt"])
    except Exception as exc:
        message = str(exc) if isinstance(exc, TimeoutError) else "Recognition could not finish. Consult the application logs."
        LOG.error("recognition_error batch=%s attempt=%s type=%s", batch_id, batch["attempt"], type(exc).__name__)
        db.finish(batch, "Failed", message)
    finally:
        try:
            clean_temporary(run)
        except OSError as exc:
            LOG.warning("temporary_cleanup_failed batch=%s type=%s", batch_id, type(exc).__name__)


def purge_deleted(db, settings):
    with db.connect() as conn:
        ids = [row["id"] for row in conn.execute("SELECT id FROM batches WHERE deleted=1")]
    for batch_id in ids:
        try:
            folder = batch_dir(settings.data, batch_id)
            if folder.exists():
                shutil.rmtree(folder)
            with db.connect(write=True) as conn:
                conn.execute("DELETE FROM batches WHERE id=? AND deleted=1", (batch_id,))
        except (OSError, ValueError) as exc:
            LOG.error("batch_deletion_failed batch=%s type=%s", batch_id, type(exc).__name__)


def clear_interrupted_inputs(db, settings):
    with db.connect() as conn:
        batches = conn.execute("SELECT id, attempt FROM batches WHERE attempt>0 AND deleted=0").fetchall()
    for batch in batches:
        folder = batch_dir(settings.data, batch["id"])
        for run in folder.glob("attempt-*"):
            if run.is_dir() and not run.is_symlink():
                try:
                    clean_temporary(run)
                except OSError as exc:
                    LOG.warning("temporary_cleanup_failed batch=%s type=%s", batch["id"], type(exc).__name__)


def run_worker(settings, once=False):
    db = Database(settings.data)
    stop = Event()

    def shutdown(*_):
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    lock_path = db.path.parent / "worker.lock"
    with worker_lock(lock_path):
        db.recover()
        clear_interrupted_inputs(db, settings)
        with ThreadPoolExecutor(max_workers=settings.concurrency) as pool:
            active = set()
            while not stop.is_set():
                done = {future for future in active if future.done()}
                for future in done:
                    future.result()
                active -= done
                purge_deleted(db, settings)
                while len(active) < settings.concurrency and not stop.is_set():
                    batch = db.claim()
                    if not batch:
                        break
                    active.add(pool.submit(process, db, settings, batch, stop))
                if once and not active:
                    break
                stop.wait(1)
            for future in active:
                future.result()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the persistent survey worker")
    parser.add_argument("--once", action="store_true", help="Exit once the available queue has been processed")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_worker(Settings(), once=parser.parse_args().once)
