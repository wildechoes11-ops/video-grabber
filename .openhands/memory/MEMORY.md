# Project memory

## Video grabber dashboard (workspace root)
- `app.py` + `index.html`: FastAPI dashboard on :12000 proxying ytdlp.online SSE + finished file (Range) for YouTube/live links.
- Run with the project venv: `PORT=12000 setsid nohup .venv/bin/python app.py > server.log 2>&1 &` (user site-packages gets wiped).
- Quota: anonymous limit is per exit IP, so `app.py` rotates proxies from `PROXY_DETAILS_URL`
  (default https://proxys-o6v0.onrender.com/proxy/details, env "" disables) up to `MAX_ATTEMPTS` (4).
- Secrets: `.ytdlp_cookie` (gitignored) = ytdlp.online cookies; `YTDLP_TOKEN` env = logged-in token.
- Upstream quirks: cf_clearance is IP/UA-bound but survives proxying; file link is `href="/api/v1/file/<t>/<n>.mp4"`
  (302 → `/download/...`, no proxy needed); outcomes arrive as `{"has_output":bool,"outcome":"success|quota_exceeded"}`.
- Details + reverse-engineering notes: `.openhands/memory/2026-11-22.md`.
