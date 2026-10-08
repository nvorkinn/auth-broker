FROM victoriametrics/victoria-logs:v1.51.0

# Upstream's entrypoint is the binary, as with the other infra images. Fluent
# Bit pushes every container's logs to it. Everything else is upstream's
# default, including the one-week retention.
# Loopback only: the container uses host networking, and Caddy is the one way
# in, at logs.nikolaivorkinn.com.
CMD ["-httpListenAddr=127.0.0.1:9428", \
     "-storageDataPath=/victoria-logs-data"]
