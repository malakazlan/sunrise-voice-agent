FROM coturn/coturn:4.19.2@sha256:ccdbc51e6f072130b0fc9eebda21f8d026d0a25581746c52557e887818011a3f
COPY turnserver.conf /etc/coturn/sunrise-base.conf
COPY entrypoint.sh /usr/local/bin/sunrise-coturn
# The image's own entrypoint passes each argument through `eval echo`, which drops options
# such as -n; this one starts coturn directly.
ENTRYPOINT ["/bin/sh", "/usr/local/bin/sunrise-coturn"]
CMD []
