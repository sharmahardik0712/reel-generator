/**
 * Reel Maker on Cloudflare Containers.
 *
 * reel.<you>.workers.dev  ->  this Worker  ->  one ReelContainer (Durable Object)
 *   ->  the container running `reel ui` on port 8080 (Python + FFmpeg)
 *
 * There is exactly one container: uploads, the job queue and finished reels live on
 * its disk and in its memory. It sleeps after INACTIVITY_MINUTES without requests,
 * and sleeping wipes its disk, so that is set longer than the app's 20 minute reel TTL.
 */
import { DurableObject } from "cloudflare:workers";

const PORT = 8080;
// Reels stay downloadable for 20 minutes; keep the container awake a little longer.
const INACTIVITY_MINUTES = 30;
// 4 vCPU / 12 GiB / 20 GB disk: the largest predefined type, for fast renders.
const INSTANCE = "standard-4";

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
		// Share startup across concurrent requests, including after a failed attempt.
		this.starting ??= this.startAndWaitForPort().finally(() => {
			this.starting = undefined;
		});
		await this.starting;

		const url = new URL(request.url);
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

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
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
