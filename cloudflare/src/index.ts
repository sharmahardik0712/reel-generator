/**
 * Public front door for Reel Maker.
 *
 * reel.<you>.workers.dev  ->  this Worker  ->  Workers VPC  ->  Cloudflare Tunnel
 *   ->  `reel ui` on the home PC (127.0.0.1:8765)
 *
 * The Worker forwards every request unchanged, except that it:
 * - tells the app who the visitor is (X-Reel-Client-IP), signed with a shared secret
 *   so the app's per-visitor limits can't be dodged by sending a fake header;
 * - shows a friendly page when the home PC is off or the tunnel is down.
 */

interface Env {
	REEL_APP: Fetcher; // VPC Service binding (see wrangler.jsonc)
	REEL_PROXY_SECRET: string; // `wrangler secret put REEL_PROXY_SECRET`
}

// The VPC Service decides where requests go; this only sets the Host header.
const ORIGIN = "http://127.0.0.1:8765";

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		const url = new URL(request.url);
		const target = new URL(url.pathname + url.search, ORIGIN);

		const headers = new Headers(request.headers);
		// Never let visitors supply these themselves.
		for (const name of [...headers.keys()]) {
			if (name.toLowerCase().startsWith("x-reel-")) headers.delete(name);
		}
		headers.set("X-Reel-Client-IP", request.headers.get("CF-Connecting-IP") ?? "unknown");
		headers.set("X-Reel-Proxy-Secret", env.REEL_PROXY_SECRET ?? "");

		const hasBody = request.method !== "GET" && request.method !== "HEAD";
		try {
			const response = await env.REEL_APP.fetch(
				new Request(target, {
					method: request.method,
					headers,
					body: hasBody ? request.body : undefined,
					redirect: "manual",
				}),
			);
			const out = new Response(response.body, response);
			out.headers.set("X-Content-Type-Options", "nosniff");
			out.headers.set("Referrer-Policy", "no-referrer");
			return out;
		} catch (err) {
			console.error("origin unreachable", err);
			return offline(url.pathname.startsWith("/api/"));
		}
	},
} satisfies ExportedHandler<Env>;

function offline(api: boolean): Response {
	if (api) {
		return Response.json(
			{ detail: "Reel Maker is offline right now (the computer that makes reels is off). Try again later." },
			{ status: 503, headers: { "Retry-After": "300" } },
		);
	}
	const html = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Reel Maker is offline</title>
<style>
  :root { --bg:#f6f5fb; --text:#17161f; --muted:#6b6880; --card:#fff; --line:#e4e2ee; }
  @media (prefers-color-scheme: dark) { :root { --bg:#0f0f14; --text:#f3f2f8; --muted:#9c99b0; --card:#191923; --line:#2b2a39; } }
  body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--text);
    font:16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; padding:16px; box-sizing:border-box; }
  .card { max-width:420px; background:var(--card); border:1px solid var(--line); border-radius:16px; padding:28px; text-align:center; }
  h1 { font-size:20px; margin:0 0 8px; } p { color:var(--muted); margin:0; }
</style></head><body><div class="card"><h1>😴 Reel Maker is offline</h1>
<p>The computer that makes the reels is switched off or restarting. Please try again in a little while.</p>
</div></body></html>`;
	return new Response(html, {
		status: 503,
		headers: { "Content-Type": "text/html; charset=utf-8", "Retry-After": "300", "Cache-Control": "no-store" },
	});
}
