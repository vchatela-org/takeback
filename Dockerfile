FROM python:3.14-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Pending Alpine patches (libssl & co. lag behind the base tag)
RUN apk upgrade --no-cache

WORKDIR /app

COPY requirements.txt .
# pip is only needed at build time; its vendored libraries (urllib3, msgpack,
# setuptools) trip the Harbor scan
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.14/ensurepip

COPY . .

# Everything the app writes (db, uploads, extracted imports) lives under
# /app/data. Run as an unprivileged user owning it: a container running as root
# can escape to the host through a kernel bug (Trivy DS-0002).
RUN mkdir -p /app/data/db /app/data/uploads /app/data/imports \
    && addgroup -S -g 1000 takeback \
    && adduser -S -D -H -u 1000 -G takeback takeback \
    && chown -R takeback:takeback /app

USER takeback

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
