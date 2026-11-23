# Video Grabber — integration guide

Everything another agent needs to run, use, or re-deploy this service. Read this top to bottom
before changing code; the "Gotchas" section is where the surprises live.

---

## 1. What this is

A single-file FastAPI service that:

1. takes a YouTube (or other yt-dlp supported) video / live-stream URL,
2. asks a third-party service (`ytdlp.online`) to record it server-side,
3. polls that job until a file exists,
4. serves the finished MP4 with HTTP Range support,
5. ships a no-build HTML dashboard that does paste → preview → record → play.

The dashboard also embeds the *actual* YouTube live player when the pasted link is live, so the user
can watch while the recorder runs.

Two files matter: `app.py` (backend) and `index.html` (frontend, no dependencies, no build step).

```
Browser ──► app.py ──► ytdlp.online  /api/v1/stream   (SSE, does the recording)
   │            │  └─► ytdlp.online  /api/v1/live/analyze (metadata)
   │            ├────► proxy service /proxy/details     (only when quota needs a new exit IP)
   │            └────► ytdlp.online  /download/...      (finished file, proxied through app.py)
   └──────────── plays /api/video/{job_id}
```

---

## 2. Files

| File | Purpose |
|---|---|
| `app.py` | FastAPI app: job runner, SSE parsing, proxy rotation, file proxying, REST API |
| `index.html` | Dashboard UI, served at `/` |
| `requirements.txt` | `fastapi`, `uvicorn`, `requests` |
| `render.yaml` | Render blueprint (optional path to deploy) |
| `deploy_render.py` | Deploys/updates the service through the Render REST API |
| `INTEGRATION.md` | This file |

Not in the zip (never ship them): `.render_key`, `.gh_key`, `.ytdlp_cookie`, `.venv/`, `.git/`.

---

## 3. Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
PORT=12000 python app.py          # or: uvicorn app:app --host 0.0.0.0 --port 12000
```

Then open `http://localhost:12000/`.

Health check: `curl localhost:12000/healthz` → `{"ok":true,"running":0,"limit":10}`

The app also runs fine under a platform that injects `$PORT` (Render, Heroku, Fly).

---

## 4. Environment variables

| Var | Default | Meaning |
|---|---|---|
| `PORT` | `8000` | Bind port |
| `MAX_CONCURRENT` | `10` | Simultaneous recordings; extra jobs queue |
| `PROXY_DETAILS_URL` | `https://proxys-o6v0.onrender.com/proxy/details` | Returns a fresh residential proxy as JSON; set to empty string to disable |
| `PROXY_FIRST` | unset | `1` = use a proxy on the first attempt instead of direct |
| `YTDLP_COOKIE` | unset | `cf_clearance` cookie for `ytdlp.online`. Falls back to a `.ytdlp_cookie` file next to `app.py` |
| `MAX_ATTEMPTS` | `4` | Retries per job (each retry after the first gets a new exit IP) |

The cookie is the only credential this service needs, and it expires (Cloudflare rotates
`cf_clearance`). Symptom when stale: upstream returns a 403 / Cloudflare HTML instead of SSE.

---

## 5. REST API

Base URL is whatever host the app runs on. `GET /api` returns a machine-readable index of all of these.

### `POST /api/start`

```json
{"url": "https://www.youtube.com/live/QZiMlUz-138", "max_minutes": 15}
```

`max_minutes` is clamped to 1..60 and only limits how long a live stream is recorded.

Response `200`: `{"job_id": "a654e1eb-..."}`

Errors: `400` for a malformed URL (must be plain `http(s)://`, no shell metacharacters).

Returns immediately; the recording runs in a background thread.

### `GET /api/status/{job_id}`

```json
{
  "id": "a654e1eb-...",
  "status": "downloading",
  "queue_position": 0,
  "concurrency_limit": 10,
  "percent": 42.5,
  "size": "18.3MiB",
  "speed": "2.1MiB/s",
  "elapsed": 61,
  "seconds": 61,
  "filename": null,
  "error": null,
  "has_video": false,
  "lines": ["attempt 1/4 via direct", "Running command: ..."]
}
```

