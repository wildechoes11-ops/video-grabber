"""Minimal dashboard: paste a video link, watch the download, play the result."""

import html
import io
import json
import os
import re
import threading
import time
import uuid
import zipfile
from datetime import datetime
from collections import deque
from urllib.parse import quote, unquote
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

UPSTREAM = "https://ytdlp.online"
HERE = Path(__file__).parent
COOKIE_FILE = HERE / ".ytdlp_cookie"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "accept-language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    "referer": f"{UPSTREAM}/live-stream-downloader",
    "user-agent": UA,
}

MAX_CONCURRENT = max(1, int(os.environ.get("MAX_CONCURRENT", "10")))

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)
RUNNING = {"count": 0}
RUNNING_LOCK = threading.Lock()
JOBS_FILE = HERE / ".jobs.json"

PERCENT_RE = re.compile(r"\[download\]\s+([\d.]+)%")
FFMPEG_RE = re.compile(r"size=\s*(\d+)\s*KiB.*?time=(\S+).*?(?:bitrate=\s*(\S+))?")
SIZE_RE = re.compile(r"of\s+~?\s*([\d.]+\s*[KMG]iB)")
FILE_RE = re.compile(r'href="(/(?:api/v1/file|download)/[^"]+)"')
URL_RE = re.compile(r"^https?://[^\s\"'`;&|$<>\\]+$")
YT_ID_RE = re.compile(r"(?:v=|/live/|/shorts/|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})")
DEST_RE = re.compile(r"\[download\] Destination: (.+)$")


def youtube_id(*urls: str | None) -> str | None:
    for url in urls:
        match = YT_ID_RE.search(url or "")
        if match:
            return match.group(1)
    return None


def load_cookie() -> str:
    cookie = os.environ.get("YTDLP_COOKIE") or (
        COOKIE_FILE.read_text().strip() if COOKIE_FILE.exists() else ""
    )
    token = os.environ.get("YTDLP_TOKEN", "").strip()
    return f"{cookie}; token={token}" if token else cookie


def save_jobs() -> None:
    keep = {
        job_id: {k: job.get(k) for k in ("status", "file_path", "filename", "started", "upstream_job")}
        for job_id, job in JOBS.items()
        if job.get("file_path")
    }
    try:
        JOBS_FILE.write_text(json.dumps(keep))
    except OSError:
        pass


def load_jobs() -> None:
    if not JOBS_FILE.exists():
        return
    try:
        saved = json.loads(JOBS_FILE.read_text())
    except (OSError, ValueError):
        return
    for job_id, data in saved.items():
        JOBS[job_id] = {
            "id": job_id,
            "lines": deque(maxlen=300),
            "percent": 100.0,
            "size": None,
            "speed": None,
            "elapsed": None,
            "error": None,
            "stop": False,
            **data,
        }


def new_job() -> str:
    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "starting",
            "lines": deque(maxlen=300),
            "percent": None,
            "size": None,
            "speed": None,
            "elapsed": None,
            "filename": None,
            "file_path": None,
            "error": None,
            "started": time.time(),
            "stop": False,
            "upstream_job": job_id,
        }
    return job_id


TAG_RE = re.compile(r"<[^>]+>")


def log(job: dict, text: str) -> None:
    job["lines"].append(TAG_RE.sub("", html.unescape(text)).strip())


PROXY_DETAILS_URL = os.environ.get(
    "PROXY_DETAILS_URL", "https://proxys-o6v0.onrender.com/proxy/details"
)
MAX_ATTEMPTS = 4
PROXY_FIRST = os.environ.get("PROXY_FIRST", "") not in ("", "0", "false")
_proxy: dict = {"url": None, "country": None, "expires": 0.0, "error": None}


