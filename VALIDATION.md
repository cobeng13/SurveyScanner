# Validation results

## Expanded upload limits

A local ASGI run uploaded an exact 500 MiB, 200-page PDF made from the physical fixture. The CSV contained 200 ordered rows and matched all 8,200 expected response fields. The request cleaned up its temporary files. Five app regression tests passed, including CSV and Word generation, and separate checks rejected 201 pages and a 501 MiB request. The updated Docker deployment still needs a smoke test on apps-server.

## Word summary

Four focused web tests passed, including the physical-scan CSV and DOCX downloads, all six verbal interpretation boundaries, the first CILO vote counts, the GiftToExam chair mapping, and the unchanged template package parts. Word rendering confirmed two 8.5 × 13 inch pages with a long course title, all four interpretations and eight populated CILOs. A Python 3.10 Linux wheel was retrieved for the pinned lxml dependency. The updated Docker image has not yet been tested on apps-server.

## CSV-only deployment

The direct PDF-to-CSV route matched all 41 fields in the retained physical baseline and all 82 fields in the retained two-page synthetic scan. Tests confirmed the byte-identical PDF download, invalid file and size rejection, expected page numbers, absence of history routes, and temporary-file cleanup. The Docker and Compose configurations use one service with temporary storage. The updated image still needs a runtime smoke test on apps-server.

## Earlier batch workflow

| Check | Result |
| --- | --- |
| Upstream files | The 41 tracked paths in the source engine checkout matched OMRChecker revision `5cf44a5a9c7a49e2e541c6e19aefd09a41a4d494`, allowing only checkout line-ending differences. The release includes the unchanged runtime source, and the upstream test samples remain in OMRChecker. |
| Immutable template assets | All five files matched the SHA-256 values in the manifest. The test client downloaded the master PDF byte for byte. |
| Upstream synthetic suite | Windows `omrchecker` environment, 3 tests passed. Coverage includes official wording and geometry, patterned and blank PNGs, and the retained two-page PDF. |
| Physical regression | The original Windows runner and the new headless application worker both matched all 41 fields in the retained physical CSV. The application worker also generated the overlay. |
| Application integration | Windows Python 3.10, 7 tests passed. Coverage includes upload validation, multipage recognition, native audit, flags and errors, reviewer corrections and restart persistence, formula protection, CSV and Excel, job recovery, retry, deletion isolation, summary denominators and Access assertion verification. |
| Linux host regression | Ubuntu 22.04, Python 3.10, pinned packages. All 7 app tests passed, the physical scan again matched all 41 response fields with an overlay, and OpenCV identified its GUI backend as NONE. |
| Dependency checks | Windows used a separate environment with only OpenCV Headless 4.12.0.88, NumPy 2.2.6, and PyMuPDF 1.28.2. Pip reported no dependency conflicts. |
| Build files | Compose YAML parsed and the browser JavaScript passed the Node syntax check. |
| Docker image and Compose runtime | Pending. No Docker CLI or daemon was available on the workstation or in its existing Ubuntu environments. Build, health, the in-container physical comparison, graceful interruption and volume persistence across container recreation remain server acceptance checks. |
| Production proxy | Pending. Live port availability and the institutional Access policy and Tunnel route require verification on apps-server. |

The bundled questionnaire SHA-256 is `e9669228de1095653d31f502fce3ca6d9b54e63e3cd3a770e4409027a1449e83`. The regression fixtures remain in the original checkout. The [README](README.md#verification) includes commands for rerunning the checks.
