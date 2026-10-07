FROM victoriametrics/victoria-metrics:v1.153.0

# Upstream's entrypoint is the binary, as with the Caddy and Fluent Bit images,
# so a check is just `docker run --rm <image> <args>`. It scrapes nothing:
# Fluent Bit forwards every metric to /api/v1/write. Everything else is
# upstream's default, including the one-month retention.
# Loopback only: the container uses host networking, and Caddy is the one way
# in, at metrics.nikolaivorkinn.com.
CMD ["-httpListenAddr=127.0.0.1:8428", \
     "-storageDataPath=/victoria-metrics-data"]
