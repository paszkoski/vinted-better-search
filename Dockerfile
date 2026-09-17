FROM python:3.11-slim

# NordVPN's Linux CLI + daemon (nordvpnd), used by vpn.py (via
# nordvpn-switcher) to rotate to a new server whenever Vinted 403s.
# gosu drops root after the daemon is up, since `nordvpn` CLI commands
# refuse to run as root.
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        curl ca-certificates gnupg iproute2 gosu git \
    && curl -sSf https://downloads.nordcdn.com/apps/linux/install.sh | sh \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

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
