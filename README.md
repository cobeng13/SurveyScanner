# Course Feedback Survey Processor

**LPU Batangas — Automated Survey Processing**

This application processes the approved LPU Batangas questionnaire locally with OMRChecker. Staff submit one course or class per batch, verify the scan and overlay against the extracted answers, and download CSV and Excel results. A FastAPI web service and a separate worker share a SQLite database and batch storage. Production runs in Linux Docker using headless OpenCV and PyMuPDF.

## Recognition baseline

The [lpu-v1 manifest](webapp/bundles/lpu-v1/manifest.json) records SHA-256 checksums for the exact printable form, configuration, recognition template, fiducial and official mapping. It also records the OMRChecker revision and the checksums of the retained Windows regression artifacts. The engine code in `main.py` and `src/` is from the validated checkout `5cf44a5a9c7a49e2e541c6e19aefd09a41a4d494` (`v1.1.0-29-g5cf44a5`). The engine CLI still runs with `-i INPUT -o OUTPUT`. The app’s headless launcher disables graphical operations and filters response values out of ordinary engine logs.

The original scan goes to the engine as a PDF. The validated configuration renders every page in grayscale at 300 dpi, uses the original marker crop and bubble layout, and saves overlays. OMRChecker reports missing alignment and multiple marks separately. For ambiguous responses the normalized dataset contains a missing value until a reviewer records a correction. The original machine value, including multiple-mark strings, remains in the audit record.

The questionnaire is single-sided 8.5 × 13 inches. Print at 100% or Actual Size. Staff enter handwritten metadata and optional CILO statements manually. Only the first N CILO rows, as selected for the batch, contribute to exports and summaries. Unused CILO columns remain empty.

## Local Windows development

From this project root, use PowerShell. A separate virtual environment keeps the production pins isolated. The Windows `omrchecker` environment provides the validated Python 3.10 interpreter for this setup.

```powershell
conda activate omrchecker
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:SURVEY_AUTH = 'local'
$env:SURVEY_PUBLIC_ORIGIN = 'http://127.0.0.1:8008'
.\.venv\Scripts\python.exe -m uvicorn webapp.main:create_app --factory --host 127.0.0.1 --port 8008 --no-proxy-headers
```

In a second PowerShell terminal, activate the same Conda environment and run the worker from the same project root.

```powershell
conda activate omrchecker
.\.venv\Scripts\python.exe -m webapp.worker
```

Open `http://127.0.0.1:8008`. Local development stores data in `survey-data/` by default. Local mode accepts connections from loopback peers and provides a single development identity. Use it only on the workstation. The production container uses Cloudflare Access.

## Linux apps-server deployment

The stack follows the deployed apps’ Docker Slim, loopback publishing, health check, restart and bounded logging conventions. The separate web and worker containers use the storage pattern from PacheckEssay. The Scheduler runbook establishes the `/opt/apps` deployment directory and the host, container and separate-guest options for Tunnel origins.

The documented existing allocations include 8000–8007. The image listens on **8008**. The commands below publish it as **127.0.0.1:8008**. Check the current apps-server listeners and Docker mappings before first startup. For a Tunnel connector on another guest, publish to the apps-server LAN address, for example `--publish 192.168.0.226:8008:8008`, and use `http://192.168.0.226:8008` for health checks and the Tunnel origin. If you need another host port, change the host side of `--publish`.

```bash
cd /opt/apps
git clone https://github.com/cobeng13/SurveyScanner.git
cd /opt/apps/SurveyScanner
umask 077
cp -n .env.example .env
docker ps --format 'table {{.Names}}\t{{.Ports}}'
sudo ss -ltn 'sport = :8008'
```

The web container can start before Cloudflare Access is configured. In this state, `/health` returns `access: setup_required` and all other routes return HTTP 503. Use the same trusted deployment account for build, updates and backup operations.

```bash
docker build -t survey-scanner:local .
docker network create survey-scanner
docker volume create survey-scanner-data
```

Start both containers using the same image and data volume.

