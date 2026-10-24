/**
 * Reel Maker on Cloudflare Containers.
 *
 * reel.<you>.workers.dev  ->  this Worker  ->  one ReelContainer (Durable Object)
 *   ->  the container running `reel ui` on port 8080 (Python + FFmpeg)
 *
 * There is exactly one container: uploads, the job queue and finished reels live on
 * its disk and in its memory. It sleeps after INACTIVITY_MINUTES without requests,
 * and sleeping wipes its disk, so that is set longer than the app's 20 minute reel TTL.
 *
 * Deploys: Cloudflare doesn't replace a running Durable Object container when a new
 * image is deployed. So the object remembers which image it started the container
 * with; when that differs from the current image, an alarm asks the app every minute
 * whether anything is in use (a render, an upload, unexpired reels, an active draft)
 * and restarts onto the new image as soon as nothing would be lost.
 */
import { DurableObject } from "cloudflare:workers";

const PORT = 8080;
// Reels stay downloadable for 20 minutes; keep the container awake a little longer.
const INACTIVITY_MINUTES = 30;
// 4 vCPU / 12 GiB / 20 GB disk: the largest predefined type, for fast renders.
const INSTANCE = "standard-4";
// CI calls this after each deploy so the switch-over starts without waiting for a visitor.
const DEPLOY_CHECK_PATH = "/__reel/deploy-check";
const UPGRADE_CHECK_EVERY_MS = 60 * 1000;
const STARTED_IMAGE_KEY = "startedImage";

interface Env {
	REEL: DurableObjectNamespace<ReelContainer>;
	REEL_PROXY_SECRET: string; // `wrangler secret put REEL_PROXY_SECRET`
}

export class ReelContainer extends DurableObject<Env> {
	private starting: Promise<void> | undefined;
	private monitoring: Promise<void> | undefined;

	constructor(ctx: DurableObjectState, env: Env) {
		super(ctx, env);
		const container = ctx.container;
		if (container?.running) {
			// Restore the timeout and exit monitoring after a Durable Object restart.
			void ctx.blockConcurrencyWhile(() =>
				container.setInactivityTimeout(INACTIVITY_MINUTES * 60 * 1000),
			);
			this.observeExit();
		}
	}

	async fetch(request: Request): Promise<Response> {
		const url = new URL(request.url);
		if (url.pathname === DEPLOY_CHECK_PATH) {
			// Never wakes a sleeping container: its next start uses the new image anyway.
			const scheduled = await this.scheduleUpgradeCheck(0);
			return new Response(scheduled ? "upgrade check scheduled\n" : "up to date\n");
		}

		// Share startup across concurrent requests, including after a failed attempt.
		this.starting ??= this.startAndWaitForPort().finally(() => {
			this.starting = undefined;
		});
		await this.starting;
		// A visitor also triggers the check, in case the CI ping didn't arrive.
		this.ctx.waitUntil(this.scheduleUpgradeCheck(5000));

		url.protocol = "http:";
		url.host = "container";
		const forwarded = new Request(url, request);
		forwarded.headers.delete("host");
		return this.ctx.container!.getTcpPort(PORT).fetch(forwarded);
	}

	private async startAndWaitForPort(): Promise<void> {
		const container = this.ctx.container!;
		if (!container.running) {
			container.start({
				image: container.images.base,
				instance: INSTANCE,
				// Song search (YouTube title, iTunes previews) needs the internet.
				enableInternet: true,
				env: { REEL_PROXY_SECRET: this.env.REEL_PROXY_SECRET ?? "" },
			});
			await this.ctx.storage.put(STARTED_IMAGE_KEY, container.images.base);
		}
		this.observeExit();
		await container.setInactivityTimeout(INACTIVITY_MINUTES * 60 * 1000);

		// `running` means startup was requested, not that the app is listening yet.
		// Python + librosa take a few seconds to import, so allow up to ~60 s.
		const port = container.getTcpPort(PORT);
		for (let attempt = 0; attempt < 120; attempt++) {
			try {
				const response = await port.fetch("http://container/api/config", {
					signal: AbortSignal.timeout(1000),
				});
				await response.body?.cancel();
				if (response.ok) return;
			} catch {
				// Not listening yet.
			}
			await scheduler.wait(500);
		}
		throw new Error(`Reel Maker did not start listening on port ${PORT}`);
	}