def get_proxy(force: bool = False) -> str | None:
    """Rotating exit IP: the per-IP daily quota is what we are working around."""
    if not PROXY_DETAILS_URL:
        return None
    if not force and _proxy["url"] and _proxy["expires"] > time.time() + 20:
        return _proxy["url"]
    try:
        data = requests.get(PROXY_DETAILS_URL, timeout=75).json()
        user = quote(str(data["proxy_user"]), safe="")
        password = quote(str(data["proxy_pass"]), safe="")
        url = f"http://{user}:{password}@{data['proxy_ip']}:{data['proxy_port']}"
        expires = datetime.strptime(data["expires"], "%Y-%m-%d %H:%M:%S").timestamp()
        _proxy.update(
            url=url,
            country=data.get("country"),
            expires=min(expires, time.time() + 1500),
        )
        _proxy["error"] = None
        return url
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        _proxy["error"] = f"{type(exc).__name__}: {exc}"[:200]
        if _proxy["url"] and _proxy["expires"] > time.time() + 20:
            return _proxy["url"]
        return None


def stream_once(job: dict, url: str, max_minutes: int, proxy: str | None) -> str:
    """One upstream attempt. Returns ok | quota | retry | stopped."""
    command = (
        f'-f "bv*[height<=1080]+ba/b[height<=1080]/best" '
        f'--merge-output-format mp4 "{url}"'
    )
    upstream_job = str(uuid.uuid4())
    job["upstream_job"] = upstream_job
    params = {
        "command": command,
        "job_id": upstream_job,
        "lang_code": "en",
        "source": "lp",
        "max_minutes": str(max_minutes),
        "live_stop": "1",
        "live_from_now": "1",
    }
    headers = {
        **BASE_HEADERS,
        "accept": "text/event-stream",
        "cache-control": "no-cache",
        "cookie": load_cookie(),
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        with requests.get(
            f"{UPSTREAM}/api/v1/stream",
            params=params,
            headers=headers,
            proxies=proxies,
            stream=True,
            timeout=(25, 180),
        ) as resp:
            if resp.status_code != 200:
                log(job, f"upstream HTTP {resp.status_code}")
                return "retry"
            event = ""
            for raw in resp.iter_lines(decode_unicode=True):
                if job["stop"]:
                    return "stopped"
                if raw is None:
                    continue
                line = raw.strip()
                if not line:
                    event = ""
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()

                if event == "live-stop-ready":
                    job["lines"].append("live stream detected — stop control ready")
                    continue
                if data in ("", "{}"):
                    continue

                text = html.unescape(data)
                log(job, text)

                dest = DEST_RE.search(text)
                if dest:
                    job["filename"] = dest.group(1).strip().split("/")[-1]

                m = PERCENT_RE.search(text)
                if m:
                    job["percent"] = float(m.group(1))
                fm = FFMPEG_RE.search(text)
                if fm:
                    job["size"] = f"{fm.group(1)} KiB"
                    job["elapsed"] = fm.group(2)
                    job["speed"] = fm.group(3)
                sm = SIZE_RE.search(text)
                if sm:
                    job["size"] = sm.group(1)

                fmatch = FILE_RE.search(data)
                if fmatch:
                    path = fmatch.group(1)
                    if path.startswith("/api/v1/file/"):
                        path = "/download/" + path[len("/api/v1/file/"):]
                    job["file_path"] = path
                    job["filename"] = unquote(path.rsplit("/", 1)[-1])
                    job["percent"] = 100.0
                    log(job, "file ready")
                    return "ok"

                if '"outcome"' in text:
                    try:
                        payload = json.loads(data)
                    except ValueError:
                        payload = {}
                    outcome = payload.get("outcome")
                    if outcome == "success" and payload.get("has_output"):
                        return "ok"
                    if outcome == "quota_exceeded":
                        return "quota"
                    if outcome:
                        job["error"] = f"upstream reported: {outcome}"
                        return "retry"
    except requests.RequestException as exc:
        log(job, f"request error: {exc}")
        job["error"] = str(exc)
        return "retry"
    return "stopped" if job["stop"] else "retry"


def run_job(job: dict, url: str, max_minutes: int) -> None:
    job["status"] = "queued"
    SLOTS.acquire()
    with RUNNING_LOCK:
        RUNNING["count"] += 1
    try:
        _run_attempts(job, url, max_minutes)
    finally:
        with RUNNING_LOCK:
            RUNNING["count"] -= 1
        SLOTS.release()


def _run_attempts(job: dict, url: str, max_minutes: int) -> None:
    job["status"] = "downloading"
    outcome = "retry"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if job["stop"]:
            outcome = "stopped"
            break
        job["attempt"] = attempt
        # Direct first (fast, no proxy hop); rotate to a fresh exit IP only if that
        # IP's quota is spent. PROXY_FIRST=1 forces the proxy from the start.
        proxy = get_proxy(force=True) if (attempt > 1 or PROXY_FIRST) else None
        where = f"proxy {_proxy['country']}" if proxy else "direct"
        log(job, f"attempt {attempt}/{MAX_ATTEMPTS} via {where}")
        outcome = stream_once(job, url, max_minutes, proxy)
        if outcome in ("ok", "stopped"):
            break
        if outcome == "quota":
            job["error"] = "daily recording limit on that exit IP — rotating"
            log(job, job["error"])
        job["percent"] = None

    if outcome == "ok" and job["file_path"]:
        job["status"] = "ready"
        job["error"] = None
        save_jobs()
    elif job["stop"]:
        job["status"] = "stopped"
    else:
        job["status"] = "failed"
        job["error"] = job["error"] or "download failed after retries"
    job["ended"] = time.time()


load_jobs()
app = FastAPI(title="Video Grabber")


@app.get("/healthz")
def healthz():
    with RUNNING_LOCK:
        running = RUNNING["count"]
    return {"ok": True, "running": running, "limit": MAX_CONCURRENT}


@app.get("/api")
def api_index():
    return {
        "service": "video-grabber",
        "concurrency_limit": MAX_CONCURRENT,
        "endpoints": {
            "POST /api/start": 'body {"url": str, "max_minutes": int} -> {"job_id": str}',
            "GET /api/status/{job_id}": "status, percent, size, speed, queue_position, lines[], has_video",
            "GET /api/video/{job_id}": "video/mp4 with Range support (for <video>)",
            "GET /api/download/{job_id}": "same bytes as an attachment",
            "POST /api/stop/{job_id}": "stop recording early",
            "GET /api/analyze?url=": "title, channel, is_live, qualities, thumbnail",
            "GET /api/stats": "running / queued / limit",
            "GET /healthz": "liveness",
        },
    }


@app.get("/api/proxy")
def proxy_status():
    url = get_proxy()
    return {
        "available": bool(url),
        "country": _proxy["country"],
        "expires": datetime.fromtimestamp(_proxy["expires"]).isoformat() if url else None,
        "error": _proxy["error"],
    }


@app.get("/api/stats")
def stats():
    with RUNNING_LOCK:
        running = RUNNING["count"]
    with JOBS_LOCK:
        queued = sum(1 for job in JOBS.values() if job["status"] == "queued")
        total = len(JOBS)
    return {"running": running, "queued": queued, "limit": MAX_CONCURRENT, "jobs": total}


@app.get("/")
def index():
    return FileResponse(HERE / "index.html", media_type="text/html")


BUNDLE_FILES = (
    "app.py",
    "index.html",
    "requirements.txt",
    "render.yaml",
    "deploy_render.py",
    "INTEGRATION.md",
    ".gitignore",
)


@app.get("/video-grabber.zip")
def bundle():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in BUNDLE_FILES:
            path = HERE / name
            if path.exists():
                archive.write(path, name)
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"content-disposition": 'attachment; filename="video-grabber.zip"'},
    )


