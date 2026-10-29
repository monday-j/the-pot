# Pinned by digest; Dependabot proposes updates.
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

WORKDIR /srv
COPY requirements.txt .
# Exact versions with hashes (requirements.txt, compiled from requirements.in): nothing unpinned or tampered installs.
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements.txt

COPY app ./app

RUN useradd --system --uid 1000 pot && mkdir /data && chown pot /data
USER pot
VOLUME /data
EXPOSE 8000

# One worker only: sync state is an in-process thread + lock (app/sync.py).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
