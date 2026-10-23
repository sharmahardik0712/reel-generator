# Deploy: public site on workers.dev, rendering on your PC (free)

```
Phone ──https──► reel.<you>.workers.dev ──Workers VPC──► Cloudflare Tunnel ──► your PC
                 (Worker, free plan)       (beta, free)     (cloudflared)       reel ui :8765
```

- Costs nothing: Workers Free, Workers VPC (free while in beta), Cloudflare Tunnel.
- No domain needed. Your PC's address stays hidden and no router ports are opened.
- **Anyone with the link can use it.** The app limits each visitor (6 renders/hour,
  30 new projects/hour, 400 upload requests/hour), keeps at most 8 renders waiting,
  and refuses uploads when the disk is nearly full.
- The PC must be on and awake. When it's off, visitors see "Reel Maker is offline".

## 1. Prepare the PC (once)

```powershell
cd C:\Users\hardik.sharma\Documents\reel-generator
powershell -ExecutionPolicy Bypass -File scripts\install-startup.ps1
```

This creates `.reel-secret` and a Windows task that starts `reel ui` at login (logs go
to `reel-data\server.log`). Check http://localhost:8765 opens.

In **Settings → System → Power**, set *Sleep* to **Never** while plugged in, or the
site goes offline whenever the PC sleeps.

## 2. Create the tunnel (Cloudflare dashboard)

1. Sign up / log in at https://dash.cloudflare.com (free plan is fine).
2. Go to **Workers VPC → Tunnels → Create**, name it `reel-pc`, save.
3. Choose **Windows / 64-bit** and run the install command it shows, in an
   **administrator** PowerShell. It installs `cloudflared` as a Windows service, so the
   tunnel reconnects on every boot.
4. Wait for the tunnel to show **Healthy**, then copy its **Tunnel ID**.

You don't add a public hostname to the tunnel; the Worker reaches it privately.

## 3. Point Workers VPC at the app

```powershell
cd cloudflare
npm install
npx wrangler login
npx wrangler vpc service create reel-app --type http --tunnel-id <TUNNEL_ID> --ipv4 127.0.0.1 --http-port 8765
```

Copy the **service ID** it prints into `cloudflare/wrangler.jsonc`
(`"service_id": "REPLACE_WITH_VPC_SERVICE_ID"`).

## 4. Deploy the Worker

```powershell
Get-Content ..\.reel-secret | npx wrangler secret put REEL_PROXY_SECRET
npx wrangler deploy
```

It prints your address, e.g. `https://reel.<your-subdomain>.workers.dev`. Open it on a
phone (on mobile data, to be sure it works from outside your Wi-Fi) and make a reel.

## If something doesn't work

| Symptom | Fix |
|---|---|
| "Reel Maker is offline" | Is the PC awake? Is `reel ui` running (http://localhost:8765)? Is the tunnel **Healthy** in the dashboard? See `reel-data\server.log`. |
| Offline page even though everything runs | Workers VPC may not reach `127.0.0.1`. Use the PC's LAN IP instead: find it with `ipconfig`, recreate the service with `--ipv4 <LAN IP>`, and start the app on all interfaces by changing `ui --no-browser` to `ui --no-browser --host 0.0.0.0` in `scripts\start-reel.ps1`. (Then anyone on your Wi-Fi can reach it directly too.) |
| Everyone hits "Too many renders" together | The Worker secret doesn't match `.reel-secret`, so all visitors look like one. Re-run the `wrangler secret put` command. |
| Uploads fail on big videos | Each upload piece is 8 MB, far below Cloudflare's 100 MB limit; check `server.log` for the error. Files over 500 MB are refused on purpose. |

## Later: your own domain

When you add a domain to Cloudflare, add a **Custom Domain** to the `reel` Worker
(Workers & Pages → reel → Settings → Domains & Routes). Nothing else changes.

Workers VPC is in beta; if its pricing or limits change, see `docs/plan.md` for the
paid Cloudflare Containers alternative.
