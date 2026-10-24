# Deploy

Two ways to put Reel Maker online. **A** is the main one.

## A. Cloudflare Containers (Workers Paid, runs entirely on Cloudflare)

```
Phone ──https──► makemyreel.in ──► Worker ──► one container (Python + FFmpeg, 4 vCPU)
```

- **Address:** https://makemyreel.in. `www.makemyreel.in` redirects there, and the
  `reel.<you>.workers.dev` address keeps working too. The domain is registered at
  GoDaddy with its nameservers pointed at Cloudflare. The Custom Domains are declared
  in `cloudflare-container/wrangler.jsonc`, so Cloudflare manages DNS and HTTPS.

- Needs the **Workers Paid** plan ($5/month). Usage beyond the included amount is
  billed by Cloudflare; see "Cost" below.
- Your PC can be off. GitHub Actions builds and deploys on every push to `main`.
- Anyone with the link can use it, within these limits (constants at the top of
  `engine/reel_engine/webapp.py` and `pipeline.py`):
  - **Per file:** photos up to 30 MB and 60 megapixels; videos up to 400 MB and 3 minutes.
  - **Per reel:** up to 30 files, 2 GB, and 5 minutes of video in total.
  - **Per visitor (IP):** 2 GB of uploads per hour and 6 GB per day; 10 new reels
    per hour; 6 renders per hour and 20 per day.
  - **Whole site:** at most 8 GB of unrendered uploads and 8 renders waiting; uploads
    are refused when the disk is nearly full.
  - Files are checked as soon as they arrive (unreadable files and too-long videos
    are refused right away), and the page checks size and video length before uploading.
- Uploads are deleted after rendering (unrendered drafts after 2 hours), reels after 20 minutes.
- **One container holds everything** (uploads, the queue, reels). It sleeps after
  30 minutes with no visitors, and **sleeping wipes its disk**, so a reel can't
  outlive that. The first visit after a sleep shows "Reel Maker is starting" for
  up to a minute while it wakes. Cloudflare may also restart a container at any
  time; a render in progress would then fail and need starting again.

### One-time setup

1. **Create an API token** at https://dash.cloudflare.com/profile/api-tokens →
   *Create Token* → *Edit Cloudflare Workers* template → add the permission
   **Account → Containers → Edit** → limit it to your account → *Create*. Copy it.
2. **Add two secrets to the GitHub repo**: Settings → Secrets and variables →
   Actions → *New repository secret*:
   - `CLOUDFLARE_API_TOKEN`: the token from step 1
   - `CLOUDFLARE_ACCOUNT_ID`: your account ID (Workers & Pages overview page)
3. **Make sure the Worker has the proxy secret** (`REEL_PROXY_SECRET`). It was set
   when the earlier `reel` Worker was deployed, and secrets survive deploys. To set
   it again: `cd cloudflare-container`, then
   `Get-Content ..\.reel-secret | npx wrangler secret put REEL_PROXY_SECRET`.
4. **Push to `main`** (or run the workflow from the Actions tab). The workflow runs
   the tests, builds the image, checks it starts, and deploys. The first container
   can take a few minutes to provision after the deploy finishes.

### Cost (rough)

Workers Paid includes 25 GiB-hours of memory, 375 vCPU-minutes and 200 GB-hours of
disk per month. The `standard-4` container reserves 12 GiB and 20 GB while awake,
so every hour it's awake uses about 12 GiB-hours. Each visit keeps it awake for at
least 30 minutes, so roughly 4 visits a month fit in the included memory, and each
extra awake hour costs about $0.11 (memory) + $0.005 (disk), plus CPU while rendering
(about 1–2 cents a reel). For example, 30 awake hours a month is roughly $3 on top
of the $5 plan. To cut cost, lower `INACTIVITY_MINUTES` (reels then disappear sooner
when nobody visits) or use a smaller `INSTANCE` in `cloudflare-container/src/index.ts`
(slower renders).

### Files