	/** Runs on the alarm: restart onto the new image once nothing would be lost. */
	async alarm(): Promise<void> {
		if (!(await this.isOutdated())) return;
		let busy: string[] = ["unknown"];
		try {
			const res = await this.ctx.container!.getTcpPort(PORT).fetch("http://container/api/internal/status", {
				headers: { "X-Reel-Proxy-Secret": this.env.REEL_PROXY_SECRET ?? "" },
				signal: AbortSignal.timeout(5000),
			});
			if (res.status === 404) {
				// An app from before this endpoint existed: we can't tell whether it's in
				// use, so don't restart it. Stop checking too, because the checks would
				// keep it awake; once it sleeps, its next start uses the new image.
				console.log("upgrade: running app predates the status check; letting it sleep");
				return;
			}
			if (res.ok) busy = ((await res.json()) as { busy: string[] }).busy;
		} catch (err) {
			console.warn("upgrade check: status unavailable", err);
		}
		if (busy.length) {
			console.log(`upgrade waiting; in use: ${busy.join(", ")}`);
			await this.ctx.storage.setAlarm(Date.now() + UPGRADE_CHECK_EVERY_MS);
			return;
		}
		console.log("upgrade: restarting the container onto the new image");
		// Visitors arriving meanwhile wait on the same startup promise.
		this.starting = this.restartOntoNewImage().finally(() => {
			this.starting = undefined;
		});
		await this.starting;
	}

	private async restartOntoNewImage(): Promise<void> {
		await this.ctx.container!.destroy();
		await this.monitoring?.catch(() => {});
		// Start right away so the next visitor finds it warm.
		await this.startAndWaitForPort();
	}

	/** True when a container is running an image other than the current deploy's. */
	private async isOutdated(): Promise<boolean> {
		const container = this.ctx.container;
		if (!container?.running) return false;
		// No record means it was started before this tracking existed: treat as old.
		const started = await this.ctx.storage.get<string>(STARTED_IMAGE_KEY);
		return started !== container.images.base;
	}

	/** Schedules the upgrade check if one is due; returns whether it did. */
	private async scheduleUpgradeCheck(delayMs: number): Promise<boolean> {
		if (!(await this.isOutdated())) return false;
		if ((await this.ctx.storage.getAlarm()) === null) {
			await this.ctx.storage.setAlarm(Date.now() + delayMs);
		}
		return true;
	}

	private observeExit(): void {
		if (this.monitoring) return;
		this.monitoring = this.ctx
			.container!.monitor()
			.then(() => console.log("Reel container exited"))
			.catch((error: unknown) => console.error("Reel container failed:", error))
			.finally(() => {
				this.monitoring = undefined;
			});
		this.ctx.waitUntil(this.monitoring);
	}
}

// The one canonical address; www.makemyreel.in redirects here.
const CANONICAL_HOST = "makemyreel.in";

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		const url = new URL(request.url);
		if (url.hostname === `www.${CANONICAL_HOST}`) {
			url.hostname = CANONICAL_HOST;
			return Response.redirect(url.toString(), 301);
		}

		const headers = new Headers(request.headers);
		// Never let visitors supply these themselves.
		for (const name of [...headers.keys()]) {
			if (name.toLowerCase().startsWith("x-reel-")) headers.delete(name);
		}
		// The app's per-visitor limits key on this; the secret proves it came from here.
		headers.set("X-Reel-Client-IP", request.headers.get("CF-Connecting-IP") ?? "unknown");
		headers.set("X-Reel-Proxy-Secret", env.REEL_PROXY_SECRET ?? "");

		const hasBody = request.method !== "GET" && request.method !== "HEAD";
		const forwarded = new Request(request.url, {
			method: request.method,
			headers,
			body: hasBody ? request.body : undefined,
			redirect: "manual",
		});
		try {
			const response = await env.REEL.getByName("main").fetch(forwarded);
			const out = new Response(response.body, response);
			out.headers.set("X-Content-Type-Options", "nosniff");
			out.headers.set("Referrer-Policy", "no-referrer");
			return out;
		} catch (err) {
			console.error("container unavailable", err);
			return unavailable(new URL(request.url).pathname.startsWith("/api/"));
		}
	},
} satisfies ExportedHandler<Env>;

function unavailable(api: boolean): Response {
	if (api) {
		return Response.json(
			{ detail: "Reel Maker is starting up or busy. Try again in a minute." },
			{ status: 503, headers: { "Retry-After": "30" } },
		);
	}
	const html = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Reel Maker is starting</title>
<meta http-equiv="refresh" content="20">
<style>
  :root { --bg:#f6f5fb; --text:#17161f; --muted:#6b6880; --card:#fff; --line:#e4e2ee; }
  @media (prefers-color-scheme: dark) { :root { --bg:#0f0f14; --text:#f3f2f8; --muted:#9c99b0; --card:#191923; --line:#2b2a39; } }
  body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--text);
    font:16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; padding:16px; box-sizing:border-box; }
  .card { max-width:420px; background:var(--card); border:1px solid var(--line); border-radius:16px; padding:28px; text-align:center; }
  h1 { font-size:20px; margin:0 0 8px; } p { color:var(--muted); margin:0; }
</style></head><body><div class="card"><h1>⏳ Reel Maker is starting</h1>
<p>It was asleep and is waking up. This page will refresh by itself in a few seconds.</p>
</div></body></html>`;
	return new Response(html, {
		status: 503,
		headers: { "Content-Type": "text/html; charset=utf-8", "Retry-After": "20", "Cache-Control": "no-store" },
	});
}
