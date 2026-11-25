from csv import DictReader
from dataclasses import replace
from io import BytesIO, StringIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

from fastapi.testclient import TestClient
from lxml import etree

from webapp.config import Settings
from webapp.stateless import SUMMARY_TEMPLATE, create_app
from webapp.summary_report import NS, rated

SOURCE = Path(os.environ["SURVEY_OMR_SOURCE"]) if os.getenv("SURVEY_OMR_SOURCE") else None


class CsvOnlyTests(TestCase):
    def test_download_and_upload_validation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(Settings(), data=root / "unused", public_origin="")
            with TestClient(create_app(settings), base_url="http://192.168.0.226:8008") as client:
                self.assertEqual(client.get("/").status_code, 200)
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.get("/history").status_code, 404)
                self.assertEqual(client.get("/questionnaire.pdf").content,
                                 (settings.bundle.path / "questionnaire.pdf").read_bytes())
                result = client.post("/process", files={"scan": ("fake.pdf", b"invalid PDF")})
                self.assertEqual(result.status_code, 422)
                self.assertIn("The selected file is not a PDF.", result.text)
            self.assertEqual(list(root.iterdir()), [])

    def test_physical_csv_and_temporary_cleanup(self):
        if SOURCE is None:
            self.skipTest("Set SURVEY_OMR_SOURCE to the validated OMRChecker checkout")
        survey = SOURCE / "lpu-survey"
        scan = (survey / "scans" / "PhysicalTest1.pdf").read_bytes()
        with (survey / "results" / "Results" / "Results_04PM.csv").open(encoding="utf-8-sig") as stream:
            expected = next(DictReader(stream))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(Settings(), data=root / "unused", public_origin="")
            with patch("webapp.stateless.TemporaryDirectory",
                       side_effect=lambda **kwargs: TemporaryDirectory(dir=root, **kwargs)):
                origin = "http://192.168.0.226:8008"
                with TestClient(create_app(settings), base_url=origin) as client:
                    result = client.post("/process", files={"scan": ("test.pdf", scan)},
                                         headers={"origin": origin})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertIn("survey-responses.csv", result.headers["content-disposition"])
            rows = list(DictReader(StringIO(result.content.decode("utf-8-sig"))))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Source PDF page number"], "1")
            self.assertEqual(rows[0]["Processing status"], "Machine recognized")
            for key in settings.bundle.fields:
                self.assertEqual(rows[0][key], expected[key], key)
            self.assertEqual(list(root.iterdir()), [])


    def test_mean_interpretation_boundaries(self):
        for count, expected in ((175, "Strongly Disagree"), (176, "Disagree"),
                                (250, "Disagree"), (251, "Agree"),
                                (325, "Agree"), (326, "Strongly Agree")):
            values = [1] * (400 - count) + [4] * (count - 100)
            self.assertEqual(rated(values), (f"{count / 100:.2f}", expected))
        self.assertEqual(rated([]), ("", ""))

    def test_word_summary_from_physical_scan(self):
        if SOURCE is None:
            self.skipTest("Set SURVEY_OMR_SOURCE to the validated OMRChecker checkout")
        survey = SOURCE / "lpu-survey"
        scan = (survey / "scans" / "PhysicalTest1.pdf").read_bytes()
        with (survey / "results" / "Results" / "Results_04PM.csv").open(encoding="utf-8-sig") as stream:
            expected = next(DictReader(stream))
        with TestClient(create_app(), base_url="http://192.168.0.226:8008") as client:
            result = client.post("/process", files={"scan": ("test.pdf", scan)}, data={
                "output": "summary", "program": "Pharmacy", "semester": "2nd",
                "course": "Pharmacology and Therapeutics", "school_year": "2026–2027",
            })
        self.assertEqual(result.status_code, 200, result.text if result.status_code != 200 else None)
        self.assertIn("course-feedback-summary.docx", result.headers["content-disposition"])
        with ZipFile(BytesIO(result.content)) as doc, ZipFile(SUMMARY_TEMPLATE) as template:
            self.assertEqual(doc.namelist(), template.namelist())
            self.assertEqual([name for name in doc.namelist() if doc.read(name) != template.read(name)],
                             ["word/document.xml"])
            body = etree.fromstring(doc.read("word/document.xml")).find("w:body", NS)
        paragraph_text = ["".join(p.xpath(".//w:t/text()", namespaces=NS)) for p in body.findall("w:p", NS)]
        self.assertTrue(any("Pharmacology and Therapeutics" in p for p in paragraph_text))
        self.assertIn("Mr. Aaron Dell A. Cobeng, RPh, MA ELM", paragraph_text)
        self.assertIn("Department Chair", paragraph_text)
        self.assertTrue(any("Overall rating" in p for p in paragraph_text))
        fixed, cilo = body.findall("w:tbl", NS)
        q01 = fixed.findall("w:tr", NS)[2].findall("w:tc", NS)[1]
        self.assertEqual("".join(q01.xpath(".//w:t/text()", namespaces=NS)), f"{int(expected['Q01']):.2f}")
        cilo01 = cilo.findall("w:tr", NS)[1].findall("w:tc", NS)[1:]
        self.assertEqual(["".join(cell.xpath(".//w:t/text()", namespaces=NS)) for cell in cilo01],
                         [str(int(expected["CILO01"] == choice))
                          for choice in ("Attained", "Needs Improvement", "Not Attained")])


    def test_csv_batches_keep_original_page_numbers(self):
        if SOURCE is None:
            self.skipTest("Set SURVEY_OMR_SOURCE to the validated OMRChecker checkout")
        source = SOURCE / "lpu-survey" / "results" / "verification" / "multipage_pdf"
        settings = Settings()
        self.assertEqual((settings.max_mb, settings.max_pages, settings.max_raster_mp), (500, 200, 2800))
        with (source / "recognition" / "responses.csv").open(encoding="utf-8-sig") as stream:
            expected = list(DictReader(stream))
        with patch("webapp.stateless.RECOGNITION_CHUNK_PAGES", 1):
            with TestClient(create_app(settings), base_url="http://192.168.0.226:8008") as client:
                result = client.post("/process", files={"scan": ("test.pdf", (source / "scans" / "batch.pdf").read_bytes())})
        self.assertEqual(result.status_code, 200)
        rows = list(DictReader(StringIO(result.content.decode("utf-8-sig"))))
        self.assertEqual([row["Source PDF page number"] for row in rows], ["1", "2"])
        for row, baseline in zip(rows, expected):
            self.assertEqual([row[key] for key in settings.bundle.fields],
                             [baseline[key] for key in settings.bundle.fields])