`status` is one of: `queued` → `starting` → `downloading` → `ready`, plus terminal `failed`
and `stopped`. Poll every 1–2 s. `queue_position` is 1-based and only non-zero while `queued`.
`lines` is the last 120 log lines (HTML already stripped) — surface them in any UI you build.

### `GET /api/video/{job_id}`

The finished MP4, streamed from upstream **with Range support** (browsers need 206 to seek).
`404` until `has_video` is true. This is what the `<video>` tag points at.

### `GET /api/download/{job_id}`

Same bytes, `Content-Disposition: attachment` (forces a save dialog).

### `POST /api/stop/{job_id}`

Cancels an in-flight recording. The partial file upstream is discarded; job ends as `stopped`.

### `GET /api/analyze?url=...`

Metadata preview, no recording. Response:

```json
{
  "video_id": "53SxFKpbKDE",
  "title": "Watch Sky News live 2026-11-22 18:31",
  "channel": "Sky News",
  "is_live": true,
  "qualities": [1080, 720, 480, 360, 240, 144],
  "thumbnail": "https://i.ytimg.com/vi/53SxFKpbKDE/maxresdefault.jpg",
  "duration": null,
  "viewer_count": 4154
}
```

`video_id` is derived locally from the URL (`watch?v=`, `/live/`, `/shorts/`, `youtu.be/`, `/embed/`).

### `GET /api/stats`

`{"running": 2, "queued": 1, "limit": 10, "jobs": 7}` — use it for capacity checks before bulk work.

### `GET /api/proxy`

`{"available": true, "country": "IN", "expires": "2026-11-22T18:27:10", "error": null}`
Fetches (and caches) one proxy so you can see what the exit IP currently is.

### `GET /healthz`

`{"ok": true, "running": 0, "limit": 10}` — point platform health checks here.

### `GET /video-grabber.zip`

Self-download of the source bundle (allowlisted files only, built on request).

---

## 6. How recording works upstream

`app.py` speaks to `https://ytdlp.online` with these calls:

1. **Analyze** — `POST /api/v1/live/analyze` with `{"url": ..., "cf_token": ""}` and the
   `cf_clearance` cookie. Returns the metadata above.
