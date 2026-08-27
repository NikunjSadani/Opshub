/**
 * Cloudflare Worker — opshub.gifsy.in  →  Cloud Run (opshub-api, asia-south1)
 *
 * WHY THIS EXISTS
 *   Cloud Run custom-domain *mapping* is not available in asia-south1 (Google returns
 *   501 UNIMPLEMENTED). The gifsy.in zone already runs a loyalty worker on the WILDCARD
 *   route `*.gifsy.in/*`, which would otherwise catch opshub.gifsy.in and send it to the
 *   LOYALTY frontend. This dedicated worker binds the MORE-SPECIFIC route
 *   `opshub.gifsy.in/*` (a specific host beats a wildcard), so opshub.gifsy.in is served
 *   by THIS worker and forwarded to the OpsHub origin — fully decoupled from loyalty.
 *
 * WHAT IT DOES
 *   OpsHub is a SINGLE Cloud Run service that serves the SPA and the API on one origin
 *   (no /api prefix, no tenant routing), so this is a plain reverse proxy: forward every
 *   path+query to the origin, let fetch() set Host to the origin host (which is how Cloud
 *   Run's host-based routing accepts the request), and rewrite any Location header that
 *   points back at *.run.app so the browser stays on opshub.gifsy.in.
 *
 * DEPLOY (one-time, from this directory, with the gifsy.in Cloudflare account):
 *   1. npx wrangler login          # browser OAuth into the Cloudflare account that owns gifsy.in
 *   2. npx wrangler deploy         # binds route opshub.gifsy.in/* to this worker
 *   3. In the Cloudflare dashboard, ensure a PROXIED (orange-cloud) DNS record for
 *      `opshub` exists in the gifsy.in zone (CNAME opshub -> opshub-api-2aadkzwkua-el.a.run.app,
 *      Proxied). The existing *.gifsy.in wildcard already gives DNS + edge TLS, but an
 *      explicit proxied record is clearer and does not depend on the wildcard staying.
 */

// The OpsHub Cloud Run service origin (stable across revisions — the run.app URL, not a
// per-revision URL). Do not point this at a bare project-number URL; use the canonical
// service URL from `gcloud run services describe opshub-api --format='value(status.url)'`.
const OPSHUB_ORIGIN = 'https://opshub-api-2aadkzwkua-el.a.run.app'

export default {
  async fetch(request) {
    const url = new URL(request.url)
    const publicHost = url.hostname

    // Preserve path + query; fetch() will set Host to the origin's host so Cloud Run accepts it.
    const originUrl = new URL(url.pathname + url.search, OPSHUB_ORIGIN)

    const headers = new Headers(request.headers)
    headers.set('x-forwarded-host', publicHost)
    headers.set('x-forwarded-proto', url.protocol.replace(':', ''))
    // Cloud Run doesn't need Cloudflare's edge headers.
    headers.delete('cf-connecting-ip')
    headers.delete('cf-ipcountry')
    headers.delete('cf-ray')
    headers.delete('cf-visitor')

    const originRequest = new Request(originUrl.toString(), {
      method: request.method,
      headers,
      body: ['GET', 'HEAD'].includes(request.method) ? undefined : request.body,
      redirect: 'manual', // handle redirects ourselves so we can rewrite Location
    })

    const response = await fetch(originRequest)

    // Keep the browser on opshub.gifsy.in: rewrite any Location that resolves to *.run.app.
    // A relative Location (e.g. "/login") resolves against the origin, so its host also ends
    // with .run.app and is rewritten to the public host — harmless, the browser would resolve
    // a relative redirect against the public host anyway.
    const responseHeaders = new Headers(response.headers)
    const location = responseHeaders.get('location')
    if (location) {
      try {
        const loc = new URL(location, OPSHUB_ORIGIN)
        if (loc.hostname.endsWith('.run.app')) {
          loc.hostname = publicHost
          loc.protocol = 'https:'
          responseHeaders.set('location', loc.toString())
        }
      } catch {
        // unparseable Location — leave as-is
      }
    }

    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: responseHeaders,
    })
  },
}
