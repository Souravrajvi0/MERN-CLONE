"""Submit one solution from ./solutions to LeetCode and log the result.

Environment:
  LC_SESSION   LEETCODE_SESSION cookie (required)
  LC_CSRF      csrftoken cookie (required)
  NO_DELAY     "1"/"true" to skip the random delay
  DELAY_MODE   "sleep"  (default) - sleep 0..MAX_DELAY_HOURS, then submit
               "hourly" - meant for an hourly cron; submit only during the
                          hour derived from today's date + DELAY_SALT
  MAX_DELAY_HOURS  upper bound for "sleep" mode (default 5)
  SUBMISSIONS_PER_RUN  "N" or a range like "2-3" (default 1); each is placed at
               its own random time inside the delay window
  DELAY_SALT   secret salt for "hourly" mode
  SOLUTION     force a specific solution file name (e.g. two-sum.py3)
"""

from __future__ import annotations

import csv
import hashlib
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://leetcode.com"
ROOT = Path(__file__).resolve().parent
SOLUTIONS_DIR = ROOT / "solutions"
LOG_FILE = ROOT / "log" / "submissions.csv"
LOG_FIELDS = ["timestamp_utc", "slug", "submission_id", "status"]

LANG_BY_EXT = {
    ".py3": "python3",
    ".py": "python",
    ".cpp": "cpp",
    ".java": "java",
    ".js": "javascript",
    ".ts": "typescript",
    ".go": "golang",
    ".rs": "rust",
    ".c": "c",
    ".cs": "csharp",
    ".kt": "kotlin",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

RETRY_WAITS = [10, 30, 90]
POLL_INTERVAL = 2
POLL_TIMEOUT = 60

# Exit codes, read by the workflow to decide what to report.
EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_SESSION_EXPIRED = 2
EXIT_BLOCKED = 3
EXIT_NOT_ACCEPTED = 4
EXIT_ERROR = 5


class Fatal(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def set_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------- timing


def submission_count() -> int:
    """Parse SUBMISSIONS_PER_RUN, either "N" or a range like "2-3"."""
    raw = os.environ.get("SUBMISSIONS_PER_RUN", "1").strip() or "1"
    try:
        low, _, high = raw.partition("-")
        low_n = int(low)
        high_n = int(high) if high else low_n
    except ValueError:
        raise Fatal(f"Invalid SUBMISSIONS_PER_RUN={raw!r}", EXIT_CONFIG)
    if low_n < 1 or high_n < low_n:
        raise Fatal(f"Invalid SUBMISSIONS_PER_RUN={raw!r}", EXIT_CONFIG)
    return random.randint(low_n, high_n)


def spread(count: int, window: int, min_gap: int) -> list[int]:
    """`count` sorted random offsets in [0, window], at least `min_gap` apart when possible."""
    min_gap = min(min_gap, window // max(count, 1))
    for _ in range(1000):
        offsets = sorted(random.randint(0, window) for _ in range(count))
        if all(b - a >= min_gap for a, b in zip(offsets, offsets[1:])):
            return offsets
    return [i * (window // count) for i in range(count)]


def plan_schedule(count: int) -> list[int] | None:
    """Offsets in seconds from now for each submission, or None if this run should skip."""
    if truthy(os.environ.get("NO_DELAY")):
        log("NO_DELAY set, submitting immediately")
        return [i * random.randint(15, 30) for i in range(count)]

    mode = os.environ.get("DELAY_MODE", "sleep").strip().lower()

    if mode == "hourly":
        salt = os.environ.get("DELAY_SALT", "")
        now = datetime.now(timezone.utc)
        digest = hashlib.sha256(f"{now:%Y-%m-%d}:{salt}".encode()).hexdigest()
        target_hour = int(digest, 16) % 24
        if now.hour != target_hour:
            log("Not the target hour for today, exiting")
            return None
        return spread(count, 50 * 60, 5 * 60)

    max_hours = float(os.environ.get("MAX_DELAY_HOURS", "5"))
    return spread(count, int(max_hours * 3600), 20 * 60)


# ---------------------------------------------------------------- solutions


def read_log() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    with LOG_FILE.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_log(slug: str, submission_id: str, status: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    new_file = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "slug": slug,
                "submission_id": submission_id,
                "status": status,
            }
        )


def pick_solution() -> Path:
    files = sorted(
        p for p in SOLUTIONS_DIR.iterdir() if p.is_file() and p.suffix in LANG_BY_EXT
    )
    if not files:
        raise Fatal(f"No solution files found in {SOLUTIONS_DIR}", EXIT_CONFIG)

    forced = os.environ.get("SOLUTION", "").strip()
    if forced:
        for p in files:
            if p.name == forced:
                return p
        raise Fatal(f"SOLUTION={forced} not found in {SOLUTIONS_DIR}", EXIT_CONFIG)

    last_used: dict[str, str] = {}
    last_status: dict[str, str] = {}
    for row in read_log():
        last_used[row["slug"]] = row["timestamp_utc"]
        last_status[row["slug"]] = row["status"]

    # Skip solutions whose last submission failed so one bad file can't fail every run.
    usable = [p for p in files if last_status.get(p.stem, "Accepted") == "Accepted"]
    if not usable:
        raise Fatal("Every solution's last submission failed; fix or remove them", EXIT_CONFIG)

    fresh = [p for p in usable if p.stem not in last_used]
    if fresh:
        return random.choice(fresh)
    return min(usable, key=lambda p: last_used[p.stem])


# ---------------------------------------------------------------- http


class LeetCodeClient:
    def __init__(self, session_cookie: str, csrf: str):
        self.csrf = csrf
        self.http = requests.Session()
        self.http.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Origin": BASE,
                "Content-Type": "application/json",
                "x-csrftoken": csrf,
            }
        )
        self.http.cookies.set("LEETCODE_SESSION", session_cookie, domain=".leetcode.com")
        self.http.cookies.set("csrftoken", csrf, domain=".leetcode.com")

    def _request(self, method: str, path: str, referer: str, **kwargs) -> dict:
        url = BASE + path
        headers = {"Referer": referer}
        for attempt in range(len(RETRY_WAITS) + 1):
            try:
                resp = self.http.request(method, url, headers=headers, timeout=30, **kwargs)
            except requests.RequestException as exc:
                status, err = None, f"network error: {exc.__class__.__name__}"
            else:
                status, err = resp.status_code, None
                if status < 400:
                    try:
                        return resp.json()
                    except ValueError:
                        if "cloudflare" in resp.text.lower() or "<html" in resp.text.lower():
                            raise Fatal(
                                f"{path}: got an HTML page instead of JSON "
                                "(likely a Cloudflare challenge)",
                                EXIT_BLOCKED,
                            )
                        raise Fatal(f"{path}: response was not JSON", EXIT_ERROR)
                if status in (401,):
                    raise Fatal("Unauthorized - update LC_SESSION / LC_CSRF", EXIT_SESSION_EXPIRED)
                if status == 403:
                    body = resp.text.lower()
                    if "cloudflare" in body or "cf-ray" in str(resp.headers).lower():
                        raise Fatal(f"{path}: 403 from Cloudflare (IP blocked)", EXIT_BLOCKED)
                    raise Fatal(
                        f"{path}: 403 Forbidden - cookies are likely expired or CSRF mismatch",
                        EXIT_SESSION_EXPIRED,
                    )
                if status != 429 and status < 500:
                    raise Fatal(f"{path}: HTTP {status}", EXIT_ERROR)
                err = f"HTTP {status}"

            if attempt < len(RETRY_WAITS):
                wait = RETRY_WAITS[attempt]
                log(f"{path}: {err}, retrying in {wait}s ({attempt + 1}/{len(RETRY_WAITS)})")
                time.sleep(wait)
        raise Fatal(f"{path}: giving up after {len(RETRY_WAITS)} retries ({err})", EXIT_ERROR)

    def graphql(self, query: str, variables: dict | None = None, referer: str = BASE + "/") -> dict:
        data = self._request(
            "POST", "/graphql/", referer, json={"query": query, "variables": variables or {}}
        )
        if data.get("errors"):
            raise Fatal(f"GraphQL error: {data['errors']}", EXIT_ERROR)
        return data["data"]

    def check_session(self) -> str:
        data = self.graphql("query { userStatus { isSignedIn username } }")
        status = data["userStatus"]
        if not status["isSignedIn"]:
            raise Fatal(
                "LeetCode session is not signed in - copy fresh LEETCODE_SESSION "
                "and csrftoken into the LC_SESSION / LC_CSRF secrets",
                EXIT_SESSION_EXPIRED,
            )
        return status["username"]

    def question_id(self, slug: str) -> str:
        data = self.graphql(
            "query($s: String!) { question(titleSlug: $s) { questionId } }",
            {"s": slug},
            referer=f"{BASE}/problems/{slug}/",
        )
        if not data.get("question"):
            raise Fatal(f"Unknown problem slug: {slug}", EXIT_CONFIG)
        return data["question"]["questionId"]

    def submit(self, slug: str, question_id: str, lang: str, code: str) -> str:
        data = self._request(
            "POST",
            f"/problems/{slug}/submit/",
            f"{BASE}/problems/{slug}/",
            json={"lang": lang, "question_id": question_id, "typed_code": code},
        )
        if "submission_id" not in data:
            raise Fatal(f"Submit response had no submission_id: {data}", EXIT_ERROR)
        return str(data["submission_id"])

    def wait_for_result(self, slug: str, submission_id: str) -> dict:
        deadline = time.monotonic() + POLL_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            data = self._request(
                "GET",
                f"/submissions/detail/{submission_id}/check/",
                f"{BASE}/problems/{slug}/",
            )
            if data.get("state") == "SUCCESS":
                return data
        raise Fatal(f"Submission {submission_id} still pending after {POLL_TIMEOUT}s", EXIT_ERROR)


# ---------------------------------------------------------------- main


def main() -> int:
    session_cookie = os.environ.get("LC_SESSION", "").strip()
    csrf = os.environ.get("LC_CSRF", "").strip()
    if not session_cookie or not csrf:
        raise Fatal("LC_SESSION and LC_CSRF must both be set", EXIT_CONFIG)

    # A forced SOLUTION is a one-off test, so it is submitted exactly once.
    count = 1 if os.environ.get("SOLUTION", "").strip() else submission_count()
    schedule = plan_schedule(count)
    if schedule is None:
        set_output("result", "skipped")
        return EXIT_OK
    log("Plan: " + ", ".join(f"+{s // 60}m{s % 60:02d}s" for s in schedule))

    client = LeetCodeClient(session_cookie, csrf)
    username = client.check_session()
    log(f"Session OK, signed in as {username}")

    start = time.monotonic()
    failed = 0
    for n, offset in enumerate(schedule, 1):
        wait = offset - (time.monotonic() - start)
        if wait > 0:
            log(f"[{n}/{count}] Sleeping {int(wait) // 60} min {int(wait) % 60} s")
            time.sleep(wait)
            if wait > 60:
                # The session could have been invalidated during a long sleep.
                client.check_session()

        solution = pick_solution()
        slug = solution.stem
        lang = LANG_BY_EXT[solution.suffix]
        code = solution.read_text(encoding="utf-8")
        log(f"[{n}/{count}] Selected {solution.name} (slug={slug}, lang={lang})")

        qid = client.question_id(slug)
        submission_id = client.submit(slug, qid, lang, code)
        log(f"[{n}/{count}] Submitted question {qid}, submission_id={submission_id}")

        result = client.wait_for_result(slug, submission_id)
        status_msg = result.get("status_msg", "Unknown")
        log(
            f"[{n}/{count}] Result: {status_msg} "
            f"({result.get('total_correct')}/{result.get('total_testcases')} tests, "
            f"runtime {result.get('status_runtime')}, memory {result.get('status_memory')})"
        )

        append_log(slug, submission_id, status_msg)
        set_output("result", "submitted")
        if status_msg != "Accepted":
            failed += 1

    if failed:
        log(f"{failed}/{count} submissions were not Accepted")
        set_output("failure", "not_accepted")
        return EXIT_NOT_ACCEPTED
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fatal as exc:
        log(f"ERROR: {exc}")
        reason = {
            EXIT_CONFIG: "config",
            EXIT_SESSION_EXPIRED: "session_expired",
            EXIT_BLOCKED: "blocked",
            EXIT_NOT_ACCEPTED: "not_accepted",
        }.get(exc.code, "error")
        set_output("failure", reason)
        sys.exit(exc.code)
