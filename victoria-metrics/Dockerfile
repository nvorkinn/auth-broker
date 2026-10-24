FROM victoriametrics/victoria-metrics:v1.154.1

# The vmui dashboards (its Dashboards tab), read from here each time the page loads.
COPY dashboards/ /vmui-dashboards/

# It scrapes nothing: metrics are pushed to it. Otherwise upstream's defaults,
# including the one-month retention. Loopback only: the container uses host
# networking, and Caddy is the one way in.
CMD ["-httpListenAddr=127.0.0.1:8428", \
     "-storageDataPath=/victoria-metrics-data", \
     "-vmui.customDashboardsPath=/vmui-dashboards", \
     "-loggerFormat=json"]
