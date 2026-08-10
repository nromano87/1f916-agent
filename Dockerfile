# Public 1F916 Watch — read-only citizen windows (no citizen secret, no engage).
# Optional: F916_PUBLISH_TOKEN accepts redacted allowance POSTs from an operator machine.
# Docs: https://fly.io/docs/languages-and-frameworks/dockerfile/
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# Persist guestbook hits (and any other Store files) on the Fly volume.
ENV F916_HOME=/data

EXPOSE 8080

# Bind all interfaces so Fly Proxy can reach the process.
CMD ["f916", "watch", "--host", "0.0.0.0", "--port", "8080", "--no-open"]
