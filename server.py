#!/usr/bin/env python3
"""Mission Control — DevHUD · local status server (127.0.0.1 only).

Lists all your git repos and shows each one's development status as a
sci-fi HUD. Reads ONLY git structure / counts. Never reads .env, keys,
tokens or secrets. Configure via config.json.
"""
import json, os, re, subprocess, datetime, collections, urllib.parse
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
PORT = int(CFG.get("port") or 8799)
DEFAULT_SCAN = ["~/Desktop", "~/Documents", "~/Projects", "~/projects", "~/dev", "~/code", "~/src", "~"]


def expand(p):
    return os.path.abspath(os.path.expanduser(p or ""))


def sh(repo, args, timeout=25):
    try:
        r = subprocess.run(args, cwd=repo, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def ok(repo, args):
    try:
        return subprocess.run(args, cwd=repo, capture_output=True, timeout=25).returncode == 0
    except Exception:
        return False


def discover_projects():
    projs, seen = [], set()
    for p in CFG.get("projects", []):
        repo = expand(p.get("repo") or HERE)
        if repo in seen:
            continue
        seen.add(repo)
        item = dict(p)
        item["repo"] = repo
        item["name"] = p.get("name") or os.path.basename(repo)
        item["configured"] = True
        projs.append(item)
    scan = CFG.get("scanDirs", DEFAULT_SCAN)
    for d in scan:
        dd = expand(d)
        if not os.path.isdir(dd):
            continue
        try:
            for name in sorted(os.listdir(dd)):
                sub = os.path.join(dd, name)
                if name.startswith(".") or not os.path.isdir(sub):
                    continue
                if os.path.isdir(os.path.join(sub, ".git")) and sub not in seen:
                    seen.add(sub)
                    projs.append({"repo": sub, "name": name, "branch": "", "configured": False})
                if len(projs) >= 60:
                    break
        except Exception:
            pass
    return projs


PROJECTS = discover_projects()


def find_project(pid):
    if not pid:
        return PROJECTS[0] if PROJECTS else None
    for p in PROJECTS:
        if p["repo"] == pid or p["name"] == pid:
            return p
    return PROJECTS[0] if PROJECTS else None


def resolve_ref(proj):
    repo, branch, fetch = proj["repo"], proj.get("branch") or "", bool(proj.get("fetch"))
    if branch and fetch:
        sh(repo, ["git", "fetch", "-q", "origin", branch], timeout=18)
        if ok(repo, ["git", "rev-parse", "--verify", "origin/" + branch]):
            return "origin/" + branch
    if branch and ok(repo, ["git", "rev-parse", "--verify", branch]):
        return branch
    return "HEAD"


def milestones(repo, ref, defs):
    out, done = [], 0
    for m in defs:
        s = bool(m.get("done"))
        if m.get("detectFile"):
            s = ok(repo, ["git", "cat-file", "-e", f"{ref}:{m['detectFile']}"])
        elif m.get("detectCommit"):
            s = re.search(m["detectCommit"], sh(repo, ["git", "log", "--oneline", ref])) is not None
        if s:
            done += 1
        out.append({"n": m.get("name", "?"), "s": "done" if s else None})
    first = True
    for mm in out:
        if mm["s"] is None:
            mm["s"] = "run" if first else "wait"
            first = False
    return out, done


def build_status(proj):
    repo = proj["repo"]
    area_re = proj.get("areaRegex") or CFG.get("areaRegex") or r"\[([^\]]+)\]|^([a-z]+)(?:\(|:)"
    ref = resolve_ref(proj)
    ms, done = milestones(repo, ref, proj.get("milestones", []))
    total = len(ms)

    def area_of(subject):
        m = re.search(area_re, subject)
        return ((m.group(1) or m.group(2) or "").strip() or None) if m else None

    clog = sh(repo, ["git", "log", ref, "--date-order", "--pretty=format:%h|%cr|%s", "-14"])
    commits = []
    for l in clog.splitlines():
        if "|" in l:
            sha, ago, msg = l.split("|", 2)
            commits.append({"sha": sha, "ago": ago, "msg": msg, "area": area_of(msg)})

    total_commits = sh(repo, ["git", "rev-list", "--count", ref]) or "0"
    branches_n = len([b for b in sh(repo, ["git", "branch"]).splitlines() if b.strip()])
    contrib = len(set(x for x in sh(repo, ["git", "log", ref, "--format=%an"]).splitlines() if x))
    files = [x for x in sh(repo, ["git", "ls-files"]).splitlines() if x]
    pages = len([x for x in files if x.lower().endswith((".html", ".htm", ".tsx", ".jsx", ".vue", ".svelte"))])
    last = sh(repo, ["git", "log", "-1", "--format=%cr", ref]) or "-"

    raw = sh(repo, ["git", "log", ref, "--since=14.days", "--date=format:%Y-%m-%d", "--pretty=%cd"])
    cnt = collections.Counter(raw.splitlines())
    today = datetime.date.today()
    timeline = [{"d": (today - datetime.timedelta(days=i)).strftime("%m/%d"),
                 "v": cnt.get((today - datetime.timedelta(days=i)).strftime("%Y-%m-%d"), 0)} for i in range(13, -1, -1)]

    areas = collections.Counter()
    for s in sh(repo, ["git", "log", ref, "--pretty=%s", "-150"]).splitlines():
        a = area_of(s)
        if a:
            areas[a] += 1
    area_list = [{"n": k, "v": v} for k, v in areas.most_common(9)]

    todo = sh(repo, ["bash", "-lc", "git grep -I -oE '(TODO|FIXME|HACK|XXX)' 2>/dev/null | wc -l | tr -d ' '"]) or "0"
    braw = sh(repo, ["git", "for-each-ref", "--sort=-committerdate", "refs/heads",
                     "--format=%(refname:short)|%(committerdate:relative)", "--count=7"])
    branches = [{"n": x.split("|", 1)[0], "ago": (x.split("|", 1)[1] if "|" in x else "")} for x in braw.splitlines() if x]

    gh = [["Total commits", total_commits], ["Branches", str(branches_n)], ["Contributors", str(contrib)],
          ["Pages/views", str(pages)], ["Tracked files", str(len(files))], ["TODO / FIXME", str(todo)],
          ["Last activity", last]]

    return {
        "project": proj.get("name"), "tagline": proj.get("tagline") or CFG.get("tagline", "MISSION CONTROL"),
        "theme": proj.get("theme") or CFG.get("theme", "cyan"), "panels": proj.get("panels") or CFG.get("panels", {}),
        "repo": repo, "ref": ref, "configured": proj.get("configured", False),
        "asOf": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": total, "done": done, "milestones": ms, "commits": commits, "gh": gh,
        "timeline": timeline, "areas": area_list, "todo": int(todo or 0), "branches": branches,
        "recs": proj.get("recs", CFG.get("recs", [])), "pages": pages, "files": len(files),
    }


def projects_list():
    return {"projects": [{"id": p["repo"], "name": p["name"], "branch": p.get("branch") or "",
                          "configured": p.get("configured", False)} for p in PROJECTS]}


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode("utf-8"))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/api/projects":
            return self._send(200, json.dumps(projects_list(), ensure_ascii=False), "application/json; charset=utf-8")
        if u.path == "/api/status":
            try:
                proj = find_project(q.get("p", [None])[0])
                if not proj:
                    return self._send(200, json.dumps({"error": "no git repos found"}), "application/json")
                return self._send(200, json.dumps(build_status(proj), ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}), "application/json")
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "web", "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            except Exception:
                return self._send(404, "not found", "text/plain")
        self._send(404, "not found", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Mission Control — DevHUD on http://127.0.0.1:{PORT}  ({len(PROJECTS)} repos)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
