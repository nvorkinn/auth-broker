# The IP location data the config page suggests a weather location from (see broker/services/geoip.py),
# as of this build: DB-IP City Lite and GeoNames' cities, both CC BY 4.0.
# Data, not code, so it is fetched once on the build machine whatever platform the image is for.
FROM --platform=$BUILDPLATFORM python:3.12-slim AS geodata
COPY scripts/fetch_geodata.py /
RUN python /fetch_geodata.py /geodata

# The source of countdown-api (the contract between the broker and the renderer, and the code generated from it), from
# its latest release. https://nvorkinn.github.io/countdown-api/latest.json names the latest release's wheel and its
# SHA-256; the wheel is downloaded from that release, checked against the hash, and its countdown_api/ package
# unpacked into /src. Only a release of nvorkinn/countdown-api and a plain countdown_api wheel name are accepted,
# whatever the page says. Data, not code, so it is fetched once on the build machine whatever platform the image is
# for. A cached build keeps the source it had (docker build --no-cache-filter countdown-api .).
FROM --platform=$BUILDPLATFORM alpine:3 AS countdown-api
RUN apk add --no-cache curl jq unzip
ARG COUNTDOWN_API_LATEST=https://nvorkinn.github.io/countdown-api/latest.json
RUN set -eu; \
    curl -fsSL "$COUNTDOWN_API_LATEST" -o /latest.json; \
    wheel="$(jq -r '.files | keys[] | select(endswith(".whl"))' /latest.json)"; \
    [ "$(printf '%s\n' "$wheel" | grep -c .)" = 1 ] || { echo "Expected one wheel in latest.json, found: $wheel" >&2; exit 1; }; \
    case "$wheel" in */*|*..*) echo "Unexpected wheel name in latest.json: $wheel" >&2; exit 1 ;; \
        countdown_api-*-py3-none-any.whl) ;; *) echo "Unexpected wheel name in latest.json: $wheel" >&2; exit 1 ;; esac; \
    page="$(jq -r .release /latest.json)"; \
    case "$page" in *..*) echo "Unexpected release in latest.json: $page" >&2; exit 1 ;; \
        https://github.com/nvorkinn/countdown-api/releases/tag/*) ;; \
        *) echo "latest.json links a release outside nvorkinn/countdown-api: $page" >&2; exit 1 ;; esac; \
    release="$(printf '%s' "$page" | sed 's#/releases/tag/#/releases/download/#')"; \
    curl -fsSL "$release/$wheel" -o "/$wheel"; \
    echo "$(jq -r --arg wheel "$wheel" '.files[$wheel]' /latest.json)  /$wheel" | sha256sum -c -; \
    unzip -q "/$wheel" 'countdown_api/*' -d /src; \
    echo "countdown-api $(jq -r .version /latest.json): $(find /src -type f | wc -l) files"

# Just the source, so a build can export it: docker build --target countdown-api-src --output src .
# (what CI and a development checkout run; the image copies it from the stage above).
FROM scratch AS countdown-api-src
COPY --from=countdown-api /src /

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=geodata /geodata geodata/
COPY broker/ broker/
COPY --from=countdown-api /src src/
ENV PYTHONPATH=/app/src
COPY wsgi.py .

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--access-logfile", "-", "--error-logfile", "-", "--log-level", "info", "wsgi:app"]
