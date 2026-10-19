# The IP location data the config page suggests a weather location from (see broker/services/geoip.py),
# as of this build: DB-IP City Lite and GeoNames' cities, both CC BY 4.0.
# Data, not code, so it is fetched once on the build machine whatever platform the image is for.
FROM --platform=$BUILDPLATFORM python:3.14-slim AS geodata
COPY scripts/fetch_geodata.py /
RUN python /fetch_geodata.py /geodata

# uv itself, to install the dependencies with.
FROM ghcr.io/astral-sh/uv:0.13.1 AS uv

FROM python:3.14-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

# The dependencies, exactly as uv.lock pins them (no dev group), in /app/.venv. vendor/countdown-api (the API contract
# and its generated code) is installed from here, editable, so it has to stay where it is. uv is only mounted for this,
# and keeps no cache, so neither is left in the image.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY vendor/countdown-api/ vendor/countdown-api/
COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv uv sync --locked --no-dev --no-cache
ENV PATH="/app/.venv/bin:$PATH"

COPY --from=geodata /geodata geodata/
COPY broker/ broker/
COPY wsgi.py gunicorn.conf.py ./

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "wsgi:app"]
