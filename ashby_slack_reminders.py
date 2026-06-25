"""
Console interview reminders: Ashby -> Slack

Runs on a schedule (every ~10 minutes). For each upcoming interview it:
  1. posts a heads-up to ONE Slack channel 30 minutes before the start, tagging the interviewer(s)
  2. posts a feedback reminder 15 minutes after the end, with a direct link to the feedback form,
     and skips anyone who has already submitted.

State is stored in Replit's key-value DB so nothing is ever sent twice.

Required environment variables (set these as Replit Secrets):
  ASHBY_API_KEY    Ashby API key with interviewsRead + candidatesRead scopes
  SLACK_BOT_TOKEN  Slack bot token (xoxb-...) with chat:write, users:read, users:read.email
  SLACK_CHANNEL    Slack channel ID to post into (e.g. C0123ABC)
  REPLIT_DB_URL    provided automatically by Replit
Optional:
  MINUTES_BEFORE   default 30
  MINUTES_AFTER    default 15
  LOOKBACK_DAYS    default 14  (how far back to pull schedules from Ashby)
  DRY_RUN          set to "1" to log instead of posting to Slack
"""

import os
import json
import base64
import datetime as dt
import pathlib
import urllib.request
import urllib.parse

ASHBY_API_KEY = os.environ["ASHBY_API_KEY"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL = os.environ["SLACK_CHANNEL"]
DB_URL = os.environ.get("REPLIT_DB_URL")

MINUTES_BEFORE = int(os.environ.get("MINUTES_BEFORE", "30"))
MINUTES_AFTER = int(os.environ.get("MINUTES_AFTER", "15"))
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "14"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

NOW = dt.datetime.now(dt.timezone.utc)


def _post_json(url, payload, headers):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


# ---------- Ashby ----------

def ashby(endpoint, body=None):
    auth = base64.b64encode(f"{ASHBY_API_KEY}:".encode()).decode()
    return _post_json(
        f"https://api.ashbyhq.com/{endpoint}",
        body or {},
        {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )


def list_interview_events():
    """Pull recent interview schedules and flatten to their interview events."""
    created_after = int((NOW - dt.timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)
    events, cursor = [], None
    while True:
        body = {"createdAfter": created_after, "limit": 100}
        if cursor:
            body["cursor"] = cursor
        resp = ashby("interviewSchedule.list", body)
        for sched in resp.get("results", []):
            for ev in sched.get("interviewEvents", []):
                ev["_applicationId"] = sched.get("applicationId")
                events.append(ev)
        if resp.get("moreDataAvailable") and resp.get("nextCursor"):
            cursor = resp["nextCursor"]
        else:
            break
    return events


_app_cache = {}

def candidate_and_role(application_id):
    """Best-effort candidate name + role for nicer messages. Falls back gracefully."""
    if not application_id:
        return ("the candidate", "")
    if application_id in _app_cache:
        return _app_cache[application_id]
    name, role = "the candidate", ""
    try:
        info = ashby("application.info", {"applicationId": application_id}).get("results", {})
        cand = info.get("candidate") or {}
        name = cand.get("name") or name
        job = info.get("job") or (info.get("jobInfo") or {})
        role = job.get("title") or ""
    except Exception as e:
        print(f"  (could not resolve candidate for {application_id}: {e})")
    _app_cache[application_id] = (name, role)
    return (name, role)


# ---------- Slack ----------

_slack_user_cache = {}

def slack_user_id(email):
    if not email:
        return None
    if email in _slack_user_cache:
        return _slack_user_cache[email]
    uid = None
    try:
        url = "https://slack.com/api/users.lookupByEmail?" + urllib.parse.urlencode({"email": email})
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode())
        if resp.get("ok"):
            uid = resp["user"]["id"]
    except Exception as e:
        print(f"  (slack lookup failed for {email}: {e})")
    _slack_user_cache[email] = uid
    return uid


def slack_post(text):
    if DRY_RUN:
        print(f"  [DRY_RUN] would post: {text}")
        return
    resp = _post_json(
        "https://slack.com/api/chat.postMessage",
        {"channel": SLACK_CHANNEL, "text": text, "unfurl_links": False},
        {"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"},
    )
    if not resp.get("ok"):
        print(f"  (slack post error: {resp.get('error')})")


def mentions(interviewers):
    tags = []
    for u in interviewers:
        uid = slack_user_id(u.get("email"))
        first = u.get("firstName") or (u.get("name") or "").split(" ")[0] or "there"
        tags.append(f"<@{uid}>" if uid else first)
    return ", ".join(tags) if tags else "team"


# ---------- State ----------
# Uses Replit DB if REPLIT_DB_URL is set (Replit hosting),
# otherwise a local JSON file (GitHub Actions / any host).

_file_state = None

def _state_set():
    global _file_state
    if _file_state is None:
        try:
            _file_state = set(json.loads(pathlib.Path(STATE_FILE).read_text()))
        except Exception:
            _file_state = set()
    return _file_state

def already_sent(key):
    if DB_URL:
        try:
            with urllib.request.urlopen(f"{DB_URL}/{urllib.parse.quote(key)}", timeout=15) as r:
                return r.read().decode() != ""
        except Exception:
            return False
    return key in _state_set()

def mark_sent(key):
    if DRY_RUN:
        return
    if DB_URL:
        try:
            data = urllib.parse.urlencode({key: "1"}).encode()
            urllib.request.urlopen(urllib.request.Request(DB_URL, data=data), timeout=15)
        except Exception as e:
            print(f"  (db write failed for {key}: {e})")
        return
    s = _state_set()
    s.add(key)
    try:
        pathlib.Path(STATE_FILE).write_text(json.dumps(sorted(s)))
    except Exception as e:
        print(f"  (state file write failed for {key}: {e})")


def parse_ts(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ---------- Main ----------

def run():
    events = list_interview_events()
    print(f"{NOW.isoformat()}  evaluating {len(events)} interview events")
    for ev in events:
        start, end = parse_ts(ev.get("startTime")), parse_ts(ev.get("endTime"))
        if not start or not end:
            continue
        ev_id = ev.get("id")
        interviewers = ev.get("interviewers", [])

        # 1) heads-up 30 min before start
        pre_at = start - dt.timedelta(minutes=MINUTES_BEFORE)
        if pre_at <= NOW < start + dt.timedelta(minutes=2) and not already_sent(f"pre:{ev_id}"):
            name, role = candidate_and_role(ev.get("_applicationId"))
            role_txt = f" for *{role}*" if role else ""
            slack_post(f":calendar: Interview in {MINUTES_BEFORE} min — {mentions(interviewers)} with *{name}*{role_txt}.")
            mark_sent(f"pre:{ev_id}")
            print(f"  sent PRE for {ev_id}")

        # 2) feedback reminder 15 min after end (skip if already submitted)
        post_at = end + dt.timedelta(minutes=MINUTES_AFTER)
        recent = end > NOW - dt.timedelta(days=1)
        if (post_at <= NOW and recent and not ev.get("hasSubmittedFeedback")
                and not already_sent(f"post:{ev_id}")):
            name, role = candidate_and_role(ev.get("_applicationId"))
            link = ev.get("feedbackLink")
            link_txt = f" Submit it here: {link}" if link else " Submit it in Ashby (or reply to the Ashby app in Slack)."
            slack_post(f":memo: {mentions(interviewers)} — please submit your feedback for *{name}*.{link_txt}")
            mark_sent(f"post:{ev_id}")
            print(f"  sent POST for {ev_id}")


if __name__ == "__main__":
    run()
