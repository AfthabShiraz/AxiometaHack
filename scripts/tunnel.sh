#!/usr/bin/env bash
# Expose the local hub (default :8000) as a public HTTPS/WSS URL via Cloudflare.
# Requires: brew install cloudflared
set -euo pipefail
PORT="${1:-8000}"
echo "Tunneling http://localhost:${PORT} ..."
echo "Paste the https://….trycloudflare.com URL into the WebXR app (?hub=wss://…)"
echo "  wss URL = same host with wss://  (path /ws)"
exec cloudflared tunnel --url "http://localhost:${PORT}"