```bash
docker run -d --name survey-scanner-web --restart unless-stopped --init \
  --network survey-scanner --publish 127.0.0.1:8008:8008 \
  --env-file .env --env SURVEY_AUTH=cloudflare \
  --volume survey-scanner-data:/data --read-only --tmpfs /tmp:size=256m,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 160 --memory 768m \
  --stop-timeout 45 --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 survey-scanner:local

docker run -d --name survey-scanner-worker --restart unless-stopped --init \
  --network none --no-healthcheck --env-file .env \
  --volume survey-scanner-data:/data --read-only --tmpfs /tmp:size=64m,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 160 --memory 1500m \
  --stop-timeout 30 --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
  survey-scanner:local python -m webapp.worker

curl -fsS http://127.0.0.1:8008/health
docker logs --tail 100 survey-scanner-web
docker logs --tail 100 survey-scanner-worker
```

The `survey-scanner-web` and `survey-scanner-worker` containers restart with the `unless-stopped` policy. The worker has no network and processes locally. The web container fetches only public signing keys from the configured Cloudflare team hostname for Access verification. Access verifies a signed RS256 assertion, issuer, audience and token lifetime. Batch access is restricted to its uploader’s Access subject. Review actions store that subject as the reviewer identifier. A distinct account cannot see, change or download another user’s batch. Administrative backup and restore occur at the server level.

### Cloudflare and proxies

When the Access application is ready, set `SURVEY_PUBLIC_ORIGIN` to the public HTTPS origin, `SURVEY_CF_TEAM` to the team hostname and `SURVEY_CF_AUD` to the application audience tag in `.env`. Allow only authorized staff or groups in Access. Recreate the web container using its startup command to apply the new environment. The worker can stay running. Check that `/health` reports `access: cloudflare`, then sign in through the public hostname.

- With a host-installed Tunnel on apps-server, set the origin service to `http://127.0.0.1:8008`.
- With a Docker-based connector on that host, attach the connector to the private `survey-scanner` network through its own deployment configuration and target `http://survey-scanner-web:8008`.
- With a connector in another Proxmox guest, bind the port to the specific apps-server LAN IP, permit only the connector’s IP through the host firewall and target the configured LAN address and port. Keep the origin off the public network.
- Preserve the `Cf-Access-Jwt-Assertion` header on every protected request. Restrict forwarded headers to the actual proxy addresses seen by Uvicorn. Set `SURVEY_PUBLIC_ORIGIN` to the public scheme and authority without a path. For a proxy that strips a URL prefix, set `SURVEY_ROOT_PATH` to that prefix. A dedicated HTTPS hostname at the root is the deployment default.
- Pass upload bodies of at least the configured file limit plus multipart overhead. Cloudflare or another proxy can have a lower cap. Bypass caching for survey pages and file routes. Verify upload, preview, polling, review submission and downloads through the actual proxy after the container smoke test.
- `GET /health` reports database readiness, Access configuration status and the loaded template revision. All batch routes require Access. Polling reports definitive counts after recognition, plus complete native CSV rows during processing when available.

Cloudflare documents the issuer, audience and signing-key setup in [Validate JWTs](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/).

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `SURVEY_PUBLIC_ORIGIN` | Required in production | External HTTPS origin used for same-origin form validation. |
| `SURVEY_CF_TEAM` | Required in production | Access team hostname, such as `team.cloudflareaccess.com`. |
| `SURVEY_CF_AUD` | Required in production | Access application audience tag. |
| `SURVEY_BIND_ADDRESS` | `127.0.0.1` | Host address Docker publishes. |
| `SURVEY_HOST_PORT` | `8008` | Published host port. |
| `SURVEY_ROOT_PATH` | Empty | External path prefix stripped by the reverse proxy. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Comma-separated trusted Uvicorn proxy peers for plain Docker. |
| `SURVEY_MAX_UPLOAD_MB` | `50` | Maximum file size in MiB, configurable from 1–500. |
| `SURVEY_MAX_PAGES` | `40` | Maximum PDF pages, configurable from 1–120. |
| `SURVEY_MAX_RASTER_MP` | `420` | Maximum total 300 dpi raster area per PDF, in megapixels. |
| `SURVEY_JOB_TIMEOUT` | `900` | Maximum engine runtime in seconds, configurable from 5–7200. |
| `SURVEY_CONCURRENCY` | `1` | Simultaneous batches in one worker, configurable from 1–4. |
| `SURVEY_WEB_TEMP` | `256m` | Web multipart and PDF preview temporary storage. |
| `SURVEY_WEB_MEMORY` | `768m` | Web container memory limit. |
| `SURVEY_WORKER_MEMORY` | `1500m` | Worker container memory limit. |
| `SURVEY_VOLUME_NAME` | `survey-scanner-data` | Named volume used by both containers. |
| `SURVEY_DATA` | `/data` in Docker | Data root. Defaults to `survey-data/` in local Python. |
| `SURVEY_AUTH` | `cloudflare` | Set to `local` for loopback development only. |
| `SURVEY_ENGINE_PYTHON` | Current interpreter | Optional Windows development override for a separate engine interpreter. |

