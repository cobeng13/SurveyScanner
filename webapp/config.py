import os
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
BUNDLES = ROOT / "webapp" / "bundles"
PROGRAMS = ("BS Biology", "BS Pharmacy", "BS Medical Laboratory Science",
            "BS Radiologic Technology", "BS Physical Therapy", "Other")
SEMESTERS = ("First Semester", "Second Semester", "Summer")
STATUSES = ("Queued", "Processing", "Completed", "Completed with errors", "Failed", "Interrupted")
RATINGS = {"4": "Strongly Agree", "3": "Agree", "2": "Disagree", "1": "Strongly Disagree"}


def integer(name, default, minimum, maximum):
    value = int(os.getenv(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Bundle:
    version: str
    path: Path = field(init=False)
    fields: dict = field(init=False)
    manifest: dict = field(init=False)

    def __post_init__(self):
        if not re.fullmatch(r"lpu-v[1-9][0-9]*", self.version):
            raise ValueError("Unknown questionnaire bundle")
        path = BUNDLES / self.version
        manifest = read_json(path / "manifest.json")
        for filename, digest in manifest["files"].items():
            if sha256((path / filename).read_bytes()).hexdigest() != digest:
                raise ValueError(f"Recognition asset checksum mismatch: {filename}")
        fields = read_json(path / "question_mapping.json")["fields"]
        if list(fields) != read_json(path / "template.json")["outputColumns"]:
            raise ValueError("Question mapping order differs from template")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "manifest", manifest)

    @property
    def cilos(self):
        return [key for key in self.fields if key.startswith("CILO")]

    def active_fields(self, count):
        inactive = set(self.cilos[count:])
        return [key for key in self.fields if key not in inactive]

    def choices(self, key):
        return [choice["value"] for choice in self.fields[key]["choices"]]


@dataclass(frozen=True)
class Settings:
    data: Path = field(default_factory=lambda: Path(os.getenv("SURVEY_DATA", ROOT / "survey-data")).resolve())
    bundle: Bundle = field(default_factory=lambda: Bundle("lpu-v1"))
    max_mb: int = field(default_factory=lambda: integer("SURVEY_MAX_UPLOAD_MB", 50, 1, 500))
    max_pages: int = field(default_factory=lambda: integer("SURVEY_MAX_PAGES", 40, 1, 120))
    max_raster_mp: int = field(default_factory=lambda: integer("SURVEY_MAX_RASTER_MP", 420, 10, 1300))
    concurrency: int = field(default_factory=lambda: integer("SURVEY_CONCURRENCY", 1, 1, 4))
    timeout: int = field(default_factory=lambda: integer("SURVEY_JOB_TIMEOUT", 900, 5, 7200))
    public_origin: str = field(default_factory=lambda: os.getenv("SURVEY_PUBLIC_ORIGIN", "").rstrip("/"))
    root_path: str = field(default_factory=lambda: os.getenv("SURVEY_ROOT_PATH", "").rstrip("/"))
    auth: str = field(default_factory=lambda: os.getenv("SURVEY_AUTH", "cloudflare"))
    cf_team: str = field(default_factory=lambda: os.getenv("SURVEY_CF_TEAM", ""))
    cf_aud: str = field(default_factory=lambda: os.getenv("SURVEY_CF_AUD", ""))

    @property
    def max_bytes(self):
        return self.max_mb * 1024 * 1024

    def check_web(self):
        """Validate startup options and report whether user routes can be enabled."""
        if self.root_path and not re.fullmatch(r"(/[A-Za-z0-9_-]+)+", self.root_path):
            raise ValueError("SURVEY_ROOT_PATH must contain only URL path segments")
        if self.auth not in {"cloudflare", "local"}:
            raise ValueError("SURVEY_AUTH must be cloudflare or local")
        try:
            origin = urlsplit(self.public_origin)
            valid_origin = bool(
                origin.scheme in {"http", "https"} and origin.hostname and not (
                    origin.path or origin.query or origin.fragment or origin.username or origin.password
                ) and (origin.port is None or origin.port > 0)
            )
        except ValueError:
            origin, valid_origin = None, False
        if self.auth == "cloudflare":
            return bool(valid_origin and origin.scheme == "https" and self.cf_aud.strip()
                        and re.fullmatch(r"[a-z0-9-]+\.cloudflareaccess\.com", self.cf_team))
        if self.public_origin and not valid_origin:
            raise ValueError("SURVEY_PUBLIC_ORIGIN must contain only the scheme and authority")
        return True
