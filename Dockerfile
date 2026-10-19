# Counter-Strike 2 dedicated server image for Flux.
#
# Written from scratch. The game's native 64-bit Linux server (Steam app 730, anonymous), installed
# and updated with SteamCMD on every start onto the install volume (~60 GB), under a supervisor
# that builds the launch line from the environment, runs the server in a pseudo-terminal so its
# console reaches the logs and the panel, installs and updates Metamod:Source + CounterStrikeSharp,
# falls back to a safe mode without them when a game update breaks them, quits cleanly on stop and
# restarts the server in place. See README.md.
FROM steamcmd/steamcmd:ubuntu-24

ARG DEBIAN_FRONTEND=noninteractive
ARG FLUX_IMAGE_VERSION=dev

# `script` (util-linux) gives the server its terminal; unzip for CounterStrikeSharp and plugin
# releases; libicu: CounterStrikeSharp's .NET runtime needs it (globalization).
RUN apt-get update -y && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl procps tzdata python3 util-linux unzip libicu74 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY scripts/flux-lib.sh scripts/flux-entrypoint.sh scripts/flux-console.sh scripts/flux-launch.py scripts/fluxcs2.py scripts/plugins.json server.cfg settings.cfg /opt/flux/
RUN chmod 0755 /opt/flux/*.sh /opt/flux/*.py && \
    ln -s /opt/flux/flux-console.sh /usr/local/bin/flux-console && \
    ln -s /opt/flux/fluxcs2.py /usr/local/bin/flux-cs2

RUN steamcmd +quit >/dev/null 2>&1; test -f /root/.local/share/Steam/steamcmd/linux64/steamclient.so

ENV FLUX_IMAGE_VERSION=${FLUX_IMAGE_VERSION}

VOLUME ["/mnt/cs2/server", "/mnt/cs2/data"]

# One port: the game and the Steam query over UDP, RCON over TCP.
EXPOSE 27015/udp 27015/tcp

HEALTHCHECK --interval=60s --timeout=10s --start-period=180m --retries=3 \
    CMD pgrep -f 'linuxsteamrt64/cs2 ' >/dev/null || exit 1

ENTRYPOINT []
CMD ["/opt/flux/flux-entrypoint.sh"]
