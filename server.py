#!/usr/bin/env python3
"""Mission Control — DevHUD · local status server (127.0.0.1 only).

Lists all your git repos and shows each one's development status as a
sci-fi HUD. Reads ONLY git structure / counts. Never reads .env, keys,
tokens or secrets. Configure via config.json.
"""
import json, os, re, subprocess, datetime, collections, urllib.parse, urllib.request, urllib.error, threading, math, time
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
DEFAULT_SCAN = ["~", "~/Desktop", "~/Documents", "~/Downloads", "~/workspace",
                "~/Projects", "~/projects", "~/dev", "~/code", "~/src", "~/repos", "~/git"]
SCAN_DEPTH = int(CFG.get("scanDepth") or 3)
EXCLUDE = {"node_modules", "Library", ".Trash", "vendor", "__pycache__", ".cache",
           ".codex", ".npm", ".cargo", "Pods", "DerivedData", "dist", "build", ".next"}


def expand(p):
    return os.path.abspath(os.path.expanduser(p or ""))


def find_repos(base, maxdepth=SCAN_DEPTH):
    base = expand(base)
    if not os.path.isdir(base):
        return []
    res, base_sep = [], base.rstrip(os.sep).count(os.sep)
    for root, dirs, _ in os.walk(base):
        depth = root.count(os.sep) - base_sep
        dirs[:] = [d for d in dirs if d not in EXCLUDE and not d.startswith(".")]
        if os.path.isdir(os.path.join(root, ".git")):
            res.append(root)        # keep descending: find nested project repos too
        if depth >= maxdepth:
            dirs[:] = []
        if len(res) >= 120:
            break
    return res


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


def pid_of(p):
    return p["repo"] + ("#" + p["subpath"] if p.get("subpath") else "")


def discover_projects():
    projs, seen = [], set()
    for p in CFG.get("projects", []):
        repo = expand(p.get("repo") or HERE)
        key = (repo, p.get("subpath") or "")
        if key in seen:
            continue
        seen.add(key)
        item = dict(p)
        item["repo"] = repo
        item["name"] = p.get("name") or os.path.basename(repo)
        item["configured"] = True
        projs.append(item)
    scan = CFG.get("scanDirs", DEFAULT_SCAN)
    found = []
    for d in scan:
        found.extend(find_repos(d))
    for repo in sorted(set(found), key=lambda r: os.path.basename(r).lower()):
        if (repo, "") in seen:
            continue
        seen.add((repo, ""))
        projs.append({"repo": repo, "name": os.path.basename(repo), "branch": "", "configured": False})
        if len(projs) >= 80:
            break
    return projs


PROJECTS = discover_projects()

# ---- "My Projects" board (local only): a curated list of non-git projects
# (e.g. Claude.ai projects, business/ops work) with status + progress, kept in
# my-projects.json next to this server. Writes bind to 127.0.0.1 only.
_board_lock = threading.Lock()
BOARD_PATH = os.path.join(HERE, "my-projects.json")
STATUSES = ("idea", "active", "done", "hold")  # 기획 / 진행 / 완료 / 보류


def board_enabled():
    # Opt-in only: the manual "My Projects" board stays hidden until the user
    # creates my-projects.json (or sets "board": true). Default experience is
    # 100% automatic — no manual data entry — so the tool works out of the box.
    return os.path.exists(BOARD_PATH) or bool(CFG.get("board"))


def load_board():
    try:
        with open(BOARD_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else data.get("projects", [])
    except Exception:
        return []


def save_board(items):
    with open(BOARD_PATH, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=2)


def _board_id():
    return "mp_" + datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")


def _days_ago(s):
    """Best-effort parse of a 'last activity' label (KO/EN, relative or absolute) → days."""
    s = (s or "").strip()
    if not s:
        return None
    today = datetime.date.today()
    low = s.lower()
    if "오늘" in s or "today" in low:
        return 0
    if "어제" in s or "yesterday" in low:
        return 1
    if "그저께" in s or "그제" in s:
        return 2
    if "지난주" in s or "last week" in low:
        return 7
    m = re.search(r"(\d+)\s*일\s*전", s) or re.search(r"(\d+)\s*days?\s*ago", low)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*월\s*(\d+)\s*일", s)            # 10월 19일
    if m:
        mo, da = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"([A-Za-z]{3,})\s+(\d+)", s)          # Oct 2
        if not m:
            return None
        try:
            mo = datetime.datetime.strptime(m.group(1)[:3], "%b").month
            da = int(m.group(2))
        except Exception:
            return None
    try:
        d = datetime.date(today.year, mo, da)
        if d > today:
            d = datetime.date(today.year - 1, mo, da)
        return (today - d).days
    except Exception:
        return None


def _activity(days):
    """Recency → activity score 0..100 (decays over ~3 weeks)."""
    if days is None:
        return 0
    return max(4, min(100, round(100 * math.exp(-days / 21.0))))


def _auto_status(days, cur):
    if cur == "done":
        return "done"
    if days is None:
        return cur or "hold"
    if days <= 14:
        return "active"
    if days <= 40:
        return "idea"   # 완만 — treated visually as mid
    return "hold"


def enrich_board(items):
    out = []
    for it in items:
        d = _days_ago(it.get("updated"))
        act = _activity(d)
        prog = int(it.get("progress") or 0)
        e = dict(it)
        e["daysAgo"] = d
        e["activity"] = act
        # show activity when the user has not set an explicit progress value
        e["metric"] = prog if prog > 0 else act
        e["status"] = _auto_status(d, it.get("status"))
        out.append(e)
    return out


def find_project(pid):
    if not pid:
        return PROJECTS[0] if PROJECTS else None
    for p in PROJECTS:
        if pid_of(p) == pid or p["name"] == pid:
            return p
    for p in PROJECTS:
        if p["repo"] == pid:
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


