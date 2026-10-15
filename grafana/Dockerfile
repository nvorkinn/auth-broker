FROM grafana/grafana:12.4.13

# The VictoriaLogs datasource, installed at build time so the container needs no network at start. Plugins
# and dashboards live outside /var/lib/grafana, which the compose file mounts a volume over.
# Dashboards and the datasource are provisioned from this repo (provisioning/, dashboards/): nothing is
# configured in the UI. Login is by Caddy's forward auth (see the grafana service in docker-compose.yml).
USER root
RUN mkdir -p /opt/grafana-plugins && \
    grafana cli --pluginsDir /opt/grafana-plugins plugins install victoriametrics-logs-datasource 0.32.0 && \
    chown -R grafana:root /opt/grafana-plugins
USER grafana
ENV GF_PATHS_PLUGINS=/opt/grafana-plugins

COPY provisioning/ /etc/grafana/provisioning/
COPY dashboards/ /etc/grafana/dashboards/
