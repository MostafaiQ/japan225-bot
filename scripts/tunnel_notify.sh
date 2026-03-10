#!/bin/bash
# Starts cloudflared tunnel, detects URL, auto-updates GitHub Pages config.
# Used by japan225-tunnel.service

PROJECT="/home/ubuntu/japan225-bot"
CONFIG_FILE="${PROJECT}/docs/api_config.json"
URL_FILE="${PROJECT}/storage/tunnel_url.txt"

# Start cloudflared in background
/usr/local/bin/cloudflared tunnel --url http://localhost:8080 --no-autoupdate 2>&1 &
CF_PID=$!

# Wait for the URL to appear in logs (up to 30s)
for i in $(seq 1 30); do
    sleep 1
    TUNNEL_URL=$(journalctl -u japan225-tunnel -n 50 --no-pager 2>/dev/null | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1)
    if [ -n "$TUNNEL_URL" ]; then
        echo "$TUNNEL_URL" > "$URL_FILE"

        # Check if URL changed
        OLD_URL=""
        [ -f "${CONFIG_FILE}" ] && OLD_URL=$(grep -o 'https://[^"]*' "$CONFIG_FILE" 2>/dev/null)

        if [ "$TUNNEL_URL" != "$OLD_URL" ]; then
            # Update api_config.json
            echo "{\"apiUrl\":\"${TUNNEL_URL}\"}" > "$CONFIG_FILE"

            # Auto-commit and push to GitHub Pages
            cd "$PROJECT"
            git add docs/api_config.json
            git commit -m "auto: update tunnel URL" --no-gpg-sign 2>/dev/null
            git push origin main 2>/dev/null

            echo "Tunnel URL updated: $TUNNEL_URL (pushed to GitHub Pages)"
        else
            echo "Tunnel URL: $TUNNEL_URL (unchanged)"
        fi
        break
    fi
done

# Keep running (wait for cloudflared)
wait $CF_PID
