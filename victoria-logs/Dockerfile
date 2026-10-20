FROM victoriametrics/victoria-logs:v1.53.0

# Upstream's defaults, including the one-week retention. Loopback only: the
# container uses host networking, and Caddy is the one way in.
CMD ["-httpListenAddr=127.0.0.1:9428", \
     "-storageDataPath=/victoria-logs-data", \
     "-loggerFormat=json"]