2. **Stream** — `GET /api/v1/stream` with query params:

   | Param | Value |
   |---|---|
   | `command` | `-f "bv*[height<=1080]+ba/b[height<=1080]/best" --merge-output-format mp4 "<URL>"` |
   | `job_id` | the job id we generated (also upstream's id) |
   | `lang_code` | `en` |
   | `source` | `lp` |
   | `max_minutes` | 1..60 |
   | `live_stop` | `1` — stop at the live edge instead of recording forever |
   | `live_from_now` | `1` — start from now, not from the beginning of the DVR window |

   The response is `text/event-stream`. Lines carry yt-dlp log text and progress
   (`[download]  42.5% of ~18.3MiB at 2.1MiB/s`), which we parse for percent/size/speed.
3. **File** — the finished file appears in the stream as an anchor:
   `href="/api/v1/file/<token>/<name>.mp4"`. Fetching it 302s to `/download/<token>/<name>.mp4`.
   We store that path and proxy it for `/api/video/{id}`.

Regexes that matter (copy them exactly, they are easy to get subtly wrong):

```python
FILE_RE = re.compile(r'href="(/(?:api/v1/file|download)/[^"]+)"')   # only href="..." — see Gotchas
PCT_RE  = re.compile(r"(\d+(?:\.\d+)?)%")
SIZE_RE = re.compile(r"of\s+~?\s*([\d.]+\s*[KMG]iB)")
```

---

## 7. Quota, proxies, and why they exist

`ytdlp.online` enforces a **daily recording quota per exit IP**. Anonymous use hits
`Daily recording limit reached` quickly.

Observed behaviour, worth knowing before you "simplify" this away:

- The quota follows the **IP**, not the cookie. The same `cf_clearance` cookie run through
  different proxy IPs each got a fresh allowance.
- `cf_clearance` is nominally IP-bound, but in practice it kept working through rotating
  residential proxies, so we do not re-mint it per IP.
- Rotation policy (current, user-approved): **attempt 1 goes direct**, attempts 2–4 pull a fresh
  proxy from `PROXY_DETAILS_URL` and retry. Set `PROXY_FIRST=1` to proxy from the start.
- The proxy endpoint returns
  `{"proxy_ip": ..., "proxy_port": ..., "proxy_user": ..., "proxy_pass": ..., "country": ..., "expires": ...}`
  with roughly 20–25 minute lifetimes. Some exit IPs are dead on arrival — the retry loop covers that.
- That proxy service is itself a sleeping free-tier app: the first call can take ~50 s. The
  fetch timeout is 75 s for this reason, with a stale-cache fallback.

If you disable proxies (`PROXY_DETAILS_URL=""`), everything still works until the host's IP
quota is exhausted, then every job fails with a quota error. That is a deliberate trade, not a bug.

---

## 8. Concurrency model

- A `threading.BoundedSemaphore(MAX_CONCURRENT)` gates recordings; a job that cannot acquire it
  reports `queued` with a `queue_position` instead of failing.
- Each recording is one background thread; there is no worker queue and no external broker.
- Jobs live in a dict in process memory, mirrored to `.jobs.json` on disk. **A restart loses
  history** (the upstream file still exists until it expires).
- Ten concurrent recordings on a 512 MB instance is fine: the heavy lifting is remote, this
  process only relays bytes.

---

## 9. Deploy to Render (REST API)

`deploy_render.py` wraps this; the raw calls are below so you can do it without the script.

**Auth header:** `Authorization: Bearer $RENDER_API_KEY`, `accept: application/json`.

1. Find your workspace:

```bash
curl -s -H "Authorization: Bearer $RENDER_API_KEY" https://api.render.com/v1/owners
# -> [{"owner": {"id": "tea-...", "name": "..."}}]
```

2. Create the service (the code must already be in a Git repo Render can clone):

```bash
curl -s -X POST https://api.render.com/v1/services \
  -H "Authorization: Bearer $RENDER_API_KEY" \
  -H 'content-type: application/json' \
  -d '{
    "type": "web_service",
    "name": "video-grabber",
    "ownerId": "tea-...",
    "repo": "https://github.com/<you>/video-grabber",
    "branch": "main",
    "autoDeploy": "yes",
    "serviceDetails": {
      "env": "python",
      "plan": "free",
      "region": "singapore",
      "healthCheckPath": "/healthz",
      "envSpecificDetails": {
        "buildCommand": "pip install -r requirements.txt",
        "startCommand": "uvicorn app:app --host 0.0.0.0 --port $PORT"
      },
      "envVars": [
        {"key": "MAX_CONCURRENT", "value": "10"},
        {"key": "PROXY_DETAILS_URL", "value": "https://proxys-o6v0.onrender.com/proxy/details"},
        {"key": "YTDLP_COOKIE", "value": "<cf_clearance>"}
      ]
    }
  }'
```

`envSpecificDetails` is **required** for non-static, non-Docker services; putting
`buildCommand`/`startCommand` at the top level of `serviceDetails` returns
`400 serviceDetails must include envSpecificDetails`.

3. Poll the deploy:

```bash
curl -s -H "Authorization: Bearer $RENDER_API_KEY" \
  "https://api.render.com/v1/services/$SERVICE_ID/deploys?limit=1"
# deploy.status: created -> build_in_progress -> update_in_progress -> live
```

4. Update env vars later (e.g. a refreshed cookie) and redeploy:

```bash
curl -s -X PUT "https://api.render.com/v1/services/$SERVICE_ID/env-vars" \
  -H "Authorization: Bearer $RENDER_API_KEY" -H 'content-type: application/json' \
  -d '[{"key":"YTDLP_COOKIE","value":"<new cookie>"}]'

curl -s -X POST "https://api.render.com/v1/services/$SERVICE_ID/deploys" \
  -H "Authorization: Bearer $RENDER_API_KEY" -H 'content-type: application/json' -d '{"clearCache":"do_not_clear"}'
```

5. Logs:

```bash
curl -s -H "Authorization: Bearer $RENDER_API_KEY" \
  "https://api.render.com/v1/logs?ownerId=$OWNER_ID&resourceId=$SERVICE_ID&limit=100"
```

Using the script instead:

```bash
RENDER_API_KEY=rnd_... python deploy_render.py \
  --repo https://github.com/<you>/video-grabber \
  --cookie-file .ytdlp_cookie
```

It is idempotent: if a service with that name exists it updates env vars and triggers a deploy
instead of creating a duplicate, then prints the URL when the deploy reports `live`.

---

## 10. Testing checklist

```bash
BASE=https://your-service.onrender.com

curl -s $BASE/healthz                                   # ok:true
curl -s "$BASE/api/analyze?url=https://www.youtube.com/@SkyNews/live"   # is_live:true, video_id set
JOB=$(curl -s -X POST $BASE/api/start -H 'content-type: application/json' \
        -d '{"url":"https://www.youtube.com/watch?v=jNQXAC9IVRw","max_minutes":1}' \
      | python -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')
curl -s $BASE/api/status/$JOB                           # watch it reach "ready"
curl -s -o /dev/null -D - -r 0-2047 $BASE/api/video/$JOB | grep -iE '^HTTP/|content-range'  # 206
```

Concurrency check: start 4 jobs, then `curl $BASE/api/stats` → `running: 4`. Each job's first log
line should read `attempt 1/4 via direct`; a quota failure makes it rotate
(`attempt 2/4 via proxy XX`).

---

## 11. Gotchas (read before editing)

1. **Match only `href="..."`** when scraping the file link. The yt-dlp log also prints
   `Destination: /download/...`, and a loose regex grabs that path instead of the real anchor.
2. **Playback needs no proxy.** Only the `ytdlp.online` calls do; `/download/...` is fetched directly.
   Keep it that way or you add a pointless failure mode.
3. **Cookie expires.** Refresh `YTDLP_COOKIE` when jobs start failing immediately with an HTML body.
4. **Free tier sleeps** after ~15 min idle; the next request takes ~50 s. Health checkers that
   time out at 30 s will flap — that is the plan, not the code.
5. **Disk is ephemeral.** `.jobs.json` and any temp files vanish on redeploy.
6. **No authentication.** Anyone with the URL can spend the quota. Add an `X-API-Key` check before
   exposing it publicly (an env-var-driven no-op when unset is the least disruptive shape).
7. **YouTube embeds can bot-wall datacenter IPs** ("Sign in to confirm you're not a bot"). The
   dashboard shows the live player optimistically and offers an "open on YouTube" link; do not
   treat that wall as a frontend bug.
8. **`max_minutes` is the recording cap, not a timeout.** A 30-minute cap on a 2-minute video still
   finishes in seconds because `live_stop=1` ends at the live edge.

---

## 12. Integrating from another app

Minimal JS client:

```js
const BASE = "https://your-service.onrender.com";

async function grab(url, maxMinutes = 15) {
  const { job_id } = await (await fetch(`${BASE}/api/start`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ url, max_minutes: maxMinutes }),
  })).json();

  for (;;) {
    const st = await (await fetch(`${BASE}/api/status/${job_id}`)).json();
    if (st.status === "ready") return `${BASE}/api/video/${job_id}`;
    if (st.status === "failed" || st.status === "stopped") throw new Error(st.error || st.status);
    await new Promise((r) => setTimeout(r, 1500));
  }
}

// <video src={await grab(link)} controls />
```

If you need to watch the live feed immediately, embed
`https://www.youtube.com/embed/<video_id>?autoplay=1&mute=1` using `video_id` from `/api/analyze`
— that plays the real feed without waiting for the recording to finish.

Server-to-server callers should treat `/api/video/{id}` as a normal URL and proxy it if they need
to add auth; it already speaks Range so CDN/`<video>` behaviour is correct.
