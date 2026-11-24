import csv
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt
from openpyxl import load_workbook
import pymupdf

from webapp.config import Settings
from webapp.database import batch_dir
from webapp.main import create_app
from webapp.reporting import report
from webapp.worker import process, purge_deleted, run_worker

ORIGIN = "http://127.0.0.1:8008"
SOURCE = Path(os.environ["SURVEY_OMR_SOURCE"]) if os.getenv("SURVEY_OMR_SOURCE") else None


def make_pdf(settings, multiple=False, encrypted=False, empty=False):
    with pymupdf.open() as doc:
        if empty or encrypted:
            doc.new_page()
        else:
            with pymupdf.open(settings.bundle.path / "questionnaire.pdf") as master:
                doc.insert_pdf(master)
            for pos, key in enumerate(settings.bundle.fields):
                field = settings.bundle.fields[key]
                page = doc[0]
                choices = field["choices"]
                picked = [choices[pos % len(choices)]]
                if multiple and key == "Q01":
                    picked.append(choices[1])
                for choice in picked:
                    page.draw_circle(choice["center_pt"], 3.7, color=None, fill=(0, 0, 0))
        protection = {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "user_pw": "test-pass",
                      "owner_pw": "owner-pass"} if encrypted else {}
        return doc.tobytes(**protection)


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.settings = replace(Settings(), data=Path(self.tmp.name), auth="local", public_origin=ORIGIN)
        self.app = create_app(self.settings)
        self.client = self.make_client(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    @staticmethod
    def make_client(app):
        return TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 12345))

    def csrf(self, url="/"):
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200, page.text)
        return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

    def upload(self, content, **changes):
        data = {"program": "BS Biology", "title": "=1+1", "semester": "First Semester",
                "academic_year": "2026–2027", "cilos": "1", "csrf": self.csrf()}
        data.update(changes)
        return self.client.post("/batches", data=data, files={"scan": ("../scanned.pdf", content)},
                                headers={"origin": ORIGIN}, follow_redirects=False)

    def run_batch(self):
        db = self.app.state.db
        batch = db.claim()
        self.assertIsNotNone(batch)
        process(db, self.settings, batch, Event())

    def test_upload_validation_and_official_download(self):
        response = self.client.get("/questionnaire.pdf")
        self.assertEqual(response.content, (self.settings.bundle.path / "questionnaire.pdf").read_bytes())
        self.assertEqual(self.client.get("/health").status_code, 200)
        token = self.csrf()
        for content, message in ((b"wrong signature", "not a PDF"), (b"%PDF-1.7", "corrupted"),
                                 (make_pdf(self.settings, encrypted=True), "Encrypted")):
            response = self.client.post("/uploads/inspect", files={"scan": ("invalid.pdf", content)},
                                        headers={"origin": ORIGIN, "x-csrf-token": token})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn(message, response.json()["detail"])
        response = self.upload(make_pdf(self.settings), cilos="9")
        self.assertEqual(response.status_code, 422)
        self.assertIn("Choose between 0 and 8", response.text)
        self.assertEqual(self.upload(b"invalid", csrf="forged").status_code, 403)

    def test_review_export_and_restart(self):
        content = make_pdf(self.settings)
        result = self.upload(content)
        self.assertEqual(result.status_code, 303, result.text)
        url = result.headers["location"]
        batch_id = url.split("/")[-1]
        self.assertEqual(self.client.get(f"/status/{batch_id}").json()["status"], "Queued")
        self.run_batch()
        self.assertEqual(self.client.get(f"/status/{batch_id}").json()["status"], "Completed")
        body = self.client.get(url)
        self.assertIn("Provisional", body.text)
        self.assertIn("Pages flagged with multiple marks", body.text)
        review = self.client.get(url + "/review?page=1")
        self.assertIn(self.settings.bundle.fields["Q01"]["statement"], review.text)
        self.assertEqual(self.client.get(url + "/file/scan?page=1").headers["content-type"], "image/png")
        self.assertEqual(self.client.get(url + "/file/overlay?page=1").status_code, 200)
        self.assertEqual(self.client.get(url + "/file/original").content, content)
        native = self.client.get(url + "/file/machine")
        self.assertEqual(native.status_code, 200)
        original = list(csv.DictReader(StringIO(native.text)))[0]
        result = self.client.post(url + "/review/1",
                                  data={"csrf": self.csrf(url + "/review"), "version": "0",
                                        "decision": "Reviewed", "Q01": "2", "CILO01": "__blank"},
                                  headers={"origin": ORIGIN}, follow_redirects=False)
        self.assertEqual(result.status_code, 303, result.text)
        stale = self.client.post(url + "/review/1",
                                 data={"csrf": self.csrf(url + "/review"), "version": "0", "decision": "Reviewed"},
                                 headers={"origin": ORIGIN})
        self.assertEqual(stale.status_code, 409)

        with self.make_client(create_app(self.settings)) as restarted:
            self.assertIn("Finalized", restarted.get(url).text)
            self.assertIn("Manually corrected", restarted.get(url + "/review").text)
            result = restarted.get(url + "/file/csv")
            self.assertIn("finalized.csv", result.headers["content-disposition"])
            rows = list(csv.DictReader(StringIO(result.content.decode("utf-8-sig"))))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Q01"], "2")
            self.assertEqual(rows[0]["CILO01"], "")
            self.assertEqual(rows[0]["CILO02"], "")
            self.assertEqual(rows[0]["Source PDF filename"], "scanned.pdf")
            self.assertEqual(rows[0]["Course title"], "'=1+1")
            book = load_workbook(BytesIO(restarted.get(url + "/file/xlsx").content))
            self.assertEqual(book.sheetnames, ["Responses", "Question Mapping", "Summary", "Batch Information",
                                               "Review Log", "Processing Exceptions"])
            self.assertEqual(book["Responses"]["G2"].value, "=1+1")
            self.assertEqual(book["Responses"]["G2"].data_type, "s")
            self.assertEqual(book["Review Log"]["D2"].value, original["Q01"])
            self.assertEqual(book["Review Log"]["E2"].value, "2")
            self.assertEqual(restarted.get(url + "/file/machine").content, native.content)
            self.assertIn("=1+1", restarted.get("/history?course=1%2B1&review=Corrected").text)
            self.assertIn("No survey batches", restarted.get("/history?program=Other").text)

    def test_multiple_marks_and_failed_pages(self):
        marked = make_pdf(self.settings, multiple=True)
        with pymupdf.open() as pdf:
            with pymupdf.open(stream=marked, filetype="pdf") as marked_page:
                pdf.insert_pdf(marked_page)
            pdf.new_page(width=612, height=936)
            scan = pdf.tobytes()
        result = self.upload(scan)
        self.assertEqual(result.status_code, 303, result.text)
        url = result.headers["location"]
        self.run_batch()
        status = self.client.get("/status/" + url.split("/")[-1]).json()
        self.assertEqual((status["status"], status["processed"], status["successful"], status["failed"]),
                         ("Completed with errors", 2, 1, 1))
        self.assertIn("Multiple marks detected", self.client.get(url + "/review?page=1").text)
        self.assertEqual(self.client.get(url + "/file/overlay?page=1").status_code, 200)
        self.assertIn("could not align", self.client.get(url + "/review?page=2").text)
        self.assertIn("Provisional", self.client.get(url).text)
        csv_result = self.client.get(url + "/file/csv")
        row = list(csv.DictReader(StringIO(csv_result.content.decode("utf-8-sig"))))[0]
        self.assertEqual(row["Q01"], "")
        self.assertEqual(row["Includes unreviewed questionnaires"], "Yes")
        response = self.client.post(url + "/review/1",
                                    data={"csrf": self.csrf(url + "/review"), "version": "0",
                                          "decision": "Reviewed"}, headers={"origin": ORIGIN})
        self.assertEqual(response.status_code, 422)
        response = self.client.post(url + "/review/1",
                                    data={"csrf": self.csrf(url + "/review"), "version": "0",
                                          "decision": "Reviewed", "Q01": "__blank"}, headers={"origin": ORIGIN})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Finalized", self.client.get(url).text)

    def test_interruption_retry_and_deletion_isolation(self):
        first = self.upload(make_pdf(self.settings)).headers["location"]
        db = self.app.state.db
        self.assertEqual(db.claim()["status"], "Processing")
        run_worker(self.settings, once=True)
        self.assertIn("Interrupted", self.client.get(first).text)
        response = self.client.post(first + "/retry", data={"csrf": self.csrf(first)}, headers={"origin": ORIGIN},
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.run_batch()
        first_id = first.split("/")[-1]
        second = self.upload(make_pdf(self.settings, empty=True)).headers["location"]
        self.run_batch()
        second_id = second.split("/")[-1]
        self.assertIn("Failed", self.client.get(second).text)
        self.assertEqual(self.client.post(second + "/delete", data={"csrf": self.csrf(second)},
                                          headers={"origin": ORIGIN}).status_code, 422)
        self.assertEqual(self.client.post(second + "/delete",
                                          data={"csrf": self.csrf(second), "confirm": "yes"},
                                          headers={"origin": ORIGIN}, follow_redirects=False).status_code, 303)
        purge_deleted(db, self.settings)
        self.assertEqual(self.client.get(second).status_code, 404)
        self.assertFalse(batch_dir(self.settings.data, second_id).exists())
        self.assertTrue(batch_dir(self.settings.data, first_id).exists())
        self.assertEqual(self.client.get(first + "/file/original").status_code, 200)

    @unittest.skipUnless(SOURCE, "Set SURVEY_OMR_SOURCE to the validated OMRChecker checkout")
    def test_retained_multipage_scan(self):
        source = SOURCE / "lpu-survey" / "results" / "verification" / "multipage_pdf"
        scan = (source / "scans" / "batch.pdf").read_bytes()
        response = self.client.post("/uploads/inspect", files={"scan": ("batch.pdf", scan)},
                                    headers={"origin": ORIGIN, "x-csrf-token": self.csrf()})
        self.assertEqual(response.json()["pages"], 2, response.text)
        result = self.upload(scan, cilos="8")
        self.assertEqual(result.status_code, 303, result.text)
        url = result.headers["location"]
        self.run_batch()
        response = self.client.get(url + "/file/machine")
        self.assertEqual(response.status_code, 200)
        rows = list(csv.DictReader(StringIO(response.text)))
        baseline = list(csv.DictReader(StringIO((source / "recognition" / "responses.csv").read_text())))
        self.assertEqual(len(rows), 2)
        keys = self.settings.bundle.fields
        for have, expected in zip(rows, baseline):
            self.assertEqual({key: have[key] for key in keys}, {key: expected[key] for key in keys})

    def test_access_validation_and_ownership(self):
        settings = replace(self.settings, auth="cloudflare", public_origin="https://survey.example.edu",
                           cf_team="test-team.cloudflareaccess.com", cf_aud="survey-audience")
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with patch("webapp.security.jwt.PyJWKClient") as jwks:
            jwks.return_value.get_signing_key_from_jwt.return_value = SimpleNamespace(key=key.public_key())
            app = create_app(settings)
        time = datetime.now(timezone.utc)

        def headers(subject, audience="survey-audience", expires=time + timedelta(minutes=5)):
            claims = {"sub": subject, "email": f"{subject}@example.edu", "aud": audience,
                      "iss": "https://test-team.cloudflareaccess.com", "iat": time - timedelta(minutes=1),
                      "exp": expires}
            return {"cf-access-jwt-assertion": jwt.encode(claims, key, algorithm="RS256")}

        with TestClient(app, base_url=settings.public_origin) as client:
            db = app.state.db
            batch_id = "a" * 32
            db.submit(batch_id, "one", settings.bundle.version, "scan.pdf", 12, 1, {
                "program": "BS Biology", "title": "Course A", "semester": "Summer",
                "academic_year": "2026", "course_code": "", "faculty": "", "section": "",
                "year_level": "", "cilos": 0, "statements": {},
            })
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/history", headers={"Cf-Access-Authenticated-User-Email":
                                                             "one@example.edu"}).status_code, 401)
            self.assertEqual(client.get("/history", headers=headers("one", "wrong")).status_code, 401)
            self.assertEqual(client.get("/history", headers=headers("one", expires=time)).status_code, 401)
            self.assertIn("Course A", client.get("/history", headers=headers("one")).text)
            self.assertEqual(client.get(f"/batches/{batch_id}/file/original",
                                        headers=headers("two")).status_code, 404)

    def test_summary_denominators(self):
        bundle = self.settings.bundle
        batch = {"metadata": {"cilos": 1, "statements": {}}, "status": "Completed",
                 "started": None, "finished": None}
        pages = [
            {"status": "Machine recognized", "review": "Reviewed", "machine": {
                "Q01": "4", "Q02": "4", "Q07": "1", "CILO01": "Attained", "CILO02": "Not Attained"},
             "corrections": {}, "warnings": {}},
            {"status": "Machine recognized", "review": "Unreviewed",
             "machine": {"Q01": "2", "Q02": "1", "CILO01": "Not Attained"},
             "corrections": {"Q01": "3", "CILO01": None}, "warnings": {}},
            {"status": "Failed", "review": "Excluded", "machine": {},
             "corrections": {}, "warnings": {}},
        ]
        summary = report(batch, pages, bundle)
        self.assertTrue(summary["provisional"])
        self.assertEqual(dict(summary["sections"])["Relevance of Course"], 3)
        self.assertEqual(summary["overall"], 2.6)
        self.assertEqual(summary["items"][0]["frequencies"][0], ("Strongly Agree", 1, .5))
        self.assertEqual(summary["items"][-1]["key"], "CILO01")
        self.assertEqual(summary["items"][-1]["missing"], 1)
        self.assertEqual(summary["items"][-1]["frequencies"][0], ("Attained", 1, 1.0))


if __name__ == "__main__":
    unittest.main()