The plain Docker commands set the bind address, host port, temporary space, memory and volume in the run flags. The corresponding `SURVEY_*` deployment variables in `.env.example` apply to the optional Compose file.

The PDF reader caps 300 dpi raster area at 14 million pixels per page and uses the configured batch-wide raster limit. Adjust the proxy, web temporary capacity and memory if raising upload limits. The engine loads all page rasters in one invocation, so test representative jobs before raising concurrency or raster limits.

## Storage, backup and updates

The named Docker volume mounts at `/data` on both services and is initialized for UID/GID `10001:10001`. Keep the volume on a local Linux filesystem that supports SQLite WAL and file locks. If substituting a bind mount, create it with mode `0700` and ownership `10001:10001` before startup.

```text
/data/
  database/survey.sqlite3
  batches/<batch-id>/
    original/scan.pdf
    attempt-<n>/output/Results/
    attempt-<n>/output/Manual/
    attempt-<n>/output/CheckedOMRs/
    attempt-<n>/logs/omrchecker.log
    exports/
```

Staged copies and plotting caches are cleared after recognition and when the worker starts after an interruption. Native CSVs, overlays, logs, original scans and generated export snapshots remain until batch deletion. Confirmed deletion hides the selected batch immediately and the worker removes its files and associated database rows. SQLite queue claims, immutable attempt paths and transactional result writes keep retries isolated. A worker restart marks abandoned jobs Interrupted. Retry uses the retained original PDF.

For a consistent backup, pick a maintenance window and allow active recognition to finish. Copy the full volume to restricted storage on a separate host or backing device. This example assumes an absolute backup directory writable by UID 10001 and protected from other local accounts.

```bash
backup_dir=/path/to/restricted/survey-backups
backup_file="survey-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
docker stop survey-scanner-web survey-scanner-worker
docker run --rm --network none --no-healthcheck --volume survey-scanner-data:/data:ro \
  --volume "$backup_dir:/backup" --entrypoint /bin/sh survey-scanner:local \
  -c 'umask 077 && tar -czf "/backup/$1" -C /data .' sh "$backup_file"
docker start survey-scanner-web survey-scanner-worker
```

For restoration, stop the stack. Choose a new volume unique to this recovery. The archive includes scans and the database. Keep the previous volume until the restored app has passed its health check and an authorized reviewer has opened a batch.

```bash
docker stop survey-scanner-web survey-scanner-worker
docker volume create survey-scanner-restored
docker run --rm --network none --no-healthcheck --volume survey-scanner-restored:/data \
  --volume "$backup_dir:/backup:ro" --entrypoint tar survey-scanner:local \
  -xzf "/backup/$backup_file" -C /data
docker run --rm --network none --no-healthcheck --volume survey-scanner-restored:/data \
  --entrypoint python survey-scanner:local -c \
  "import sqlite3; db=sqlite3.connect('/data/database/survey.sqlite3'); assert db.execute('PRAGMA quick_check').fetchone()==('ok',)"
docker rm survey-scanner-web survey-scanner-worker
```

