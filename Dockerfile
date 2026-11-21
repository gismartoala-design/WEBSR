FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    TZ=America/Guayaquil

COPY cloudrun/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && groupadd --gid 10001 iess \
    && useradd --uid 10001 --gid iess --create-home iess \
    && mkdir /work \
    && chown iess:iess /work

COPY consultar_iess.py /app/consultar_iess.py
COPY cloudrun /app/cloudrun
USER 10001:10001
WORKDIR /work
ENTRYPOINT ["python", "-m", "cloudrun.worker"]
