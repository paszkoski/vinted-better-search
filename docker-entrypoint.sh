#!/bin/sh
set -e

if [ -n "$NORDVPN_TOKEN" ]; then
    echo "Starting nordvpnd..."
    nordvpnd &

    echo "Waiting for nordvpnd's control socket..."
    for i in $(seq 1 30); do
        [ -S /run/nordvpn/nordvpnd.sock ] && break
        sleep 1
    done

    echo "Logging in to NordVPN..."
    gosu vintedapp nordvpn login --token "$NORDVPN_TOKEN"
    gosu vintedapp nordvpn set technology NordLynx || true
    gosu vintedapp nordvpn set killswitch off || true
else
    echo "WARNING: NORDVPN_TOKEN is not set — VPN rotation on Vinted blocks will not work; the app falls back to a timed cooldown." >&2
fi

exec gosu vintedapp "$@"
