#!/usr/bin/env python3
"""Mission Control — DevHUD · local status server (127.0.0.1 only).

Reads ONLY structural / count data from git. Never reads .env, keys, tokens
or secrets. Point it at any git repo via config.json.
"""
import json, os, re, shlex, subprocess, datetime, collections
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config():
    for name in ("config.json", "config.sample.json"):
        p = os.path.join(HERE, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
    return {}


CFG = load_config()
REPO = os.path.expanduser(CFG.get("repo") or "") or HERE
BRANCH = CFG.get("branch") or ""
DO_FETCH = bool(CFG.get("fetch"))
PORT = int(CFG.get("port") or 8799)
AREA_RE = CFG.get("areaRegex") or r"\[([^\]]+)\]|^([a-z]+)(?:\(|:)"


def sh(args, timeout=25):
    try:
        r = subprocess.run(args, cwd=REPO, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def ok(args):
    try:
        return subprocess.run(args, cwd=REPO, capture_output=True, timeout=25).returncode == 0
    except Exception:
        return False


def resolve_ref():
    if BRANCH and DO_FETCH:
        sh(["git", "fetch", "-q", "origin", BRANCH], timeout=18)
        if ok(["git", "rev-parse", "--verify", "origin/" + BRANCH]):
            return "origin/" + BRANCH
    if BRANCH and ok(["git", "rev-parse", "--verify", BRANCH]):
        return BRANCH
    return "HEAD"


def milestones(ref):
    out, done = [], 0
    for m in CFG.get("milestones", []):
        s = bool(m.get("done"))
        if m.get("detectFile"):
            s = ok(["git", "cat-file", "-e", f"{ref}:{m['detectFile']}"])
        elif m.get("detectCommit"):
            log = sh(["git", "log", "--oneline", ref])
            s = re.search(m["detectCommit"], log) is not None
        if s:
            done += 1
        out.append({"n": m.get("name", "?"), "s": "done" if s else None})
    first = True
    for mm in out:
        if mm["s"] is None:
            mm["s"] = "run" if first else "wait"
            first = False
    return out, done


def area_of(subject):
    m = re.search(AREA_RE, subject)
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip() or None


def build_status():
    ref = resolve_ref()
    ms, done = milestones(ref)
    total = max(1, len(ms))

    # recent commits
    clog = sh(["git", "log", ref, "--date-order", "--pretty=format:%h|%cr|%s", "-12"])
    commits = []
    for l in clog.splitlines():
        if "|" in l:
            sha, ago, msg = l.split("|", 2)
            commits.append({"sha": sha, "ago": ago, "msg": msg, "area": area_of(msg)})

    # KPI counts
    total_commits = sh(["git", "rev-list", "--count", ref]) or "0"
    branches_list = [b.strip().lstrip("* ").strip() for b in sh(["git", "branch"]).splitlines() if b.strip()]
    branches_n = len(branches_list)
    contrib = len(set(x for x in sh(["git", "log", ref, "--format=%an"]).splitlines() if x))
    files = [x for x in sh(["git", "ls-files"]).splitlines() if x]
    pages = len([x for x in files if x.lower().endswith((".html", ".htm", ".tsx", ".jsx", ".vue", ".svelte"))])
    last = sh(["git", "log", "-1", "--format=%cr", ref]) or "-"

    # 14-day commit timeline
    raw = sh(["git", "log", ref, "--since=14.days", "--date=format:%Y-%m-%d", "--pretty=%cd"])
    cnt = collections.Counter(raw.splitlines())
    today = datetime.date.today()
    timeline = []
    for i in range(13, -1, -1):
        d = today - datetime.timedelta(days=i)
        key = d.strftime("%Y-%m-%d")
        timeline.append({"d": d.strftime("%m/%d"), "v": cnt.get(key, 0)})

    # area distribution (last 120 commits)
    subs = sh(["git", "log", ref, "--pretty=%s", "-120"]).splitlines()
    areas = collections.Counter()
    for s in subs:
        a = area_of(s)
        if a:
            areas[a] += 1
    area_list = [{"n": k, "v": v} for k, v in areas.most_common(8)]

    # TODO / FIXME signal
    todo = sh(["bash", "-lc", "git grep -I -oE '(TODO|FIXME|HACK|XXX)' 2>/dev/null | wc -l | tr -d ' '"]) or "0"

    # branch activity
    braw = sh(["git", "for-each-ref", "--sort=-committerdate", "refs/heads",
               "--format=%(refname:short)|%(committerdate:relative)", "--count=6"])
    branches = [{"n": x.split("|", 1)[0], "ago": (x.split("|", 1)[1] if "|" in x else "")}
                for x in braw.splitlines() if x]

    gh = [["Total commits", total_commits], ["Branches", str(branches_n)],
          ["Contributors", str(contrib)], ["Pages/views", str(pages)],
          ["Tracked files", str(len(files))], ["TODO / FIXME", str(todo)],
          ["Last activity", last]]

    return {
        "project": CFG.get("project", "Mission Control"),
        "tagline": CFG.get("tagline", "MISSION CONTROL"),
        "theme": CFG.get("theme", "cyan"),
        "panels": CFG.get("panels", {}),
        "asOf": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ref": ref,
        "total": total, "done": done, "milestones": ms,
        "commits": commits, "gh": gh,
        "timeline": timeline, "areas": area_list,
        "todo": int(todo or 0), "branches": branches,
        "recs": CFG.get("recs", []),
        "pages": pages, "files": len(files),
    }


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode("utf-8"))

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/status":
            try:
                self._send(200, json.dumps(build_status(), ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}), "application/json")
            return
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "web", "index.html"), "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except Exception:
                self._send(404, "not found", "text/plain")
            return
        self._send(404, "not found", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Mission Control — DevHUD on http://127.0.0.1:{PORT}  (repo: {REPO}, ref: {BRANCH or 'HEAD'})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
