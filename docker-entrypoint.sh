#!/bin/sh
set -e

if [ -n "$NORDVPN_TOKEN" ]; then
    echo "Starting nordvpnd..."
    # First run asks a telemetry-consent question on stdin (wants literal
    # "yes"/"no", not "y"/"n") with no TTY attached to answer it — without
    # this it spins forever re-reading EOF as an "invalid response".
    yes "yes" | nordvpnd &

    echo "Waiting for nordvpnd's control socket..."
    for i in $(seq 1 30); do
        [ -S /run/nordvpn/nordvpnd.sock ] && break
        sleep 1
    done

    echo "Logging in to NordVPN..."
    yes "yes" | nordvpn login --token "$NORDVPN_TOKEN"
    # NordLynx (WireGuard-based) doesn't reliably establish tunnels inside a
    # plain Docker container even with NET_ADMIN/tun granted — OpenVPN
    # (userspace, TUN-only) works far more reliably here, at some cost to speed.
    nordvpn set technology OpenVPN || true
    nordvpn set killswitch off || true
else
    echo "WARNING: NORDVPN_TOKEN is not set — VPN rotation on Vinted blocks will not work; the app falls back to a timed cooldown." >&2
fi

exec "$@"
