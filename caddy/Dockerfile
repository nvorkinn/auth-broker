FROM caddy:2-alpine

COPY Caddyfile /etc/caddy/Caddyfile
# The public pitch page for the display, served as static files (see the Caddyfile).
COPY site /srv/countdown

# Make caddy the entrypoint, as fluent-bit already is in its image, so a
# check is just `docker run --rm <image> <args>` for both. The command it runs
# by default is unchanged.
ENTRYPOINT ["caddy"]
CMD ["run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
