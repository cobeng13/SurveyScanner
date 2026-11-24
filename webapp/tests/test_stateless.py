from csv import DictReader
from dataclasses import replace
from io import StringIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp.config import Settings
from webapp.stateless import create_app

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