Repeat the two container startup commands with `survey-scanner-restored:/data` in both volume flags. Protect and retain backups under institutional policy. Copy the entire volume, including any SQLite sidecar files.

To update, back up the volume, pull the reviewed revision, build while the active image is running, and recreate the services using the same `.env` and volume.

```bash
git pull --ff-only
docker build -t survey-scanner:local .
docker stop survey-scanner-web survey-scanner-worker
docker rm survey-scanner-web survey-scanner-worker
```

Repeat the two container startup commands with the existing data volume, then check health and logs. Use `docker stop survey-scanner-web survey-scanner-worker` to stop the application. Review the image and volume before rolling back a version with schema changes.

## Summary and audit

Fixed item distributions use the number of valid answers for that item as the percentage denominator. The section mean pools all valid fixed-item ratings in the section. The overall mean pools all valid answers across the 33 fixed items. Every valid answer has equal weight. CILO attainment percentages use valid responses from the applicable CILO row. Missing values stay missing. Failed and manually excluded questionnaires never enter the denominator.

Downloads use the latest saved corrections, with the native machine CSV and machine audit files separately available. CSV names and the workbook identify provisional or finalized data. A result remains provisional while an included questionnaire is unreviewed or has an unresolved recognition issue. Finalized counts can still disclose pages that failed or were excluded. Excel includes Responses, Question Mapping, Summary, Batch Information, Review Log and Processing Exceptions.

## Verification

Run the application tests from the project root. For the retained multipage regression, point `SURVEY_OMR_SOURCE` to the original OMRChecker checkout. The physical comparison uses the original fixtures from that checkout. Keep those scans and outputs local.

```powershell
$env:SURVEY_OMR_SOURCE = 'C:\OCR\OMRChecker'
.\.venv\Scripts\python.exe -m unittest discover -s webapp/tests -v
.\.venv\Scripts\python.exe -m webapp.verify_baseline `
  "$env:SURVEY_OMR_SOURCE\lpu-survey\scans\PhysicalTest1.pdf" `
  "$env:SURVEY_OMR_SOURCE\lpu-survey\results\Results\Results_04PM.csv"
```

For a Linux image recognition smoke test, mount a restricted directory holding only the approved physical fixture and its baseline CSV. The verifier checks both file hashes, runs the worker in scratch storage, compares all 41 response values and checks the overlay. The mounts below are read-only. Scratch results are automatically discarded.

```bash
docker run --rm --network none --no-healthcheck \
  --volume /absolute/path/to/regression-fixtures:/fixtures:ro \
  survey-scanner:local python -m webapp.verify_baseline /fixtures/PhysicalTest1.pdf /fixtures/Results_04PM.csv
```

Then test through the protected hostname with the same scan and a multipage batch. Verify the form checksum, exports and flags, then use a disposable batch to test interruption, retry, restart and container recreation with the same volume. See [validation results](VALIDATION.md) for executed checks.

## Troubleshooting and template versioning

- **PDF rejected.** Open it locally, unlock it if needed, rescan or re-export as a regular PDF, and check the configured limits and proxy cap.
- **No recognizable responses.** Check that the PDF is upright, that each page contains one form, and that all four fiducials are fully visible. Confirm that scaling was disabled when printing. Review page warnings and `docker logs survey-scanner-worker`.
- **Queued jobs stay queued.** Check `docker ps -a --filter name=survey-scanner-worker`, worker logs and volume ownership. A single worker instance should own the volume lock.
- **An attempt was interrupted.** Restart the worker if necessary, then retry the original scan from batch results. Verify successful pages before exporting.
- **Access fails.** Confirm the actual hostname, Access audience, sign-in policy, team hostname, origin URL and assertion header. The browser must use the configured public origin for edits and uploads.

Use a new bundle directory and version for a future approved questionnaire revision. Verify the PDF, mapping, configuration and marker together against a physical and synthetic baseline. Record new checksums and the engine revision, keep old bundles for existing records and explicitly switch the default after verification. Keep the original source files and test artifacts in the OMRChecker checkout.