- `Dockerfile`: the image (Python 3.12, Debian FFmpeg with zscale, the engine).
- `cloudflare-container/`: the Worker and container class (`wrangler.jsonc`, `src/index.ts`).
- `.github/workflows/deploy.yml`: test, build, smoke-test and deploy.

---

## B. Free: public site on workers.dev, rendering on your PC

```
Phone ──https──► reel-home.<you>.workers.dev ──Workers VPC──► Cloudflare Tunnel ──► your PC
                 (Worker, free plan)       (beta, free)     (cloudflared)       reel ui :8765
```

- Costs nothing: Workers Free, Workers VPC (free while in beta), Cloudflare Tunnel.
- No domain needed. Your PC's address stays hidden and no router ports are opened.
- **Anyone with the link can use it,** within the same per-file, per-reel, per-visitor
  and site-wide limits as option A above.
- The PC must be on and awake. When it's off, visitors see "Reel Maker is offline".

### 1. Prepare the PC (once)

```powershell
cd C:\Users\hardik.sharma\Documents\reel-generator
powershell -ExecutionPolicy Bypass -File scripts\install-startup.ps1
```

This creates `.reel-secret` and a Windows task that starts `reel ui` at login (logs go
to `reel-data\server.log`). Check http://localhost:8765 opens.

In **Settings → System → Power**, set *Sleep* to **Never** while plugged in, or the
site goes offline whenever the PC sleeps.

### 2. Create the tunnel (Cloudflare dashboard)

1. Sign up / log in at https://dash.cloudflare.com (free plan is fine).
2. Go to **Workers VPC → Tunnels → Create**, name it `reel-pc`, save.
3. Choose **Windows / 64-bit** and run the install command it shows, in an
   **administrator** PowerShell. It installs `cloudflared` as a Windows service, so the
   tunnel reconnects on every boot.
4. Wait for the tunnel to show **Healthy**, then copy its **Tunnel ID**.

You don't add a public hostname to the tunnel; the Worker reaches it privately.

### 3. Point Workers VPC at the app

```powershell
cd cloudflare
npm install
npx wrangler login
npx wrangler vpc service create reel-app --type http --tunnel-id <TUNNEL_ID> --ipv4 127.0.0.1 --http-port 8765
```

Copy the **service ID** it prints into `cloudflare/wrangler.jsonc`
(`"service_id": "REPLACE_WITH_VPC_SERVICE_ID"`).

### 4. Deploy the Worker

```powershell
Get-Content ..\.reel-secret | npx wrangler secret put REEL_PROXY_SECRET
npx wrangler deploy
```

It prints your address, e.g. `https://reel-home.<your-subdomain>.workers.dev`. Open it on a
phone (on mobile data, to be sure it works from outside your Wi-Fi) and make a reel.

### If something doesn't work

| Symptom | Fix |
|---|---|
| "Reel Maker is offline" | Is the PC awake? Is `reel ui` running (http://localhost:8765)? Is the tunnel **Healthy** in the dashboard? See `reel-data\server.log`. |
| Offline page even though everything runs | Workers VPC may not reach `127.0.0.1`. Use the PC's LAN IP instead: find it with `ipconfig`, recreate the service with `--ipv4 <LAN IP>`, and start the app on all interfaces by changing `ui --no-browser` to `ui --no-browser --host 0.0.0.0` in `scripts\start-reel.ps1`. (Then anyone on your Wi-Fi can reach it directly too.) |
| Everyone hits "Too many renders" together | The Worker secret doesn't match `.reel-secret`, so all visitors look like one. Re-run the `wrangler secret put` command. |
| Uploads fail on big videos | Each upload piece is 8 MB, far below Cloudflare's 100 MB limit; check `server.log` for the error. Files over 500 MB are refused on purpose. |

### Later: your own domain

When you add a domain to Cloudflare, add a **Custom Domain** to the Worker
(Workers & Pages → the Worker → Settings → Domains & Routes). Nothing else changes.

Workers VPC is in beta; if its pricing or limits change, see `docs/plan.md` for the
paid Cloudflare Containers alternative.
