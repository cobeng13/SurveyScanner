# Course Feedback Survey Processor

**LPU Batangas — Automated Survey Processing**

Download the approved PDF or upload a completed scan and download the recognized responses as CSV or generate a completed Word summary. Each row identifies its PDF page and carries the response fields, processing status and any recognition warnings. Blank and ambiguous responses remain empty. The Docker web app uses temporary files for recognition and clears them when the request ends.

## Deploy on apps-server

Run from the checkout. The image uses the validated OMRChecker engine, headless OpenCV and the original 300 dpi recognition configuration. It runs on internal port 8008.

```bash
git pull --ff-only
docker build -t survey-scanner:local .
```

For a fresh setup, clone `https://github.com/cobeng13/SurveyScanner.git` under `/opt/apps` before building. If an earlier web container uses port 8008, stop it first. The earlier background worker can also be stopped.

```bash
docker run -d --name survey-scanner-csv --restart unless-stopped --init \
  -p 192.168.0.226:8008:8008 --read-only --tmpfs /tmp:size=256m,mode=1777 \
  survey-scanner:local

curl -fsS http://192.168.0.226:8008/health
docker logs --tail 50 survey-scanner-csv
```

Open `http://192.168.0.226:8008`. The CSV-only app requires no account, database, persistent volume, worker container or Cloudflare credentials. Use it on the trusted LAN. If publishing it through an existing Tunnel later, set the origin to `http://192.168.0.226:8008` and apply your access policy at the proxy.

For an update, build first, then run `docker stop survey-scanner-csv` and `docker rm survey-scanner-csv`. Repeat the startup command. Results are downloaded directly and should be saved by the user.

## Word summary

Enter the program, semester, course or subject, and school year, then click **Generate Completed Form (Word)**. The program sets the department chair using the GiftToExam program mapping. The report fills the supplied `Course_feedback_summary.docx` layout, including its official header and signoff. It uses the scanned questionnaire wording for five Teachers and TLAs rows where the printed survey and summary template differ.

The report displays item, section and overall means to two decimals. Each valid response contributes equally to the applicable mean. The verbal interpretation uses the displayed two-decimal score. Blank, multiple-marked and unreadable responses do not contribute to means. The CILO table shows counts and extends the template's numbered rows when votes are detected beyond row four.

## Recognition

Print the official PDF single-sided on 8.5 × 13 in. paper at actual size. Scan at 300 dpi with one completed form and all four fiducials visible on each page. Upload a regular unlocked PDF. The browser waits for the CSV response, so split a large scan if the reverse proxy times out.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `SURVEY_MAX_UPLOAD_MB` | 50 | Upload limit in MiB. |
| `SURVEY_MAX_PAGES` | 40 | Pages per upload. |
| `SURVEY_MAX_RASTER_MP` | 420 | Maximum total raster area in megapixels. |
| `SURVEY_JOB_TIMEOUT` | 900 | Engine timeout in seconds. |
| `SURVEY_CONCURRENCY` | 1 | Simultaneous uploads and recognitions. |
| `SURVEY_PUBLIC_ORIGIN` | Empty | Optional exact public origin for browser uploads. |
| `SURVEY_ROOT_PATH` | Empty | Optional prefix stripped by the reverse proxy. |

Raise temporary space and check server memory before increasing the upload or concurrency limits. The original form, fixed question mapping and alignment assets remain in the [immutable template bundle](webapp/bundles/lpu-v1/manifest.json). The separate batch-review workflow remains in the source tree and is outside the Docker deployment.

## Verification

The app uses the same OMRChecker revision and geometry as the verified physical run. For development, activate the Windows `omrchecker` environment, install `requirements-dev.txt` in an isolated virtual environment and run:

```bash
python -m uvicorn webapp.stateless:create_app --factory --host 127.0.0.1 --port 8008
python -m unittest discover -s webapp/tests -v
```
