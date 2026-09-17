FROM python:3.11-slim

# NordVPN's Linux CLI + daemon (nordvpnd), used by vpn.py (via
# nordvpn-switcher) to rotate to a new server whenever Vinted 403s.
# gosu drops root after the daemon is up, since `nordvpn` CLI commands
# refuse to run as root.

# Debian package installers sometimes try to start their service via
# systemctl right after install — there's no systemd in a plain docker
# build, so that call errors and aborts apt-get (this is what breaks the
# NordVPN .deb's postinst below). policy-rc.d blocks any service
# auto-start attempt, and a no-op systemctl covers packages that call it
# directly. Harmless here since nordvpnd is started manually by
# docker-entrypoint.sh, never through systemd.
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod +x /usr/sbin/policy-rc.d \
    && printf '#!/bin/sh\nexit 0\n' > /usr/bin/systemctl \
    && chmod +x /usr/bin/systemctl

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        curl ca-certificates gnupg iproute2 gosu git

# install.sh's own `apt-get install nordvpn` runs without -y — with no TTY
# in a docker build, that Y/n prompt hits EOF and aborts. `yes` answers it.
RUN curl -sSf https://downloads.nordcdn.com/apps/linux/install.sh -o /tmp/nordvpn-install.sh \
    && yes | sh /tmp/nordvpn-install.sh \
    && rm /tmp/nordvpn-install.sh

RUN apt-get clean && rm -rf /var/lib/apt/lists/*

# The installer creates the `nordvpn` group; membership is what lets a
# non-root user talk to nordvpnd's control socket.
RUN useradd --create-home --shell /bin/bash --groups nordvpn vintedapp

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt --no-cache-dir

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

COPY . .
RUN chown -R vintedapp:vintedapp /app

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-u", "app.py"]
