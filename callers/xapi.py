"""Official X API v2, read-only, app bearer token (env X_BEARER_TOKEN, set by the user on Render from their own X
developer account; never logged, never stored, never sent anywhere but api.x.com). No user login, no cookies, no
scraping, no posting. Hard monthly budget (X_MONTHLY_BUDGET_USD, default 0 = OFF): before every request the worst
case (max_results x price) must fit in what is left; the ledger is an ESTIMATE — X's own bill is authoritative."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from callers import config as C

API = "https://api.x.com/2"
ALLOWED = ("/users/by/username/", "/users/", "/tweets/search/recent")
TWEET_FIELDS = "created_at,entities,author_id,referenced_tweets"


class BudgetExceeded(Exception):
    pass


class XDisabled(Exception):
    pass


def _http(url: str, token: str, timeout: float = 30):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "smart-caller-hunter"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class XClient:
    def __init__(self, store, token: str | None = None, budget: float | None = None, transport=_http,
                 sleep=time.sleep):
        self.store = store
        self.token = token if token is not None else os.environ.get(C.X_BEARER_TOKEN_ENV)
        self.budget = C.X_MONTHLY_BUDGET_USD if budget is None else budget
        self.transport, self.sleep = transport, sleep
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token) and self.budget > 0

    def status(self) -> dict:
        return {"enabled": self.enabled, "token_set": bool(self.token), "budget_usd": self.budget,
                "spent_est_usd": round(self.store.spent(), 4), "month": self.store.month(), "error": self.last_error,
                "price_per_post": C.X_COST_PER_POST, "price_per_user": C.X_COST_PER_USER}

    def _get(self, path: str, params: dict, worst_posts: int, worst_users: int) -> dict:
        if not self.enabled:
            raise XDisabled("X collection is off (no X_BEARER_TOKEN or X_MONTHLY_BUDGET_USD = 0)")
        if not any(path.startswith(a) for a in ALLOWED):
            raise PermissionError(f"read-only X client: {path} not allowed")
        worst = worst_posts * C.X_COST_PER_POST + worst_users * C.X_COST_PER_USER
        if self.store.spent() + worst > self.budget:
            raise BudgetExceeded(f"monthly X budget {self.budget} USD would be exceeded")
        url = f"{API}{path}?{urllib.parse.urlencode(params)}"
        try:
            d = self.transport(url, self.token)
        except urllib.error.HTTPError as e:
            self.last_error = f"HTTP {e.code}"
            if e.code == 429:                       # rate limited: back off, no retry storm (budget untouched)
                self.sleep(15)
            raise
        posts = len(d.get("data") or []) if isinstance(d.get("data"), list) else (1 if d.get("data") else 0)
        users = len((d.get("includes") or {}).get("users") or [])
        if path.startswith("/users/by/username/"):
            posts, users = 0, 1
        self.store.charge(posts, users, posts * C.X_COST_PER_POST + users * C.X_COST_PER_USER)
        self.last_error = None
        return d

    def user(self, username: str) -> dict | None:
        d = self._get(f"/users/by/username/{username}", {"user.fields": "public_metrics,name"}, 0, 1)
        return d.get("data")

    def timeline(self, user_id: str, since_id: str | None = None, max_results: int = C.X_MAX_RESULTS,
                 pagination_token: str | None = None) -> dict:
        p = {"max_results": max(5, min(100, max_results)), "tweet.fields": TWEET_FIELDS, "exclude": "retweets"}
        if since_id:
            p["since_id"] = since_id
        if pagination_token:
            p["pagination_token"] = pagination_token
        return self._get(f"/users/{user_id}/tweets", p, p["max_results"], 0)

    def search(self, query: str, since_id: str | None = None, max_results: int = C.X_MAX_RESULTS) -> dict:
        p = {"query": query, "max_results": max(10, min(100, max_results)), "tweet.fields": TWEET_FIELDS,
             "expansions": "author_id", "user.fields": "username,public_metrics"}
        if since_id:
            p["since_id"] = since_id
        return self._get("/tweets/search/recent", p, p["max_results"], p["max_results"])


def post_urls(tweet: dict) -> list[str]:
    return [u.get("expanded_url") or u.get("url") or "" for u in ((tweet.get("entities") or {}).get("urls") or [])]


def iso_ts(s: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
