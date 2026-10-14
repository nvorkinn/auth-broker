# The IP location data the config page suggests a weather location from (see broker/services/geoip.py),
# as of this build: DB-IP City Lite and GeoNames' cities, both CC BY 4.0.
# Data, not code, so it is fetched once on the build machine whatever platform the image is for.
FROM --platform=$BUILDPLATFORM python:3.12-slim AS geodata
COPY scripts/fetch_geodata.py /
RUN python /fetch_geodata.py /geodata

# The source of countdown-api (the contract between the broker and the renderer, and the code generated from it), from
# its latest release. Fetched here, like the data above, so it is as fresh as the image; a cached build keeps the one
# it had (docker build --no-cache-filter countdown-api .).
FROM python:3.12-slim AS countdown-api
COPY scripts/fetch_countdown_api.py /
RUN python /fetch_countdown_api.py /src

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
