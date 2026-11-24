"""Compare the approved physical fixture with the application worker, without touching live data."""
import argparse
import csv
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import Event
from uuid import uuid4

from .config import Settings
from .database import Database, batch_dir
from .worker import process


def verify(scan, expected):
    settings = Settings()
    manifest = settings.bundle.manifest["regression"]
    for path, name in ((scan, "lpu-survey/scans/PhysicalTest1.pdf"),
                       (expected, "lpu-survey/results/Results/Results_04PM.csv")):
        if sha256(path.read_bytes()).hexdigest() != manifest[name]:
            raise SystemExit(f"The supplied {path.name} does not match the recorded baseline checksum.")
    with expected.open(encoding="utf-8-sig", newline="") as stream:
        baseline = list(csv.DictReader(stream))
    with TemporaryDirectory(prefix="survey-regression-") as directory:
        settings = replace(settings, data=Path(directory).resolve())
        db = Database(settings.data)
        batch_id = uuid4().hex
        folder = batch_dir(settings.data, batch_id) / "original"
        folder.mkdir(parents=True)
        shutil.copyfile(scan, folder / "scan.pdf")
        db.submit(batch_id, "regression", settings.bundle.version, scan.name, scan.stat().st_size, 1, {})
        process(db, settings, db.claim(), Event())
        with db.connect() as conn:
            batch = db.batch(conn, "regression", batch_id)
            rows, _ = db.pages(conn, batch_id)
        if batch["status"] != "Completed" or len(rows) != 1 or len(baseline) != 1:
            raise SystemExit(f"Physical recognition did not complete successfully: {batch['status']}")
        mismatches = [key for key in settings.bundle.fields
                      if (baseline[0][key] or None) != rows[0]["machine"].get(key)]
        if mismatches or not rows[0]["overlay"]:
            raise SystemExit("Physical match failed. Mismatched fields: " + ", ".join(mismatches))
        print(f"Physical baseline matched all {len(settings.bundle.fields)} responses. Overlay present.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scan", type=Path)
    parser.add_argument("expected_csv", type=Path)
    inputs = parser.parse_args()
    verify(inputs.scan, inputs.expected_csv)