def _read(repo, path):
    try:
        with open(os.path.join(repo, path), encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except Exception:
        return ""


def build_status(proj):
    repo = proj["repo"]
    sub = proj.get("subpath")
    ps = (["--", sub] if sub else [])      # path scope for monorepo sub-apps
    area_re = proj.get("areaRegex") or CFG.get("areaRegex") or r"\[([^\]]+)\]|^([a-z]+)(?:\(|:)"
    ref = resolve_ref(proj)

    def area_of(subject):
        m = re.search(area_re, subject)
        return ((m.group(1) or m.group(2) or "").strip() or None) if m else None

    clog = sh(repo, ["git", "log", ref, "--date-order", "--pretty=format:%h|%cr|%s", "-14"] + ps)
    commits = []
    for l in clog.splitlines():
        if "|" in l:
            sha, ago, msg = l.split("|", 2)
            commits.append({"sha": sha, "ago": ago, "msg": msg, "area": area_of(msg)})

    total_commits = sh(repo, ["git", "rev-list", "--count", ref] + ps) or "0"
    branches_n = len([b for b in sh(repo, ["git", "branch"]).splitlines() if b.strip()])
    contrib = len(set(x for x in sh(repo, ["git", "log", ref, "--format=%an"] + ps).splitlines() if x))
    files = [x for x in sh(repo, ["git", "ls-files"] + ps).splitlines() if x]
    pages = len([x for x in files if x.lower().endswith((".html", ".htm", ".tsx", ".jsx", ".vue", ".svelte"))])
    last = sh(repo, ["git", "log", "-1", "--format=%cr", ref] + ps) or "-"
    dbt = 0
    for f in files:
        fl = f.lower()
        if fl.endswith(".sql"):
            dbt += len(re.findall(r"(?im)^\s*create\s+table", _read(repo, f)))
        elif fl.endswith(".prisma"):
            dbt += len(re.findall(r"(?im)^\s*model\s", _read(repo, f)))

    raw = sh(repo, ["git", "log", ref, "--since=14.days", "--date=format:%Y-%m-%d", "--pretty=%cd"] + ps)
    cnt = collections.Counter(raw.splitlines())
    today = datetime.date.today()
    timeline = [{"d": (today - datetime.timedelta(days=i)).strftime("%m/%d"),
                 "v": cnt.get((today - datetime.timedelta(days=i)).strftime("%Y-%m-%d"), 0)} for i in range(13, -1, -1)]

    areas = collections.Counter()
    for s in sh(repo, ["git", "log", ref, "--pretty=%s", "-150"] + ps).splitlines():
        a = area_of(s)
        if a:
            areas[a] += 1
    area_list = [{"n": k, "v": v} for k, v in areas.most_common(9)]

    todo = sh(repo, ["bash", "-lc", "git grep -I -oE '(TODO|FIXME|HACK|XXX)' 2>/dev/null | wc -l | tr -d ' '"]) or "0"
    braw = sh(repo, ["git", "for-each-ref", "--sort=-committerdate", "refs/heads",
                     "--format=%(refname:short)|%(committerdate:relative)", "--count=7"])
    branches = [{"n": x.split("|", 1)[0], "ago": (x.split("|", 1)[1] if "|" in x else "")} for x in braw.splitlines() if x]

    # code composition (file-type breakdown)
    extc = collections.Counter(os.path.splitext(f)[1].lower().lstrip(".") or "other" for f in files)
    langs = [{"n": k, "v": v} for k, v in extc.most_common(8)]
    # module structure (top-level dirs relative to subpath)
    basep = (sub.rstrip("/") + "/") if sub else ""
    dirc = collections.Counter()
    for f in files:
        rel = f[len(basep):] if basep and f.startswith(basep) else f
        dirc[rel.split("/")[0] if "/" in rel else "(root)"] += 1
    dirs = [{"n": k, "v": v} for k, v in dirc.most_common(8)]
    # contributors (top committers)
    cc = collections.Counter(x for x in sh(repo, ["git", "log", ref, "--format=%an"] + ps).splitlines() if x)
    contributors = [{"n": k, "v": v} for k, v in cc.most_common(6)]
    # tech stack detection
    deps = "".join(_read(repo, p).lower() for p in files if p.endswith("package.json"))
    fl = " ".join(files).lower()
    stack = []
    def _add(label, cond):
        if cond and label not in stack:
            stack.append(label)
    _add("Next.js", "next.config" in fl or '"next"' in deps)
    _add("React", '"react"' in deps or ".tsx" in fl or ".jsx" in fl)
    _add("Vue", '"vue"' in deps or ".vue" in fl)
    _add("Svelte", '"svelte"' in deps or ".svelte" in fl)
    _add("TypeScript", ".ts" in fl or ".tsx" in fl or "tsconfig.json" in fl)
    _add("JavaScript", ".js" in fl or ".jsx" in fl)
    _add("Tailwind", "tailwind.config" in fl or "tailwindcss" in deps)
    _add("Prisma", ".prisma" in fl)
    _add("Supabase", "supabase" in fl or "@supabase" in deps)
    _add("Python", ".py" in fl)
    _add("SQL", ".sql" in fl)

    # ---- auto-detection: used when config does not provide milestones/phases/features ----
    fnames = [os.path.basename(f).lower() for f in files]
    lp = ["/" + f.lower() for f in files]
    has_docs = any(n.startswith("readme") for n in fnames) or any("/docs/" in p for p in lp)
    has_tests = any(("test" in p or "spec" in p or "__tests__" in p) for p in lp)
    has_ci = any("/.github/workflows/" in p for p in lp) or any(n in (".gitlab-ci.yml", ".travis.yml") for n in fnames)
    has_docker = any(n == "dockerfile" or n.startswith("docker-compose") for n in fnames)
    has_types = any(n == "tsconfig.json" for n in fnames)
    has_lint = any(n.startswith((".eslintrc", ".prettierrc", ".flake8", ".ruff")) or n in ("ruff.toml",) for n in fnames)
    tags_n = len([t for t in sh(repo, ["git", "tag"]).splitlines() if t])

    mdefs = proj.get("milestones")
    if not mdefs:
        mdefs = [
            {"name": "Repository initialized", "done": True},
            {"name": "Documentation", "done": bool(has_docs)},
            {"name": "Tests", "done": bool(has_tests)},
            {"name": "CI / CD", "done": bool(has_ci)},
            {"name": "Containerized", "done": bool(has_docker)},
            {"name": "Released (tags)", "done": tags_n > 0},
        ]
    ms, done = milestones(repo, ref, mdefs)
    total = len(ms)

    phases_out = proj.get("phases")
    if not phases_out:
        phases_out = [
            {"n": "Docs", "v": 100 if has_docs else 0},
            {"n": "Tests", "v": 100 if has_tests else 0},
            {"n": "CI / CD", "v": 100 if has_ci else 0},
            {"n": "Typed", "v": 100 if has_types else 0},
            {"n": "Lint / Format", "v": 100 if has_lint else 0},
            {"n": "Containerized", "v": 100 if has_docker else 0},
        ]

    features_out = proj.get("features")
    if not features_out:
        now = datetime.datetime.now().timestamp()
        features_out = []
        SKIP_DIR = {"styles", "db", "dist", "build", "public", "assets", "__tests__",
                    "node_modules", "vendor", "coverage", "scripts", "config", "static", "images", "img"}
        for d in dirs:
            nm = d["n"]
            if nm == "(root)" or nm.startswith(".") or nm in SKIP_DIR:
                continue
            ct = sh(repo, ["git", "log", "-1", "--format=%ct", ref] + ps + ["--", basep + nm])
            try:
                age = now - int(ct)
            except Exception:
                age = 9e18
            features_out.append({"n": nm, "s": "wip" if age < 14 * 86400 else "done"})
            if len(features_out) >= 12:
                break

    # planning / spec documents
    docs_list, seen_d = [], set()
    for f in files:
        fl = f.lower()
        b = os.path.basename(f)
        is_doc = fl.endswith((".md", ".mdx", ".rst", ".adoc")) or "/docs/" in ("/" + fl) or "spec" in fl or "plan" in fl
        if is_doc and b.lower() not in seen_d:
            seen_d.add(b.lower())
            tag = "SPEC" if "spec" in fl else ("PLAN" if "plan" in fl else ("SQL" if fl.endswith(".sql") else "DOC"))
            docs_list.append({"n": b, "t": tag})
        if len(docs_list) >= 14:
            break
    docs_out = proj.get("docs") or docs_list
    # UI screens / routes
    PAGE_DIRS = ("/pages/", "/app/", "/screens/", "/routes/", "/views/")
    scr, seen_s = [], set()
    for f in files:
        fl = f.lower()
        b = os.path.basename(f).rsplit(".", 1)[0]
        is_page = fl.endswith((".html", ".htm")) or (
            any(s in ("/" + fl) for s in PAGE_DIRS) and fl.endswith((".tsx", ".jsx", ".vue", ".svelte", ".ts", ".js")))
        if is_page and b.lower() not in ("index", "layout", "_app", "_document") and b.lower() not in seen_s:
            seen_s.add(b.lower())
            scr.append(b)
        if len(scr) >= 24:
            break
    screens_out = proj.get("screens") or scr

    gh = [["Total commits", total_commits], ["Branches", str(branches_n)], ["Contributors", str(contrib)],
          ["Pages/views", str(pages)], ["DB tables", str(dbt)], ["Tracked files", str(len(files))],
          ["TODO / FIXME", str(todo)], ["Last activity", last]]

    return {
        "project": proj.get("name"), "tagline": proj.get("tagline") or CFG.get("tagline", "MISSION CONTROL"),
        "theme": proj.get("theme") or CFG.get("theme", "cyan"), "panels": proj.get("panels") or CFG.get("panels", {}),
        "repo": repo, "ref": ref, "configured": proj.get("configured", False),
        "asOf": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": total, "done": done, "milestones": ms, "commits": commits, "gh": gh,
        "timeline": timeline, "areas": area_list, "todo": int(todo or 0), "branches": branches,
        "recs": proj.get("recs", CFG.get("recs", [])), "pages": pages, "files": len(files),
        "dbt": dbt, "phases": phases_out, "features": features_out,
        "agents": proj.get("agents", []), "langs": langs, "dirs": dirs,
        "contributors": contributors, "stack": stack,
        "docsList": docs_out, "screens": screens_out,
    }


def projects_list():
    items, tot = [], 0
    for p in PROJECTS:
        psub = (["--", p["subpath"]] if p.get("subpath") else [])
        c = int(sh(p["repo"], ["git", "rev-list", "--count", "HEAD"] + psub, timeout=8) or 0)
        tot += c
        items.append({"id": pid_of(p), "name": p["name"], "branch": p.get("branch") or "",
                      "configured": p.get("configured", False), "commits": c})
    return {"projects": items, "totals": {"projects": len(items), "commits": tot}}


def overview_item(proj):
    """Light per-project rollup for the aggregate dashboard (few git calls)."""
    repo = proj["repo"]
    sub = proj.get("subpath")
    ps = (["--", sub] if sub else [])
    ref = resolve_ref(proj)
    commits = int(sh(repo, ["git", "rev-list", "--count", ref] + ps, timeout=8) or 0)
    last = sh(repo, ["git", "log", "-1", "--format=%cr", ref] + ps, timeout=8) or "-"
    mdefs = proj.get("milestones") or []
    if mdefs:
        ms, done = milestones(repo, ref, mdefs)
        total = len(ms)
    else:
        total, done = 0, 0
    feats = proj.get("features") or []
    fd = sum(1 for f in feats if f.get("s") == "done")
    fw = sum(1 for f in feats if f.get("s") == "wip")
    ftd = sum(1 for f in feats if f.get("s") == "todo")
    ags = proj.get("agents") or []
    arun = sum(1 for a in ags if a.get("s") == "run")
    phs = proj.get("phases") or []
    pavg = round(sum(p.get("v", 0) for p in phs) / len(phs)) if phs else None
    prog = round(done / total * 100) if total else pavg
    # extra dev metrics (cheap-ish; only for configured projects)
    files = [x for x in sh(repo, ["git", "ls-files"] + ps, timeout=8).splitlines() if x]
    db = 0
    db_names = []
    for f in files:
        fl = f.lower()
        if fl.endswith(".sql"):
            nm = re.findall(r"(?im)^\s*create\s+table\s+(?:if\s+not\s+exists\s+)?[\"'`\[]?([A-Za-z0-9_\.]+)", _read(repo, f))
            db += len(nm); db_names += [n.split(".")[-1] for n in nm]
        elif fl.endswith(".prisma"):
            nm = re.findall(r"(?im)^\s*model\s+([A-Za-z0-9_]+)", _read(repo, f))
            db += len(nm); db_names += nm
    # module structure (top-level dirs), file-type mix, screens, docs — real names for the dev log
    basep = (sub.rstrip("/") + "/") if sub else ""
    dirc = collections.Counter()
    for f in files:
        rel = f[len(basep):] if basep and f.startswith(basep) else f
        dirc[rel.split("/")[0] if "/" in rel else "(root)"] += 1
    dirs = [{"n": k, "v": v} for k, v in dirc.most_common(10)]
    extc = collections.Counter(os.path.splitext(f)[1].lower().lstrip(".") or "other" for f in files)
    langs = [{"n": k, "v": v} for k, v in extc.most_common(8)]
    PGD = ("/pages/", "/app/", "/screens/", "/routes/", "/views/")
    scr = []
    for f in files:
        lf = "/" + f.lower()
        for d in PGD:
            if d in lf:
                seg = f.lower().split(d.strip("/") + "/", 1)[-1].split("/")[0]
                seg = os.path.splitext(seg)[0]
                if seg and seg not in ("index", "_app", "layout") and seg not in scr:
                    scr.append(seg)
                break
    doc_names = [os.path.basename(f) for f in files if f.lower().endswith((".md", ".mdx"))][:12]
    c7 = int(sh(repo, ["git", "rev-list", "--count", "--since=7.days", ref] + ps, timeout=8) or 0)
    # 7-day daily commit sparkline
    raw = sh(repo, ["git", "log", ref, "--since=7.days", "--date=format:%Y-%m-%d", "--pretty=%cd"] + ps, timeout=8)
    cnt = collections.Counter(raw.splitlines())
    today = datetime.date.today()
    spark = [cnt.get((today - datetime.timedelta(days=i)).strftime("%Y-%m-%d"), 0) for i in range(6, -1, -1)]
    # real feature-completion (planned features: done vs total) — distinct from the
    # git-maturity `progress` estimate. None when no features are defined.
    feat_total = fd + fw + ftd
    feat_prog = round(fd / feat_total * 100) if feat_total else None
    return {
        "id": pid_of(proj), "name": proj.get("name"), "tagline": proj.get("tagline") or "",
        "theme": proj.get("theme") or CFG.get("theme", "cyan"),
        "commits": commits, "last": last, "progress": prog,
        "featProgress": feat_prog, "featDone": fd, "featTotal": feat_total,
        "ms": {"done": done, "total": total},
        "feat": {"done": fd, "wip": fw, "todo": ftd},
        "agents": {"run": arun, "total": len(ags)},
        "phases": phs, "db": db, "files": len(files), "commits7d": c7, "spark": spark,
        "dbNames": db_names[:14], "dirs": dirs, "langs": langs,
        "screens": scr[:14], "docNames": doc_names,
    }


def overview():
    cfg_items, others = [], []
    tot_c = prog_sum = prog_n = FD = FW = FT = ARUN = 0
    DB = FILES = C7 = AG_TOT = 0
    spark7 = [0, 0, 0, 0, 0, 0, 0]
    for p in PROJECTS:
        if p.get("configured"):
            it = overview_item(p)
            cfg_items.append(it)
            tot_c += it["commits"]
            if it["progress"] is not None:
                prog_sum += it["progress"]
                prog_n += 1
            FD += it["feat"]["done"]
            FW += it["feat"]["wip"]
            FT += it["feat"]["todo"]
            ARUN += it["agents"]["run"]
            AG_TOT += it["agents"]["total"]
            DB += it.get("db", 0)
            FILES += it.get("files", 0)
            C7 += it.get("commits7d", 0)
            for i, v in enumerate(it.get("spark", [])):
                if i < 7:
                    spark7[i] += v
        else:
            repo = p["repo"]
            sub = p.get("subpath")
            ps = (["--", sub] if sub else [])
            c = int(sh(repo, ["git", "rev-list", "--count", "HEAD"] + ps, timeout=8) or 0)
            l = sh(repo, ["git", "log", "-1", "--format=%cr"] + ps, timeout=8) or "-"
            tot_c += c
            others.append({"id": pid_of(p), "name": p["name"], "commits": c, "last": l})
    feat_all = FD + FW + FT
    totals = {"projects": len(cfg_items), "repos": len(PROJECTS), "commits": tot_c,
              "avgProgress": round(prog_sum / prog_n) if prog_n else 0,
              "featProgress": round(FD / feat_all * 100) if feat_all else None,
              "featDone": FD, "featTotal": feat_all,
              "feat": {"done": FD, "wip": FW, "todo": FT}, "agentsRun": ARUN, "agentsTotal": AG_TOT,
              "db": DB, "files": FILES, "commits7d": C7, "inProgress": FW, "spark7": spark7}
    # real dev-artifact log lines (DB tables, module structure, screens, docs, stack) for the console stream
    devlog = []
    for it in cfg_items:
        pn = it["name"]
        for n in it.get("dbNames", []):
            devlog.append({"k": "DB", "t": n, "p": pn})
        for d in it.get("dirs", [])[:6]:
            if d["n"] != "(root)":
                devlog.append({"k": "DIR", "t": "%s/ · %d" % (d["n"], d["v"]), "p": pn})
        for s in it.get("screens", [])[:8]:
            devlog.append({"k": "SCREEN", "t": s, "p": pn})
        for d in it.get("docNames", [])[:6]:
            devlog.append({"k": "DOC", "t": d, "p": pn})
        for l in it.get("langs", [])[:5]:
            devlog.append({"k": "LANG", "t": "%s · %d" % (l["n"], l["v"]), "p": pn})
        for f in (it.get("feat") and []):
            pass
    # per-ring breakdown so metric tiles can cycle project-by-project, then show the aggregate
    metric_break = {
        "db": [{"n": it["name"], "v": it.get("db", 0)} for it in cfg_items],
        "c7": [{"n": it["name"], "v": it.get("commits7d", 0)} for it in cfg_items],
        "wip": [{"n": it["name"], "v": it["feat"]["wip"]} for it in cfg_items],
        "ag": [{"n": it["name"], "v": it["agents"]["run"]} for it in cfg_items],
    }
    return {"projects": cfg_items, "others": others, "totals": totals,
            "devlog": devlog, "metricBreak": metric_break,
            "theme": CFG.get("theme", "cyan"),
            "asOf": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---- auto dev report + next actions (real git/config signals only) ----
_report_cache = {"ts": 0, "data": None}


def project_report(proj):
    repo = proj["repo"]
    sub = proj.get("subpath")
    ps = (["--", sub] if sub else [])
    ref = resolve_ref(proj)
    today_n = int(sh(repo, ["git", "rev-list", "--count", "--since=midnight", ref] + ps, timeout=8) or 0)
    week_n = int(sh(repo, ["git", "rev-list", "--count", "--since=7.days", ref] + ps, timeout=8) or 0)
    commits = []
    for x in sh(repo, ["git", "log", ref, "--since=7.days", "--pretty=%h\x1f%s\x1f%cr", "-15"] + ps, timeout=8).splitlines():
        parts = x.split("\x1f")
        if len(parts) >= 3:
            commits.append({"sha": parts[0], "msg": parts[1][:80], "when": parts[2]})
    # files changed in the last 7 days (real)
    fch = collections.Counter()
    for l in sh(repo, ["git", "log", ref, "--since=7.days", "--name-only", "--pretty=format:"] + ps, timeout=8).splitlines():
        l = l.strip()
        if l:
            fch[l] += 1
    files_changed = [{"f": os.path.basename(k), "path": k, "n": v} for k, v in fch.most_common(8)]
    # open TODO/FIXME with location (real)
    todos = []
    for l in sh(repo, ["bash", "-lc", "git grep -n -I -E '(TODO|FIXME|HACK)' 2>/dev/null | head -14"], timeout=8).splitlines():
        parts = l.split(":", 2)
        if len(parts) >= 3:
            todos.append({"file": os.path.basename(parts[0]), "line": parts[1], "txt": parts[2].strip()[:70]})
    feats = proj.get("features") or []
    f_wip = [f.get("n") for f in feats if f.get("s") == "wip"]
    f_todo = [f.get("n") for f in feats if f.get("s") == "todo"]
    f_done = [f.get("n") for f in feats if f.get("s") == "done"]
    ms = proj.get("milestones") or []
    ms_open = [m.get("name") for m in ms if not m.get("done")]
    # next actions — surfaced ONLY from real signals, never invented
    nxt = []
    for n in f_wip:
        nxt.append({"t": n, "why": "진행 중인 기능 마무리", "k": "wip"})
    for n in f_todo[:4]:
        nxt.append({"t": n, "why": "계획된 미착수 기능", "k": "todo"})
    for td in todos[:4]:
        nxt.append({"t": td["txt"], "why": "코드 내 TODO · %s:%s" % (td["file"], td["line"]), "k": "code"})
    for n in ms_open[:2]:
        nxt.append({"t": n, "why": "미완료 마일스톤", "k": "ms"})
    ft = len(feats)
    # ---- risk detection (real git/config signals only; never invented) ----
    ct = sh(repo, ["git", "log", "-1", "--format=%ct", ref] + ps, timeout=8).strip()
    days_since = int((time.time() - int(ct)) / 86400) if ct.isdigit() else None
    reverts = int(sh(repo, ["bash", "-lc", "git log --since=7.days -i --grep=revert --oneline 2>/dev/null | wc -l | tr -d ' '"], timeout=8) or 0)
    allfiles = [x for x in sh(repo, ["git", "ls-files"] + ps, timeout=8).splitlines() if x]
    has_tests = any(re.search(r"(^|/)(tests?|__tests__|spec)/|\.(test|spec)\.[a-zA-Z]+$", f) for f in allfiles)
    risks = []
    if f_wip and days_since is not None and days_since >= 7:
        risks.append({"level": "high", "kind": "stale", "n": days_since, "wip": len(f_wip),
                      "msg": "장기 정체 · 진행중 기능 %d개" % len(f_wip), "ev": "마지막 커밋 %d일 전" % days_since})
    if reverts:
        risks.append({"level": "high" if reverts >= 3 else "med", "kind": "revert", "n": reverts,
                      "msg": "되돌림(revert) %d건" % reverts, "ev": "최근 7일 커밋"})
    # churn hotspots — top 3 only, so other risk kinds stay visible
    for fc in [f for f in files_changed if f["n"] >= 4][:3]:
        risks.append({"level": "med", "kind": "churn", "f": fc["f"], "n": fc["n"],
                      "msg": "잦은 수정: %s" % fc["f"], "ev": "7일간 %d회 변경" % fc["n"]})
    if allfiles and not has_tests:
        risks.append({"level": "med", "kind": "notest",
                      "msg": "테스트 파일 없음", "ev": "test/spec 파일 미발견"})
    if len(todos) >= 15:
        risks.append({"level": "low", "kind": "todo", "n": len(todos),
                      "msg": "열린 TODO %d개" % len(todos), "ev": "git grep TODO/FIXME"})
    _lv = {"high": 0, "med": 1, "low": 2}
    risks.sort(key=lambda r: _lv.get(r["level"], 3))
    risks = risks[:6]
    return {
        "id": pid_of(proj), "name": proj.get("name"),
        "commitsToday": today_n, "commits7d": week_n, "commits": commits[:8],
        "filesChanged": files_changed, "todos": todos[:10], "todoCount": len(todos),
        "features": {"done": f_done, "wip": f_wip, "todo": f_todo},
        "featProgress": round(len(f_done) / ft * 100) if ft else None,
        "milestonesOpen": ms_open, "next": nxt[:8],
        "risks": risks, "daysSinceCommit": days_since, "reverts": reverts, "hasTests": has_tests,
    }


def report():
    now = time.time()
    if _report_cache["data"] and now - _report_cache["ts"] < 120:
        return _report_cache["data"]
    items = [project_report(p) for p in PROJECTS if p.get("configured")]
    tot_today = sum(i["commitsToday"] for i in items)
    tot_week = sum(i["commits7d"] for i in items)
    tot_todo = sum(i["todoCount"] for i in items)
    n_risk = sum(len(i.get("risks") or []) for i in items)
    n_high = sum(1 for i in items for r in (i.get("risks") or []) if r.get("level") == "high")
    try:
        errs = claude_errors()
    except Exception:
        errs = 0
    out = {"projects": items,
           "totals": {"commitsToday": tot_today, "commits7d": tot_week, "openTodos": tot_todo,
                      "nextCount": sum(len(i["next"]) for i in items),
                      "risks": n_risk, "risksHigh": n_high, "claudeErrors": errs},
           "asOf": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "day": datetime.date.today().strftime("%Y-%m-%d")}
    _report_cache.update(ts=now, data=out)
    return out


# ---- real Claude Code usage (local session logs, token counts) ----
import glob as _glob
CLAUDE_PROJECTS = os.path.expanduser(os.environ.get("MC_CLAUDE_PROJECTS") or CFG.get("claudeProjectsDir") or "~/.claude/projects")
_usage_cache = {}   # filepath -> (mtime, {in,out,cache,msgs,last})
_errors_cache = {"ts": 0, "n": 0}


def claude_errors():
    """Count real error records (API errors / failed tool results) in recent local logs."""
    now = time.time()
    if now - _errors_cache["ts"] < 120:
        return _errors_cache["n"]
    n = 0
    cutoff = now - 7 * 86400
    if os.path.isdir(CLAUDE_PROJECTS):
        for fp in _glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")):
            try:
                if os.path.getmtime(fp) < cutoff:
                    continue
                with open(fp, encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        if '"isApiErrorMessage":true' in line or '"is_error":true' in line:
                            n += 1
            except Exception:
                continue
    _errors_cache.update(ts=now, n=n)
    return n


def _scan_usage_file(fp):
    agg = {"in": 0, "out": 0, "cache": 0, "msgs": 0}
    try:
        with open(fp, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                u = (o.get("message") or {}).get("usage") or o.get("usage")
                if isinstance(u, dict):
                    agg["in"] += u.get("input_tokens", 0)
                    agg["out"] += u.get("output_tokens", 0)
                    agg["cache"] += u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                    agg["msgs"] += 1
    except Exception:
        pass
    return agg


def claude_usage():
    items, tin, tout, tcache, tsess = [], 0, 0, 0, 0
    if os.path.isdir(CLAUDE_PROJECTS):
        for d in sorted(_glob.glob(os.path.join(CLAUDE_PROJECTS, "*"))):
            if not os.path.isdir(d):
                continue
            files = _glob.glob(os.path.join(d, "*.jsonl"))
            if not files:
                continue
            ai = ao = ac = am = 0
            last = 0.0
            for fp in files:
                mt = os.path.getmtime(fp)
                c = _usage_cache.get(fp)
                if not c or c[0] != mt:
                    c = (mt, _scan_usage_file(fp))
                    _usage_cache[fp] = c
                a = c[1]
                ai += a["in"]; ao += a["out"]; ac += a["cache"]; am += a["msgs"]
                last = max(last, mt)
            name = os.path.basename(d)
            label = (name.replace("-Users-" + os.path.basename(os.path.expanduser("~")), "~").replace("-", "/")) or name
            items.append({"name": label, "sessions": len(files), "msgs": am,
                          "input": ai, "output": ao, "cache": ac,
                          "last": datetime.datetime.fromtimestamp(last).strftime("%Y-%m-%d") if last else "-",
                          "lastTs": last})
            tin += ai; tout += ao; tcache += ac; tsess += len(files)
    items.sort(key=lambda x: -x["lastTs"])
    return {"projects": items, "totals": {"input": tin, "output": tout, "cache": tcache, "sessions": tsess, "projects": len(items)},
            "asOf": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---- real Claude Code activity (what's being worked on, from local logs) ----
# Reads your own session logs on 127.0.0.1 only. Topic text can be hidden with
# config "activityTopics": false (actions/files still show).
_activity_cache = {}   # filepath -> (mtime, summary)
_ACT_HASHTOOL = re.compile(r"^[0-9a-f]{12,}\s+toolu_")
_ACT_SHOW_TOPICS = CFG.get("activityTopics", True)


def _act_clean(txt):
    if not isinstance(txt, str):
        return ""
    txt = re.sub(r"<pasted_content[^>]*>.*?</pasted_content>", " ", txt, flags=re.S)
    txt = re.sub(r"<system-reminder>.*?</system-reminder>", " ", txt, flags=re.S)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


_ACT_SKIP = ("Called the", "Result of", "This session is being continued",
             "/login", "/clear", "/compact", "Login interrupted", "Caveat:",
             "The command below", "The user sent", "The user opened", "The user", "<command",
             "This is how Claude Code", "DO NOT respond", "Do NOT respond",
             "<local-command", "The following", "Your task is", "[Request interrupted",
             "Your response above", "Resume directly", "Continue the conversation",
             "The assistant", "System:", "<system")
_ACT_SKIP_SUB = ("was run directly in Claude Code", "not sent to you as a request",
                 "surfaces messages the user sends", "<command-name>", "<command-message>",
                 "cut off mid-stream", "ran out of context", "no apology, no recap")


def _act_is_prompt(t):
    if len(t) < 2 or _ACT_HASHTOOL.match(t):
        return False
    if t.startswith(_ACT_SKIP):
        return False
    for s in _ACT_SKIP_SUB:
        if s in t:
            return False
    if re.match(r"^[0-9a-f/_.\-]{20,}$", t):
        return False
    return True


def _act_verb(name, inp):
    inp = inp or {}
    b = os.path.basename
    if name == "Edit":
        return "edit", b(inp.get("file_path", ""))
    if name == "Write":
        return "write", b(inp.get("file_path", ""))
    if name == "Read":
        return "read", b(inp.get("file_path", ""))
    if name == "Bash":
        return "run", (inp.get("description") or inp.get("command", "")).strip()[:52]
    if name.startswith(("mcp__Claude_Browser", "mcp__claude-in-chrome")):
        return "browser", ""
    if name.startswith("mcp__"):
        return "tool", name.split("__")[-1]
    return "tool", name


def _edit_delta(name, inp):
    # Real added/removed line counts from the tool input (no secrets, just counts).
    inp = inp or {}
    if name == "Write":
        c = inp.get("content", "")
        return (c.count("\n") + (1 if c else 0), 0)
    if name == "Edit":
        ns, os_ = inp.get("new_string", ""), inp.get("old_string", "")
        la = ns.count("\n") + (1 if ns else 0)
        lr = os_.count("\n") + (1 if os_ else 0)
        return (la, lr)
    return (0, 0)


def _scan_activity_file(fp):
    prompts, actions, changes = [], [], []
    files = {}            # basename -> [added, removed, edits]
    last = None
    title = None
    cwd = None
    try:
        with open(fp, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("cwd"):
                    cwd = o["cwd"]
                if o.get("customTitle"):
                    title = o["customTitle"]
                ts = o.get("timestamp")
                if ts:
                    last = ts
                m = o.get("message")
                if not isinstance(m, dict):
                    continue
                if o.get("type") == "user" and m.get("role") == "user" and isinstance(m.get("content"), str):
                    t = _act_clean(m["content"])
                    if _act_is_prompt(t):
                        prompts.append((ts, t[:80]))
                elif o.get("type") == "assistant" and isinstance(m.get("content"), list):
                    for blk in m["content"]:
                        if isinstance(blk, dict) and blk.get("type") == "tool_use":
                            nm = blk.get("name")
                            inp = blk.get("input")
                            v, tgt = _act_verb(nm, inp)
                            actions.append((ts, v, tgt))
                            if nm in ("Edit", "Write"):
                                fn = os.path.basename((inp or {}).get("file_path", "")) or "?"
                                la, lr = _edit_delta(nm, inp)
                                rec = files.setdefault(fn, [0, 0, 0])
                                rec[0] += la; rec[1] += lr; rec[2] += 1
                                changes.append((ts, fn, la, lr))
    except Exception:
        pass
    return {"last": last, "title": title, "cwd": cwd,
            "prompts": prompts[-6:], "actions": actions[-14:],
            "nPrompts": len(prompts), "nActions": len(actions),
            "files": files, "changes": changes[-18:]}


def _act_label(cwd):
    if not cwd:
        return "?"
    return os.path.basename(cwd.rstrip("/")) or cwd


def claude_activity():
    sessions = []
    if os.path.isdir(CLAUDE_PROJECTS):
        for fp in _glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")):
            try:
                mt = os.path.getmtime(fp)
            except OSError:
                continue
            c = _activity_cache.get(fp)
            if not c or c[0] != mt:
                c = (mt, _scan_activity_file(fp))
                _activity_cache[fp] = c
            s = c[1]
            if not s["last"]:
                continue
            proj = _act_label(s["cwd"])
            topics = [t for _, t in s["prompts"][-3:]] if _ACT_SHOW_TOPICS else []
            fdict = s.get("files", {})
            flist = sorted(([fn] + rec for fn, rec in fdict.items()),
                           key=lambda r: -(r[1] + r[2]))
            top_files = [{"f": r[0], "add": r[1], "rem": r[2], "n": r[3]} for r in flist[:6]]
            lines_added = sum(r[1] for r in flist)
            lines_removed = sum(r[2] for r in flist)
            # estimated session state (by recency of last logged event — labeled as inferred)
            state = "idle"
            try:
                age = time.time() - datetime.datetime.fromisoformat(s["last"].replace("Z", "+00:00")).timestamp()
                if age < 180:
                    state = "active"
                elif age < 1800:
                    state = "recent"
            except Exception:
                age = None
            sessions.append({
                "title": s["title"] or proj, "proj": proj, "cwd": s["cwd"],
                "last": s["last"], "nActions": s["nActions"], "nPrompts": s["nPrompts"],
                "state": state,
                "topic": topics[-1] if topics else "", "topics": topics,
                "actions": [{"ts": ts, "v": v, "t": tgt} for ts, v, tgt in s["actions"]],
                "files": top_files, "nFiles": len(fdict),
                "linesAdded": lines_added, "linesRemoved": lines_removed,
                "changes": [{"ts": ts, "f": fn, "add": la, "rem": lr} for ts, fn, la, lr in s.get("changes", [])],
            })
    sessions.sort(key=lambda s: s["last"], reverse=True)
    feed = []
    for s in sessions:
        for a in s["actions"]:
            feed.append((a["ts"], s["proj"], a["v"], a["t"]))
    feed.sort(reverse=True)
    feed = [{"ts": ts, "proj": p, "v": v, "t": t} for ts, p, v, t in feed[:16]]
    # global file-change stream (newest-first) — the "+144" view, real line counts
    chg = []
    for s in sessions:
        for c in s.get("changes", []):
            chg.append((c["ts"], s["proj"], c["f"], c["add"], c["rem"]))
    chg.sort(reverse=True)
    changes = [{"ts": ts, "proj": p, "f": f, "add": a, "rem": r} for ts, p, f, a, r in chg[:24]]
    return {"sessions": sessions[:8], "feed": feed, "changes": changes,
            "showTopics": bool(_ACT_SHOW_TOPICS),
            "asOf": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---- Anthropic API usage/cost (official Admin report; key stays local) ----
_apiusage_cache = {"ts": 0, "data": None}


def _admin_key():
    # Admin key is never committed: read from env or the git-ignored config.json.
    return (os.environ.get("ANTHROPIC_ADMIN_KEY") or CFG.get("anthropicAdminKey") or "").strip()


def _sum_tokens(obj, acc):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (int, float)) and k.endswith("_tokens"):
                if "output" in k:
                    acc["output"] += v
                elif "cache" in k:
                    acc["cache"] += v
                elif "input" in k:
                    acc["input"] += v
            else:
                _sum_tokens(v, acc)
    elif isinstance(obj, list):
        for x in obj:
            _sum_tokens(x, acc)


def _sum_cost(obj, acc):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("amount", "amount_cents", "cost") and isinstance(v, (int, float, str)):
                try:
                    acc[0] += float(v)
                except Exception:
                    pass
            else:
                _sum_cost(v, acc)
    elif isinstance(obj, list):
        for x in obj:
            _sum_cost(x, acc)


def _anthropic_get(path, params, key):
    url = "https://api.anthropic.com" + path + "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def api_usage():
    key = _admin_key()
    if not key:
        return {"configured": False}
    now = time.time()
    if _apiusage_cache["data"] and now - _apiusage_cache["ts"] < 300:
        return _apiusage_cache["data"]
    start = (datetime.date.today() - datetime.timedelta(days=7)).isoformat() + "T00:00:00Z"
    out = {"configured": True, "input": 0, "output": 0, "cache": 0, "cost": None,
           "asOf": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        acc = {"input": 0, "output": 0, "cache": 0}
        u = _anthropic_get("/v1/organizations/usage_report/messages",
                           {"starting_at": start, "bucket_width": "1d", "limit": 31}, key)
        _sum_tokens(u, acc)
        out.update(acc)
        try:
            c = _anthropic_get("/v1/organizations/cost_report",
                               {"starting_at": start, "bucket_width": "1d", "limit": 31}, key)
            cacc = [0.0]
            _sum_cost(c, cacc)
            out["cost"] = round(cacc[0] / 100.0, 2)  # amounts are in cents
        except Exception:
            out["cost"] = None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:160]
        out = {"configured": True, "error": "HTTP %s · %s" % (e.code, body)}
    except Exception as e:
        out = {"configured": True, "error": str(e)[:160]}
    _apiusage_cache.update(ts=now, data=out)
    return out


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
        if u.path == "/api/overview":
            try:
                return self._send(200, json.dumps(overview(), ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}), "application/json")
        if u.path == "/api/report":
            try:
                return self._send(200, json.dumps(report(), ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}), "application/json")
        if u.path == "/api/api-usage":
            try:
                return self._send(200, json.dumps(api_usage(), ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}), "application/json")
        if u.path == "/api/claude-usage":
            try:
                return self._send(200, json.dumps(claude_usage(), ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}), "application/json")
        if u.path == "/api/activity":
            try:
                return self._send(200, json.dumps(claude_activity(), ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}), "application/json")
        if u.path == "/api/my-projects":
            items = enrich_board(load_board())
            tot = len(items)
            act = sum(1 for it in items if it.get("status") == "active")
            done = sum(1 for it in items if it.get("status") == "done")
            recent = sum(1 for it in items if (it.get("daysAgo") is not None and it["daysAgo"] <= 7))
            avgAct = round(sum(it.get("activity") or 0 for it in items) / tot) if tot else 0
            return self._send(200, json.dumps({"enabled": board_enabled(), "projects": items, "totals": {"total": tot, "active": act, "done": done, "recent": recent, "avgActivity": avgAct}}, ensure_ascii=False), "application/json; charset=utf-8")
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

    def do_POST(self):
        # Local-only writes (server binds 127.0.0.1): manage the "My Projects" board.
        u = urllib.parse.urlparse(self.path)
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}") if ln else {}
        except Exception:
            return self._send(400, json.dumps({"error": "bad request body"}), "application/json")

        def err(code, msg):
            return self._send(code, json.dumps({"error": msg}, ensure_ascii=False), "application/json; charset=utf-8")

        def ok_json(d):
            return self._send(200, json.dumps(d, ensure_ascii=False), "application/json; charset=utf-8")

        def _int(v):
            try:
                return max(0, int(v or 0))
            except Exception:
                return 0

        def clean(it):
            name = (it.get("name") or "").strip()
            st = it.get("status") if it.get("status") in STATUSES else "active"
            try:
                pr = max(0, min(100, int(it.get("progress") or 0)))
            except Exception:
                pr = 0
            return {
                "name": name,
                "desc": (it.get("desc") or "").strip(),
                "status": st,
                "progress": pr,
                "updated": (it.get("updated") or "").strip(),
                "tag": (it.get("tag") or "").strip(),
                "task": (it.get("task") or "").strip(),      # current work in progress
                "eta": (it.get("eta") or "").strip(),        # expected completion
                "convos": _int(it.get("convos")),            # number of conversations
                "docs": _int(it.get("docs")),                # attached documents
            }

        if u.path == "/api/add-project":
            ent = clean(body)
            if not ent["name"]:
                return err(400, "이름을 입력하세요")
            ent["id"] = _board_id()
            with _board_lock:
                items = load_board()
                items.insert(0, ent)
                save_board(items)
            return ok_json({"ok": True, "id": ent["id"]})

        if u.path == "/api/update-project":
            pid = (body.get("id") or "").strip()
            with _board_lock:
                items = load_board()
                found = False
                for it in items:
                    if it.get("id") == pid:
                        it.update({k: v for k, v in clean(body).items()})
                        it["id"] = pid
                        found = True
                        break
                if not found:
                    return err(404, "프로젝트를 찾을 수 없습니다")
                save_board(items)
            return ok_json({"ok": True})

        if u.path == "/api/remove-project":
            pid = (body.get("id") or "").strip()
            with _board_lock:
                items = load_board()
                kept = [it for it in items if it.get("id") != pid]
                if len(kept) == len(items):
                    return err(404, "프로젝트를 찾을 수 없습니다")
                save_board(kept)
            return ok_json({"ok": True})

        return err(404, "not found")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Mission Control — DevHUD on http://127.0.0.1:{PORT}  ({len(PROJECTS)} repos)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