@app.post("/api/start")
async def start(request: Request):
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url is required")
    if not URL_RE.match(url):
        raise HTTPException(400, "that does not look like a plain http(s) video link")
    try:
        max_minutes = max(1, min(int(body.get("max_minutes") or 15), 60))
    except (TypeError, ValueError):
        max_minutes = 15
    job_id = new_job()
    job = JOBS[job_id]
    threading.Thread(
        target=run_job, args=(job, url, max_minutes), daemon=True
    ).start()
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
def status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    now = time.time()
    queued_ahead = 0
    if job["status"] == "queued":
        with JOBS_LOCK:
            queued_ahead = sum(
                1
                for other in JOBS.values()
                if other["status"] == "queued" and other["started"] < job["started"]
            )
    return {
        "id": job["id"],
        "queue_position": queued_ahead + 1 if job["status"] == "queued" else 0,
        "concurrency_limit": MAX_CONCURRENT,
        "status": job["status"],
        "percent": job["percent"],
        "size": job["size"],
        "speed": job["speed"],
        "elapsed": job["elapsed"],
        "filename": job["filename"],
        "error": job["error"],
        "seconds": round(now - job["started"]),
        "has_video": bool(job["file_path"]),
        "lines": list(job["lines"])[-120:],
    }


@app.get("/api/analyze")
def analyze(url: str):
    if not URL_RE.match(url):
        raise HTTPException(400, "that does not look like a plain http(s) video link")
    try:
        resp = requests.post(
            f"{UPSTREAM}/api/v1/live/analyze",
            json={"url": url, "cf_token": ""},
            headers={**BASE_HEADERS, "cookie": load_cookie()},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise HTTPException(502, f"could not reach the analyzer: {exc}") from exc
    if resp.status_code != 200:
        raise HTTPException(502, f"analyzer returned HTTP {resp.status_code}")
    data = resp.json()
    return {
        "video_id": youtube_id(data.get("webpage_url"), url),
        **{
            key: data.get(key)
            for key in ("title", "channel", "is_live", "qualities", "thumbnail", "duration", "viewer_count")
        },
    }


@app.post("/api/stop/{job_id}")
def stop(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    job["stop"] = True
    for path in ("/api/v1/live/stop", "/api/v1/stream-cancel"):
        try:
            requests.post(
                UPSTREAM + path,
                json={"job_id": job["upstream_job"], "token": ""},
                headers={**BASE_HEADERS, "cookie": load_cookie()},
                timeout=8,
            )
        except requests.RequestException:
            pass
    return {"ok": True}


def upstream_file(job: dict, range_header: str | None) -> requests.Response:
    headers = {**BASE_HEADERS, "cookie": load_cookie()}
    if range_header:
        headers["range"] = range_header
    return requests.get(
        UPSTREAM + job["file_path"], headers=headers, stream=True, timeout=(20, 300)
    )


@app.get("/api/video/{job_id}")
def video(job_id: str, request: Request):
    job = JOBS.get(job_id)
    if not job or not job["file_path"]:
        raise HTTPException(404, "no video for this job")
    upstream = upstream_file(job, request.headers.get("range"))
    if upstream.status_code >= 400:
        upstream.close()
        raise HTTPException(upstream.status_code, "upstream rejected the file request")

    passthrough = {}
    for key in ("content-length", "content-range", "accept-ranges", "etag", "last-modified"):
        if key in upstream.headers:
            passthrough[key] = upstream.headers[key]

    def body():
        try:
            for chunk in upstream.iter_content(chunk_size=1 << 16):
                yield chunk
        finally:
            upstream.close()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "video/mp4"),
        headers=passthrough,
    )


@app.get("/api/download/{job_id}")
def download(job_id: str, request: Request):
    job = JOBS.get(job_id)
    if not job or not job["file_path"]:
        raise HTTPException(404, "no file for this job")
    upstream = upstream_file(job, request.headers.get("range"))
    if upstream.status_code >= 400:
        upstream.close()
        raise HTTPException(upstream.status_code, "upstream rejected the file request")
    name = job["filename"] or "video.mp4"
    passthrough = {"content-disposition": f'attachment; filename="{name}"'}
    for key in ("content-length", "content-range", "accept-ranges"):
        if key in upstream.headers:
            passthrough[key] = upstream.headers[key]

    def body():
        try:
            for chunk in upstream.iter_content(chunk_size=1 << 16):
                yield chunk
        finally:
            upstream.close()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        headers=passthrough,
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 12000)))
