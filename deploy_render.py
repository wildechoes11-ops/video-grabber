#!/usr/bin/env python3
"""Create or update this app's Render web service through the Render REST API.

    RENDER_API_KEY=rnd_... python deploy_render.py \
        --repo https://github.com/<you>/video-grabber --cookie-file .ytdlp_cookie

Idempotent: an existing service with the same name gets its env vars updated and a
deploy triggered instead of a duplicate being created.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

import requests

API = "https://api.render.com/v1"
HERE = pathlib.Path(__file__).resolve().parent


def api_key(explicit: str | None) -> str:
    key = explicit or os.environ.get("RENDER_API_KEY") or os.environ.get("RENDER_KEY")
    if not key and (HERE / ".render_key").exists():
        key = (HERE / ".render_key").read_text().strip()
    if not key:
        sys.exit("no Render API key: pass --api-key, set RENDER_API_KEY, or create .render_key")
    return key


def call(method: str, path: str, key: str, **kwargs):
    resp = requests.request(
        method, f"{API}{path}", headers={"Authorization": f"Bearer {key}"}, timeout=90, **kwargs
    )
    if resp.status_code >= 300:
        sys.exit(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:400]}")
    return resp.json() if resp.content else None


def owner_id(key: str, wanted: str | None) -> str:
    owners = call("GET", "/owners", key) or []
    if not owners:
        sys.exit("this API key has no workspace")
    for entry in owners:
        owner = entry["owner"]
        if wanted and owner["name"] == wanted:
            return owner["id"]
    return owners[0]["owner"]["id"]


def find_service(key: str, name: str) -> dict | None:
    for entry in call("GET", "/services?limit=100", key) or []:
        service = entry["service"]
        if service["name"] == name:
            return service
    return None


def env_vars(args) -> list[dict]:
    values = {
        "MAX_CONCURRENT": str(args.max_concurrent),
        "PROXY_DETAILS_URL": args.proxy_url,
    }
    if args.proxy_first:
        values["PROXY_FIRST"] = "1"
    cookie = ""
    if args.cookie:
        cookie = args.cookie
    elif args.cookie_file and pathlib.Path(args.cookie_file).exists():
        cookie = pathlib.Path(args.cookie_file).read_text().strip()
    if cookie:
        values["YTDLP_COOKIE"] = cookie
    for pair in args.env or []:
        name, _, value = pair.partition("=")
        values[name] = value
    return [{"key": k, "value": v} for k, v in values.items()]


def service_payload(args, owner: str, vars_: list[dict]) -> dict:
    return {
        "type": "web_service",
        "name": args.name,
        "ownerId": owner,
        "repo": args.repo,
        "branch": args.branch,
        "autoDeploy": "yes",
        "serviceDetails": {
            "env": "python",
            "plan": args.plan,
            "region": args.region,
            "healthCheckPath": "/healthz",
            "envSpecificDetails": {
                "buildCommand": "pip install -r requirements.txt",
                "startCommand": "uvicorn app:app --host 0.0.0.0 --port $PORT",
            },
            "envVars": vars_,
        },
    }


def wait_for_live(key: str, service_id: str, timeout: int) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        deploys = call("GET", f"/services/{service_id}/deploys?limit=1", key) or []
        status = deploys[0]["deploy"]["status"] if deploys else "pending"
        print(f"  deploy: {status}", flush=True)
        if status == "live":
            return status
        if status in ("build_failed", "update_failed", "canceled", "deactivated"):
            return status
        time.sleep(15)
    return "timeout"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="public git repo URL Render can clone")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--name", default="video-grabber")
    parser.add_argument("--plan", default="free", choices=["free", "starter", "standard", "pro"])
    parser.add_argument("--region", default="singapore")
    parser.add_argument("--owner", help="workspace name; defaults to the first one")
    parser.add_argument("--max-concurrent", default="10")
    parser.add_argument("--proxy-url", default="https://proxys-o6v0.onrender.com/proxy/details")
    parser.add_argument("--proxy-first", action="store_true", help="use a proxy on the first attempt")
    parser.add_argument("--cookie", help="cf_clearance value for ytdlp.online")
    parser.add_argument("--cookie-file", default=".ytdlp_cookie")
    parser.add_argument("--env", action="append", metavar="KEY=VALUE")
    parser.add_argument("--api-key")
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    key = api_key(args.api_key)
    vars_ = env_vars(args)
    existing = find_service(key, args.name)

    if existing:
        service_id = existing["id"]
        print(f"updating existing service {service_id}")
        call("PUT", f"/services/{service_id}/env-vars", key, json=vars_)
        call("POST", f"/services/{service_id}/deploys", key, json={"clearCache": "do_not_clear"})
        service = existing
    else:
        owner = owner_id(key, args.owner)
        print(f"creating service in {owner}")
        created = call("POST", "/services", key, json=service_payload(args, owner, vars_))
        service = created.get("service", created)
        service_id = service["id"]

    url = (service.get("serviceDetails") or {}).get("url")
    print(f"id:  {service_id}")
    print(f"url: {url}")
    print(f"dashboard: {service.get('dashboardUrl', 'https://dashboard.render.com')}")

    if args.no_wait:
        return
    print("waiting for the deploy…")
    status = wait_for_live(key, service_id, args.timeout)
    print(f"final: {status}")
    if status != "live":
        sys.exit(1)


if __name__ == "__main__":
    main()
