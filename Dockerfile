# The IP location data the config page suggests a weather location from (see broker/services/geoip.py),
# as of this build: DB-IP City Lite and GeoNames' cities, both CC BY 4.0.
# Data, not code, so it is fetched once on the build machine whatever platform the image is for.
FROM --platform=$BUILDPLATFORM python:3.14-slim AS geodata
COPY scripts/fetch_geodata.py /
RUN python /fetch_geodata.py /geodata

FROM python:3.14-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

# requirements.txt installs vendor/countdown-api (the API contract and its generated code) from here, editable,
# so it has to stay where it is.
COPY vendor/countdown-api/ vendor/countdown-api/
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=geodata /geodata geodata/
COPY broker/ broker/
COPY wsgi.py .

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--access-logfile", "-", "--error-logfile", "-", "--log-level", "info", "wsgi:app"]
