FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1

WORKDIR /app
COPY requirements-web.txt constraints-py310.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check --only-binary=:all: -r requirements-web.txt \
    && pip check \
    && python -c "import cv2, numpy, pymupdf; assert cv2.__version__ == '4.12.0' and numpy.__version__ == '2.2.6'; assert any(line.split() == ['GUI:', 'NONE'] for line in cv2.getBuildInformation().splitlines())" \
    && useradd --create-home --uid 10001 survey

COPY LICENSE main.py ./
COPY src/ ./src/
COPY webapp/ ./webapp/
RUN python -c "from webapp.config import Settings; Settings()"

USER 10001:10001
EXPOSE 8008
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8008/health', timeout=3).close()"]
CMD ["uvicorn", "webapp.stateless:create_app", "--factory", "--host", "0.0.0.0", "--port", "8008", "--no-access-log", "--timeout-graceful-shutdown", "35", "--limit-concurrency", "64"]
