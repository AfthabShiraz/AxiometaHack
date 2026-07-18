# Pre-demo checklist

## Network

1. Dedicated Wi-Fi / hotspot — laptop, Quest, glove, CAM, Genesis boards all joined.
2. Set `config.json` camera URL to the CAM’s IP if mDNS fails (`http://10.x.x.x/stream`).
3. Start hub: `source .venv/bin/activate && python -m hub.server`
4. Start tunnel: `./scripts/tunnel.sh 8000` — copy the `https://….trycloudflare.com` URL.

## Quest + cast

1. Pair Quest with Meta Horizon app on the laptop (or open castr.meta.com).
2. Start casting **before** judges arrive; leave the cast window on the second screen.
3. On Quest Browser open:  
   `https://YOUR-VERCEL-OR-HUB/xr/?hub=https://YOUR-TUNNEL.trycloudflare.com`  
   (first time only — URL is saved).
4. Tap **Enter Rover View** once. Allow XR permissions.

## Safety

1. Hub starts with **e-stop latched**. Clear via dashboard **Arm** or squeeze-toggle in XR after you’re ready.
2. Yank USB / kill hub → motors stop within 300 ms (watchdog).
3. Keep a dashboard tab open as backup control.

## Demo acts

1. **Teleop** — passthrough + FPV + head look + glove drive.
2. **Rescue** — mode Rescue, mark victims, show minimap.
3. **Companion** — face / emotion (when face firmware is flashed).
