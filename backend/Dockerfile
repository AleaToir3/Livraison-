FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --system --uid 10001 --no-create-home app
COPY --chown=app:app config.py uk_address_parser.py geocoder_uk.py vision_extractor.py hybrid_matrix.py routing_engine.py main.py ./
USER app

# Les hébergeurs (Render, Railway...) fournissent le port dans $PORT ; 8000 par défaut en local.
ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ['PORT'], timeout=2).status == 200 else 1)"

# uvloop + httptools ; chaque worker uvicorn a son propre pool de processus OR-Tools (SOLVER_WORKERS).
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --loop uvloop --http httptools --ws-max-size 262144 --workers ${UVICORN_WORKERS:-1} --no-access-log"]
