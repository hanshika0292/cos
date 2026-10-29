#!/usr/bin/env python3
"""cos — a tiny chief of staff for many parallel coding-agent sessions.

Data sources (no LLM involved):
  * Agent hooks (Claude Code, Gemini CLI; Codex via notify) -> ~/.cos/events.jsonl
  * Claude's own live session files  <config>/sessions/<pid>.json
  * Remote VMs over ssh/gcloud (`cos pull`), which run this same file in `export` mode
  * Window snapshots (browser tabs, terminal tabs, app windows) via snap.js
The only LLM call is `cos brief` (claude -p --model haiku).

Stdlib only; keep it Python 3.6+ compatible (VMs may be old).
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
COS = os.environ.get("COS_HOME", os.path.join(HOME, ".cos"))
EVENTS = os.path.join(COS, "events.jsonl")
DB = os.path.join(COS, "cos.db")
BRIEFS = os.path.join(COS, "briefs")
CONFIG = os.path.join(COS, "config.json")
HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "hooks", "cos-hook.sh")
LAUNCHER = os.path.join(COS, "bin", "cos")       # written by install.sh; stable path for SwiftBar/launchd
HOST = socket.gethostname().split(".")[0]
REPO_URL = "https://github.com/hanshika0292/cos"

HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "PostToolUse", "Notification", "Stop", "SessionEnd"]
GEMINI_EVENTS = ["SessionStart", "BeforeAgent", "AfterTool", "Notification", "AfterAgent", "SessionEnd"]
# never record tabs/windows whose URL or title contains any of these (override with `cos config snap_exclude`)
DEFAULT_EXCLUDE = ["mail.google.com", "outlook.live.com", "outlook.office.com", "accounts.google.com",
                   "1password", "bitwarden", "lastpass", "paypal.com", "bank", "/login", "/signin"]
IDLE_AFTER = 2 * 3600          # "done" turns into "idle" after this long without activity
STALE_AFTER = 12 * 3600        # hook-only sessions with no live file are considered ended
TICK_EVERY = 300               # seconds between background ticks (VM pull + window snapshot)
ROTATE_AT = 20 * 1024 * 1024

STATE_ORDER = ["needs_you", "working", "done", "idle", "ended"]
STATE_LABEL = {"needs_you": "Needs you", "working": "Working", "done": "Your turn",
               "idle": "Idle", "ended": "Ended"}
STATE_ICON = {"needs_you": "⏳", "working": "⚙️", "done": "✅", "idle": "💤", "ended": "·"}


# ---------------------------------------------------------------- utils

def now():
    return int(time.time())


def today(offset=0):
    return (dt.date.today() + dt.timedelta(days=offset)).isoformat()


def age(ts):
    if not ts:
        return "?"
    s = max(0, now() - int(ts))
    if s < 60:
        return "<1m"
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 86400:
        return "%dh" % (s // 3600)
    return "%dd" % (s // 86400)


def clip(text, n):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    os.makedirs(COS, exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except Exception:
        return False


def config_dirs():
    dirs = [os.path.join(HOME, ".claude")]
    dirs += sorted(glob.glob(os.path.join(HOME, ".claude-profiles", "*")))
    dirs += load_config().get("config_dirs", [])
    seen, out = set(), []
    for d in dirs:
        d = os.path.abspath(os.path.expanduser(d))
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


_git_cache = {}


def stream_of(cwd, src):
    """A work stream is a git repo (or plain folder) on one machine."""
    if not cwd:
        return "unknown"
    if src != "local":
        return cwd
    if cwd not in _git_cache:
        root, d = cwd, cwd
        while d and d != "/":
            if os.path.exists(os.path.join(d, ".git")):
                root = d
                break
            d = os.path.dirname(d)
        _git_cache[cwd] = root
    return _git_cache[cwd]


def stream_name(stream, src):
    base = os.path.basename(stream.rstrip("/")) or stream
    return base if src == "local" else "%s@%s" % (base, src)


def osa(script):
    res = subprocess.run(["osascript", "-e", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         universal_newlines=True)
    return res.stdout.strip()


def app_running(proc):
    return subprocess.run(["pgrep", "-xq", proc]).returncode == 0


def ask(question, default=True, assume=None):
    """y/n prompt for `cos setup`; `assume` answers without asking (--yes)."""
    if assume is not None:
        return assume if default else False
    ans = input("%s [%s] " % (question, "Y/n" if default else "y/N")).strip().lower()
    return default if not ans else ans.startswith("y")


def mac_notify(title, msg):
    if sys.platform != "darwin":
        return
    q = lambda s: (s or "").replace("\\", "\\\\").replace('"', '\\"')
    osa('display notification "%s" with title "%s" sound name "Glass"' % (q(msg), q(title)))


def is_permission_wait(p):
    kind = (p.get("notification_type") or "").lower()
    msg = (p.get("message") or "").lower()
    if kind:
        return "idle" not in kind
    return "waiting for your input" not in msg


# ---------------------------------------------------------------- db

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, src TEXT, host TEXT, profile TEXT, cwd TEXT, stream TEXT,
  name TEXT, title TEXT, state TEXT, needs_msg TEXT, live TEXT, pid INTEGER,
  term TEXT, iterm TEXT, orca TEXT, transcript TEXT, last_prompt TEXT, last_reply TEXT,
  started INTEGER, act_ts INTEGER, last_ts INTEGER, hidden INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS prompts(sid TEXT, ts INTEGER, text TEXT);
CREATE INDEX IF NOT EXISTS prompts_sid ON prompts(sid, ts);
CREATE TABLE IF NOT EXISTS todos(
  id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, due TEXT, stream TEXT,
  status TEXT DEFAULT 'open', source TEXT DEFAULT 'me', created INTEGER, done_at INTEGER);
CREATE TABLE IF NOT EXISTS offsets(file TEXT PRIMARY KEY, pos INTEGER);
CREATE TABLE IF NOT EXISTS remotes(name TEXT PRIMARY KEY, ssh TEXT, pos INTEGER DEFAULT 0,
  last_pull INTEGER DEFAULT 0, last_error TEXT);
CREATE TABLE IF NOT EXISTS windows(
  key TEXT PRIMARY KEY, kind TEXT, app TEXT, title TEXT, url TEXT, cwd TEXT,
  first_seen INTEGER, last_seen INTEGER, seen_n INTEGER DEFAULT 0, front_n INTEGER DEFAULT 0);
"""
MIGRATIONS = ["ALTER TABLE sessions ADD COLUMN seen_ts INTEGER",
              "ALTER TABLE sessions ADD COLUMN agent TEXT",
              "ALTER TABLE sessions ADD COLUMN tty TEXT",
              "ALTER TABLE sessions ADD COLUMN tmux TEXT"]


def db():
    os.makedirs(COS, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for m in MIGRATIONS:
        try:
            con.execute(m)
        except sqlite3.OperationalError:
            pass   # already applied
    return con


def upsert(con, sid, **cols):
    cols = {k: v for k, v in cols.items() if v not in (None, "")}
    row = con.execute("SELECT id FROM sessions WHERE id=?", (sid,)).fetchone()
    if not row:
        cols.setdefault("started", now())
        keys = ["id"] + list(cols)
        con.execute("INSERT INTO sessions(%s) VALUES(%s)" % (",".join(keys), ",".join("?" * len(keys))),
                    [sid] + list(cols.values()))
    elif cols:
        con.execute("UPDATE sessions SET %s WHERE id=?" % ",".join("%s=?" % k for k in cols),
                    list(cols.values()) + [sid])


# ---------------------------------------------------------------- ingest

def apply_event(con, e, src, notify=False):
    p = e.get("p") or {}
    sid = p.get("session_id")
    if not sid:
        return
    ev, ts = e.get("ev"), int(e.get("ts") or now())
    cwd = p.get("cwd")
    base = dict(src=src, host=e.get("host"), profile=e.get("profile"), last_ts=ts, agent=e.get("agent"),
                term=e.get("term"), iterm=e.get("iterm"), orca=e.get("orca"), tty=e.get("tty"),
                tmux=e.get("tmux"), transcript=p.get("transcript_path"))
    if cwd:
        base.update(cwd=cwd, stream=stream_of(cwd, src))
    if str(e.get("cpid") or "").isdigit() and src == "local":
        base["pid"] = int(e["cpid"])
    if ev == "SessionStart":
        upsert(con, sid, state="idle", **base)
    elif ev == "UserPromptSubmit":
        text = p.get("prompt") or ""
        row = con.execute("SELECT title FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row or not row["title"]:
            base["title"] = clip(text, 80)
        upsert(con, sid, state="working", act_ts=ts, last_prompt=clip(text, 2000), **base)
        con.execute("UPDATE sessions SET needs_msg=NULL WHERE id=?", (sid,))
        con.execute("INSERT INTO prompts VALUES(?,?,?)", (sid, ts, clip(text, 2000)))
    elif ev == "PostToolUse":
        upsert(con, sid, state="working", act_ts=ts, **base)
    elif ev == "Notification":
        if is_permission_wait(p):
            upsert(con, sid, state="needs_you", needs_msg=clip(p.get("message"), 200), **base)
            if notify and now() - ts < 600:
                notify_line(e, src)
        else:
            upsert(con, sid, **base)
    elif ev == "Stop":
        reply = p.get("last_assistant_message") or p.get("prompt_response")   # claude/codex | gemini
        upsert(con, sid, state="done", act_ts=ts, last_reply=clip(reply, 3000), **base)
        if notify and now() - ts < 600:
            notify_finished(con, sid, ts)
    elif ev == "SessionEnd":
        upsert(con, sid, state="ended", **base)
    else:
        upsert(con, sid, **base)


def ingest_text(con, text, src, notify=False):
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            apply_event(con, json.loads(line), src, notify)
        except Exception:
            continue  # a half-written or odd line never breaks the dashboard


def ingest_local(con):
    if not os.path.exists(EVENTS):
        return
    row = con.execute("SELECT pos FROM offsets WHERE file=?", (EVENTS,)).fetchone()
    pos = row["pos"] if row else 0
    size = os.path.getsize(EVENTS)
    if size < pos:
        pos = 0
    if size > pos:
        with open(EVENTS, "rb") as f:
            f.seek(pos)
            chunk = f.read()
        end = chunk.rfind(b"\n") + 1     # leave a partial trailing line for next time
        ingest_text(con, chunk[:end].decode("utf-8", "replace"), "local")
        pos += end
    if pos >= ROTATE_AT and pos == os.path.getsize(EVENTS):
        os.replace(EVENTS, EVENTS + ".1")
        pos = 0
    con.execute("INSERT OR REPLACE INTO offsets VALUES(?,?)", (EVENTS, pos))


def live_sessions():
    """Claude Code writes <config>/sessions/<pid>.json for every running session."""
    out = []
    for d in config_dirs():
        for f in glob.glob(os.path.join(d, "sessions", "*.json")):
            try:
                with open(f) as fh:
                    s = json.load(fh)
            except Exception:
                continue
            if s.get("pid") and s.get("sessionId") and pid_alive(s["pid"]):
                out.append(dict(sid=s["sessionId"], pid=s["pid"], cwd=s.get("cwd"), name=s.get("name"),
                                status=s.get("status"), profile=os.path.basename(d),
                                updated=int((s.get("updatedAt") or 0) / 1000) or None))
    return out


def apply_live(con, live, src, host):
    ids = set()
    for s in live:
        ids.add(s["sid"])
        upsert(con, s["sid"], src=src, host=host, pid=s["pid"] if src == "local" else None,
               cwd=s["cwd"], stream=stream_of(s["cwd"], src), name=s["name"], live=s["status"] or "idle",
               profile=s["profile"])
        if s.get("updated"):
            con.execute("UPDATE sessions SET last_ts=MAX(COALESCE(last_ts,0),?) WHERE id=?", (s["updated"], s["sid"]))
        con.execute("UPDATE sessions SET state='idle' WHERE id=? AND state IS NULL", (s["sid"],))
    # anything from this source that is no longer live has ended
    for r in con.execute("SELECT id, pid, last_ts FROM sessions WHERE src=? AND state!='ended'", (src,)).fetchall():
        if r["id"] in ids:
            continue
        gone = (r["pid"] and not pid_alive(r["pid"])) if src == "local" else True
        if gone or (not r["pid"] and now() - (r["last_ts"] or 0) > STALE_AFTER):
            con.execute("UPDATE sessions SET state='ended', live=NULL WHERE id=?", (r["id"],))


def refresh(con=None):
    con = con or db()
    with con:
        ingest_local(con)
        apply_live(con, live_sessions(), "local", HOST)
    return con


def state_of(s):
    if s["state"] == "ended":
        return "ended"
    if s["state"] == "needs_you":
        return "needs_you"
    live = (s["live"] or "").lower()
    if live in ("busy", "running", "working") or (not live and s["state"] == "working"):
        return "working"
    if live in ("waiting", "blocked", "permission", "input"):
        return "needs_you"
    last = s["act_ts"] or s["last_ts"] or 0
    if s["seen_ts"] and s["seen_ts"] >= last:
        return "idle"     # you already looked at it via cos
    return "done" if s["act_ts"] and now() - last < IDLE_AFTER else "idle"


def sessions(con, include_ended=False, since=None):
    rows = con.execute("SELECT * FROM sessions WHERE hidden=0").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["st"] = state_of(r)
        d["label"] = d["name"] or d["title"] or d["id"][:8]
        d["sname"] = stream_name(d["stream"] or d["cwd"] or "?", d["src"] or "local")
        if d["st"] == "ended" and not include_ended:
            continue
        if since and (d["last_ts"] or 0) < since:
            continue
        out.append(d)
    out.sort(key=lambda d: (STATE_ORDER.index(d["st"]), -(d["act_ts"] or d["last_ts"] or 0)))
    return out


# ---------------------------------------------------------------- remotes

def export(args):
    """Runs on a VM: print new events since an offset plus live sessions, as JSON."""
    pos, data = args.offset, ""
    if os.path.exists(EVENTS):
        size = os.path.getsize(EVENTS)
        if size < pos:
            pos = 0
        with open(EVENTS, "rb") as f:
            f.seek(pos)
            chunk = f.read(4 * 1024 * 1024)
        end = chunk.rfind(b"\n") + 1
        data = chunk[:end].decode("utf-8", "replace")
        pos += end
    print(json.dumps({"host": HOST, "pos": pos, "data": data, "live": live_sessions()}))


def gcloud_parts(target):
    """gcloud:INSTANCE[:ZONE[:PROJECT]] -> (instance, extra gcloud flags)."""
    parts = target[len("gcloud:"):].split(":")
    flags = []
    if len(parts) > 1 and parts[1]:
        flags += ["--zone", parts[1]]
    if len(parts) > 2 and parts[2]:
        flags += ["--project", parts[2]]
    return parts[0], flags


def remote_argv(target, cmd, interactive=False):
    """Command line that runs `cmd` on a VM. Targets: user@host, an ssh-config alias, or gcloud:INSTANCE:ZONE:PROJECT."""
    if target.startswith("gcloud:"):
        inst, flags = gcloud_parts(target)
        if interactive:
            return ["gcloud", "compute", "ssh", inst] + flags + ["--", "-t", cmd]
        return ["gcloud", "compute", "ssh", inst] + flags + ["--quiet", "--command", cmd]
    if interactive:
        return ["ssh", "-t", target, cmd]
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", target, cmd]


def ssh(target, cmd, timeout=20):
    return subprocess.run(remote_argv(target, cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, universal_newlines=True)


def remote_copy(target, src, dst):
    """Copy a local file to a path relative to the VM user's home."""
    if target.startswith("gcloud:"):
        inst, flags = gcloud_parts(target)
        argv = ["gcloud", "compute", "scp"] + flags + ["--quiet", src, "%s:%s" % (inst, dst)]
    else:
        argv = ["scp", "-q", "-o", "BatchMode=yes", src, "%s:%s" % (target, dst)]
    subprocess.run(argv, check=True)


def pull(con, only=None, quiet=False):
    for r in con.execute("SELECT * FROM remotes").fetchall():
        if only and r["name"] != only:
            continue
        try:
            res = ssh(r["ssh"], "python3 ~/.cos/bin/cos.py export %d" % r["pos"])
            if res.returncode != 0:
                raise RuntimeError(clip(res.stderr, 200) or "ssh failed")
            out = json.loads(res.stdout)
            with con:
                ingest_text(con, out["data"], r["name"], notify=True)
                apply_live(con, out["live"], r["name"], out["host"])
                con.execute("UPDATE remotes SET pos=?, last_pull=?, last_error=NULL WHERE name=?",
                            (out["pos"], now(), r["name"]))
            if not quiet:
                print("pulled %s: %d live sessions" % (r["name"], len(out["live"])))
        except Exception as ex:
            with con:
                con.execute("UPDATE remotes SET last_pull=?, last_error=? WHERE name=?",
                            (now(), clip(str(ex), 200), r["name"]))
            if not quiet:
                print("pull %s failed: %s" % (r["name"], ex))


def remote_install(target, dirs):
    if ssh(target, "mkdir -p ~/.cos/bin/hooks", timeout=60).returncode != 0:
        sys.exit("cannot reach %s (key-based ssh must work without a password prompt)" % target)
    for src, dst in ((os.path.abspath(__file__), ".cos/bin/cos.py"), (HOOK, ".cos/bin/hooks/cos-hook.sh")):
        remote_copy(target, src, dst)
    extra = " ".join("--dir '%s'" % d for d in dirs)
    res = ssh(target, "python3 ~/.cos/bin/cos.py install-hooks %s" % extra, timeout=60)
    print(res.stdout.strip() or res.stderr.strip())


# ---------------------------------------------------------------- gui snapshot

ORCA = shutil.which("orca") or "/Applications/Orca.app/Contents/Resources/bin/orca"
DOC_KINDS = [("docs.google.com/spreadsheets", "Sheets"), ("docs.google.com/presentation", "Slides"),
             ("docs.google.com/document", "Docs"), ("docs.google.com/forms", "Forms"),
             ("figma.com/", "Figma"), ("notion.so/", "Notion"), ("claude.ai/artifact", "Artifact"),
             ("github.com/", "GitHub"), ("linear.app/", "Linear"), ("canva.com/design", "Canva")]


def doc_kind(url):
    for pat, kind in DOC_KINDS:
        if pat in (url or ""):
            if kind == "GitHub" and not re.search(r"/(pull|issues)/\d+", url):
                return None
            return kind
    return None


def win_key(w):
    if w["url"]:
        u = w["url"].split("#")[0]
        m = re.match(r"(https://docs\.google\.com/\w+/d/[^/?]+)", u)   # one key per doc, whatever the tab/range
        return m.group(1) if m else u.split("?")[0] if doc_kind(u) else u
    return "%s|%s|%s" % (w["kind"], w["app"], w["title"])


def snap(con):
    items = []
    if sys.platform == "darwin":
        res = subprocess.run(["osascript", "-l", "JavaScript", os.path.join(HERE, "snap.js")],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True, timeout=60)
        try:
            items = [dict(kind=x["k"], app=x["app"], title=x["title"], url=x["url"], front=x["front"], cwd=None)
                     for x in json.loads(res.stdout)]
        except Exception:
            pass
    if os.path.exists(ORCA):
        try:
            res = subprocess.run([ORCA, "terminal", "list", "--json"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, universal_newlines=True, timeout=20)
            for t in json.loads(res.stdout)["result"]["terminals"]:
                if t.get("connected") and t.get("title"):
                    items.append(dict(kind="term", app="Orca", title=t["title"], url="", front=False,
                                      cwd=t.get("worktreePath")))
        except Exception:
            pass
    excl = [x.lower() for x in load_config().get("snap_exclude", DEFAULT_EXCLUDE)]
    ts = now()
    with con:
        for w in items:
            if any(x in (w["url"] + " " + w["title"]).lower() for x in excl) or w["url"].startswith("file://" + COS):
                continue
            key = win_key(w)
            if con.execute("SELECT 1 FROM windows WHERE key=?", (key,)).fetchone():
                con.execute("UPDATE windows SET title=?, last_seen=?, seen_n=seen_n+1, front_n=front_n+?, "
                            "cwd=COALESCE(?,cwd) WHERE key=?", (w["title"], ts, int(w["front"]), w["cwd"], key))
            else:
                con.execute("INSERT INTO windows VALUES(?,?,?,?,?,?,?,?,1,?)", (key, w["kind"], w["app"], w["title"],
                            w["url"], w["cwd"], ts, ts, int(w["front"])))
        con.execute("DELETE FROM windows WHERE last_seen<?", (ts - 7 * 86400,))
    return len(items)


def tick(con):
    """Background work the menu bar kicks off every few minutes: VM pulls + window snapshot."""
    pull(con, quiet=True)
    if load_config().get("snap", True):
        snap(con)


def windows_digest(con, since):
    rows = [dict(r) for r in con.execute("SELECT * FROM windows WHERE last_seen>?", (since,))]
    lines = []
    focused = sorted([r for r in rows if r["front_n"]], key=lambda r: -r["front_n"])[:12]
    if focused:
        lines.append("\nWHERE MY ATTENTION WENT (foreground app/tab, sampled every %d min):" % (TICK_EVERY // 60))
        lines += ["- ~%d min · %s · %s" % (r["front_n"] * TICK_EVERY // 60, r["app"], clip(r["title"], 90)) for r in focused]
    docs = sorted([r for r in rows if doc_kind(r["url"])], key=lambda r: -r["last_seen"])[:20]
    if docs:
        lines.append("\nDOCUMENTS OPEN IN BROWSER (kind · title · last seen):")
        lines += ["- %s · %s · %s ago" % (doc_kind(r["url"]), clip(r["title"], 90), age(r["last_seen"])) for r in docs]
    terms = [r for r in rows if r["kind"] == "term"]
    if terms:
        lines.append("\nTERMINAL TABS (Orca/iTerm titles):")
        lines += ["- %s · %s%s" % (r["app"], clip(r["title"], 80), " (%s)" % r["cwd"] if r["cwd"] else "") for r in terms[:25]]
    other = sorted([r for r in rows if not doc_kind(r["url"]) and r["kind"] != "term"], key=lambda r: -r["seen_n"])[:20]
    if other:
        lines.append("\nOTHER OPEN TABS / APP WINDOWS:")
        lines += ["- %s · %s%s" % (r["app"], clip(r["title"], 80),
                                   " (%s)" % r["url"].split("/")[2] if r["url"].startswith("http") else "") for r in other]
    return lines


# ---------------------------------------------------------------- hooks install

def hook_command(ev, agent="claude"):
    return 'sh "%s" %s%s' % (HOOK, ev, "" if agent == "claude" else " " + agent)


GEMINI_DIR = os.path.join(HOME, ".gemini")
CODEX_TOML = os.path.join(HOME, ".codex", "config.toml")


def edit_hooks_file(path, events, agent, remove, timeout, tool_event, tool_matcher):
    """Add (or remove) our entry per event in a Claude/Gemini-style settings.json; other hooks are untouched."""
    settings = {}
    if os.path.exists(path):
        with open(path) as f:
            settings = json.load(f)
        if not os.path.exists(path + ".cos-bak"):
            shutil.copy(path, path + ".cos-bak")
    elif remove:
        return
    hooks = settings.setdefault("hooks", {})
    for ev in events:
        groups = hooks.setdefault(ev, [])
        groups[:] = [g for g in groups if not any("cos-hook.sh" in h.get("command", "") for h in g.get("hooks", []))]
        if not remove:
            g = {"hooks": [{"type": "command", "command": hook_command(ev, agent), "timeout": timeout}]}
            if ev == tool_event:
                g["matcher"] = tool_matcher
            groups.append(g)
        if not groups:
            del hooks[ev]
    if not hooks:
        del settings["hooks"]
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
    print("%s %s hooks: %s" % ("removed" if remove else "installed", agent, path))


def codex_hooks(remove):
    """Codex has a single `notify` program; we wrap whatever is there so it keeps working."""
    if not os.path.exists(CODEX_TOML):
        return
    with open(CODEX_TOML) as f:
        text = f.read()
    m = re.search(r"^notify\s*=\s*(\[.*?\])\s*$", text, re.M)
    current = json.loads(m.group(1)) if m else []
    ours = "codex-notify" in current
    if remove:
        if not ours:
            return
        orig = current[current.index("codex-notify") + 1:]
        new = "notify = %s" % json.dumps(orig) if orig else ""
    else:
        if ours:
            return
        if not os.path.exists(CODEX_TOML + ".cos-bak"):
            shutil.copy(CODEX_TOML, CODEX_TOML + ".cos-bak")
        new = "notify = %s" % json.dumps([sys.executable, os.path.abspath(__file__), "codex-notify"] + current)
    text = text.replace(m.group(0), new) if m else new + "\n" + text   # top-level key must precede [tables]
    with open(CODEX_TOML, "w") as f:
        f.write(text)
    print("%s codex notify: %s" % ("removed" if remove else "installed", CODEX_TOML))


def codex_notify(argv):
    """Called by Codex as: cos.py codex-notify [original notify cmd...] <json>."""
    payload, orig = argv[-1], argv[:-1]
    try:
        d = json.loads(payload)
        if d.get("type") == "agent-turn-complete":
            msgs = d.get("input-messages") or d.get("input_messages") or []
            p = {"session_id": d.get("thread-id") or d.get("turn-id"), "cwd": d.get("cwd") or os.getcwd(),
                 "last_assistant_message": d.get("last-assistant-message")}
            base = {"ts": now(), "agent": "codex", "host": HOST, "profile": "codex",
                    "term": os.environ.get("TERM_PROGRAM", ""), "iterm": os.environ.get("ITERM_SESSION_ID", "")}
            os.makedirs(COS, exist_ok=True)
            with open(EVENTS, "a") as f:
                if msgs:
                    f.write(json.dumps(dict(base, ev="UserPromptSubmit", p=dict(p, prompt=msgs[-1]))) + "\n")
                f.write(json.dumps(dict(base, ev="Stop", p=p)) + "\n")
    except Exception:
        pass
    if orig:
        subprocess.run(orig + [payload])


def install_hooks(args):
    cfg = load_config()
    for d in args.dir or []:
        d = os.path.abspath(os.path.expanduser(d))
        cfg.setdefault("config_dirs", [])
        if d not in cfg["config_dirs"]:
            cfg["config_dirs"].append(d)
        os.makedirs(d, exist_ok=True)
    save_config(cfg)
    os.chmod(HOOK, 0o755)
    for d in config_dirs():
        edit_hooks_file(os.path.join(d, "settings.json"), HOOK_EVENTS, "claude", args.remove, 5, "PostToolUse", "*")
    if os.path.isdir(GEMINI_DIR):   # Gemini CLI: same JSON shape, different event names, timeout in ms
        edit_hooks_file(os.path.join(GEMINI_DIR, "settings.json"), GEMINI_EVENTS, "gemini", args.remove, 5000,
                        "AfterTool", ".*")
    if args.remove or getattr(args, "codex", False):
        codex_hooks(args.remove)


# ---------------------------------------------------------------- focus / notify

def focus(con, sid):
    r = con.execute("SELECT * FROM sessions WHERE id LIKE ?", (sid + "%",)).fetchone()
    if not r:
        sys.exit("no such session")
    with con:
        con.execute("UPDATE sessions SET seen_ts=? WHERE id=?", (now(), r["id"]))
    if r["src"] != "local":
        return focus_remote(con, r)
    if r["orca"] and os.path.exists(ORCA):
        subprocess.run([ORCA, "terminal", "switch", "--terminal", r["orca"]],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["open", "-a", "Orca"])
        return
    tty = r["tty"]
    if r["tmux"]:
        # pick the right window/pane inside tmux, then bring up the terminal tab attached to that session
        tmux = shutil.which("tmux") or next((p for p in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux")
                                             if os.path.exists(p)), "tmux")
        win, pane = r["tmux"].rsplit(".", 1)[0], r["tmux"]
        subprocess.run([tmux, "select-window", "-t", win], stderr=subprocess.DEVNULL)
        subprocess.run([tmux, "select-pane", "-t", pane], stderr=subprocess.DEVNULL)
        res = subprocess.run([tmux, "list-clients", "-t", r["tmux"].split(":")[0], "-F", "#{client_tty}"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
        tty = (res.stdout.split() or [tty])[0]
    if r["iterm"] and not r["tmux"] and app_running("iTerm2"):
        if osa(ITERM_FIND % ("unique id", r["iterm"].split(":")[-1])) == "ok":
            return
    if focus_tty(tty):
        return
    app = {"Apple_Terminal": "Terminal", "vscode": "Visual Studio Code", "WarpTerminal": "Warp",
           "ghostty": "Ghostty", "iTerm.app": "iTerm", "WezTerm": "WezTerm"}.get(r["term"] or "", "")
    if app:
        subprocess.run(["open", "-a", app])


ITERM_FIND = '''tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if %s of s is "%s" then
          select w
          select t
          select s
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell'''

TERMINAL_FIND = '''tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      if tty of t is "%s" then
        set selected of t to true
        set index of w to 1
        activate
        return "ok"
      end if
    end repeat
  end repeat
end tell'''


def focus_tty(tty):
    """Bring the iTerm or Terminal.app tab that owns this tty to the front (never launches either app)."""
    if not tty or "?" in tty:
        return False
    tty = tty if tty.startswith("/dev/") else "/dev/" + tty
    if app_running("iTerm2") and osa(ITERM_FIND % ("tty", tty)) == "ok":
        return True
    return app_running("Terminal") and osa(TERMINAL_FIND % tty) == "ok"


def terminal_app():
    return load_config().get("terminal") or ("iTerm" if os.path.exists("/Applications/iTerm.app") else "Terminal")


def focus_remote(con, r):
    """Open a terminal window on the VM, re-attached to the session's tmux pane when we know it."""
    remote = con.execute("SELECT ssh FROM remotes WHERE name=?", (r["src"],)).fetchone()
    if not remote:
        return mac_notify("cos", "%s runs on %s (%s)" % (r["name"] or r["title"], r["src"], r["cwd"]))
    if r["tmux"]:
        s = r["tmux"].split(":")[0]
        cmd = "tmux attach -t '%s' \\; select-window -t '%s' \\; select-pane -t '%s'" % (
            s, r["tmux"].rsplit(".", 1)[0], r["tmux"])
    else:
        cmd = "cd '%s' 2>/dev/null; exec $SHELL -l" % (r["cwd"] or "~")
    argv = remote_argv(remote["ssh"], cmd, interactive=True)
    os.makedirs(os.path.join(COS, "tmp"), exist_ok=True)
    path = os.path.join(COS, "tmp", "attach-%s.command" % r["src"])
    with open(path, "w") as f:
        f.write("#!/bin/sh\nexec %s\n" % " ".join(shlex.quote(a) for a in argv))
    os.chmod(path, 0o755)
    subprocess.run(["open", "-a", terminal_app(), path])



def notify_finished(con, sid, ts):
    """Notify when a turn that ran at least `notify_done_min` minutes finishes (0 = off)."""
    mins = load_config().get("notify_done_min", 5)
    start = con.execute("SELECT MAX(ts) FROM prompts WHERE sid=? AND ts<=?", (sid, ts)).fetchone()[0]
    if not mins or not start or ts - start < mins * 60:
        return
    s = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    where = stream_name(s["stream"] or s["cwd"] or "?", s["src"] or "local")
    mac_notify("cos · %s finished (%s)" % (where, age(now() - (ts - start))),
               clip(s["last_reply"] or s["last_prompt"] or "", 120))


def notify_line(e, src="local"):
    p = e.get("p") or {}
    if e.get("ev") == "Stop":
        con = refresh()       # the Stop line is already on disk, so this ingests it
        notify_finished(con, p.get("session_id"), int(e.get("ts") or now()))
        return
    if not is_permission_wait(p):
        return
    where = stream_name(stream_of(p.get("cwd") or "", src), src)
    mac_notify("cos · %s needs you" % where, p.get("message") or "Claude is waiting for you")


# ---------------------------------------------------------------- todos

def parse_due(args):
    if getattr(args, "date", None):
        return args.date
    if getattr(args, "tomorrow", False):
        return today(1)
    if getattr(args, "someday", False):
        return None
    return today()


def add_todo(con, text, due, stream=None, source="me"):
    text = text.strip()
    if text.lower().startswith("tmr:"):
        text, due = text[4:].strip(), today(1)
    if not text:
        return None
    with con:
        cur = con.execute("INSERT INTO todos(text,due,stream,source,created) VALUES(?,?,?,?,?)",
                          (text, due, stream, source, now()))
    return cur.lastrowid


def todos(con, which):
    q = "SELECT * FROM todos WHERE status='open' AND "
    if which == "today":
        rows = con.execute(q + "due IS NOT NULL AND due<=? ORDER BY due, id", (today(),))
    elif which == "tomorrow":
        rows = con.execute(q + "due=? ORDER BY id", (today(1),))
    elif which == "later":
        rows = con.execute(q + "(due IS NULL OR due>?) ORDER BY due IS NULL, due, id", (today(1),))
    else:
        rows = con.execute(q + "1 ORDER BY due IS NULL, due, id")
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- brief

def find_transcript(s):
    if s["transcript"] and os.path.exists(s["transcript"]):
        return s["transcript"]
    for d in config_dirs():
        hits = glob.glob(os.path.join(d, "projects", "*", s["id"] + ".jsonl"))
        if hits:
            return hits[0]
    return None


def read_transcript(path, since):
    """Recent user prompts and Claude's last text reply, from the tail of a transcript."""
    prompts, reply = [], ""
    if not path:
        return prompts, reply
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 400000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return prompts, reply
    for line in lines:
        try:
            m = json.loads(line)
        except Exception:
            continue
        content = (m.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
        else:
            continue
        if not text.strip() or text.lstrip().startswith("<"):   # skip tool results / system-injected text
            continue
        if m.get("type") == "user" and not m.get("isMeta") and not m.get("isCompactSummary") \
                and not text.startswith("This session is being continued"):
            prompts.append(text)
        elif m.get("type") == "assistant":
            reply = text
    return prompts[-5:], reply


def digest(con, hours=24):
    since = now() - hours * 3600
    lines = ["Now: %s" % dt.datetime.now().strftime("%A %Y-%m-%d %H:%M"), ""]
    lines.append("OPEN TO-DOS (id · due · text):")
    for t in todos(con, "all"):
        lines.append("- #%d · %s · %s%s" % (t["id"], t["due"] or "someday", t["text"],
                                          " [%s]" % t["stream"] if t["stream"] else ""))
    done = con.execute("SELECT text FROM todos WHERE status='done' AND done_at>?", (since,)).fetchall()
    if done:
        lines.append("\nDONE IN LAST %dh:" % hours)
        lines += ["- %s" % r["text"] for r in done]
    lines.append("\nWORK STREAMS (coding-agent sessions active in last %dh):" % hours)
    by_stream = {}
    for s in sessions(con, include_ended=True, since=since):
        by_stream.setdefault(s["sname"], []).append(s)
    for name, ss in by_stream.items():
        lines.append("\n## %s  (%s)" % (name, ss[0]["cwd"]))
        for s in ss:
            lines.append("- %s session '%s' [%s, profile %s, last activity %s ago]%s" % (
                s["agent"] or "claude", s["label"], STATE_LABEL[s["st"]], s["profile"], age(s["act_ts"] or s["last_ts"]),
                " — waiting on: " + s["needs_msg"] if s["st"] == "needs_you" and s["needs_msg"] else ""))
            ps = [r["text"] for r in con.execute(
                "SELECT text FROM prompts WHERE sid=? AND ts>? ORDER BY ts DESC LIMIT 5", (s["id"], since))][::-1]
            reply = s["last_reply"]
            if s["src"] == "local" and (not ps or not reply):
                tp, tr = read_transcript(find_transcript(s), since)
                ps, reply = ps or tp, reply or tr
            for p in ps:
                lines.append("    user asked: " + clip(p, 300))
            if reply:
                lines.append("    agent's last reply: " + clip(reply, 700))
    if load_config().get("brief_windows", True):
        lines += windows_digest(con, since)
    return "\n".join(lines)


BRIEF_PROMPT = """You are my chief of staff. I run many coding-agent sessions (Claude Code, Gemini, Codex) in parallel across \
terminals, accounts and VMs, alongside browser tabs, Google Sheets/Slides/Docs and other apps. Below is a raw \
activity digest. Write a short, scannable brief in Markdown:

# Brief — <weekday, date, morning/evening>
## Needs you now
(sessions blocked on me or finished and awaiting my review; say what each needs, one line each)
## Work streams
(one line per stream: status emoji, stream name, what was done, what's pending/next)
## Beyond the terminal
(work outside the agents, from the window data: documents I'm working on, where my attention went, \
anything that looks open-ended or forgotten; skip email/chat noise; max 6 lines)
## Today
(prioritised checklist: existing open to-dos first by #id, then anything implied by the streams)
## Tomorrow / carry-over
## Suggested to-dos
(only NEW items not already in my to-do list, formatted exactly as `- [ ] text`; max 6; none if nothing new)

Be concrete, no filler, no preamble. If the digest is nearly empty, say so in one line.

DIGEST:
"""


def md_to_html(md, title):
    """Just enough Markdown for briefs: headings, lists, checkboxes, bold, code, links."""
    import html as h

    def inline(s):
        s = h.escape(s, quote=False)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![*\w])\*([^*]+)\*(?!\w)", r"<em>\1</em>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
        s = re.sub(r'(?<![">])(https?://[^\s<)]+)', r'<a href="\1">\1</a>', s)
        return s

    out, lst = [], None
    for line in md.splitlines():
        m = re.match(r"^\s*(?:([-*])|(\d+)\.)\s+(.*)$", line)
        kind = ("ul" if m.group(1) else "ol") if m else None
        if lst and kind != lst:
            out.append("</%s>" % lst)
            lst = None
        if m:
            if not lst:
                out.append("<%s>" % kind)
                lst = kind
            item = m.group(3)
            box = re.match(r"^\[( |x)\]\s+(.*)$", item)
            if box:
                item = '<input type="checkbox"%s> %s' % (" checked" if box.group(1) == "x" else "", inline(box.group(2)))
                out.append('<li class="todo">%s</li>' % item)
            else:
                out.append("<li>%s</li>" % inline(item))
            continue
        hm = re.match(r"^(#{1,4})\s+(.*)$", line)
        if hm:
            n = len(hm.group(1))
            out.append("<h%d>%s</h%d>" % (n, inline(hm.group(2)), n))
        elif line.strip():
            out.append("<p>%s</p>" % inline(line))
    if lst:
        out.append("</%s>" % lst)
    return """<!doctype html><html><head><meta charset="utf-8"><title>%s</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#fbfaf7;--fg:#1f1f1c;--muted:#6b6a64;--line:#e6e3dc;--accent:#b4541a;--code:#f1eee7}
@media (prefers-color-scheme:dark){:root{--bg:#1b1b19;--fg:#ecebe6;--muted:#9c9a92;--line:#33322e;--accent:#e58a4e;--code:#2a2926}}
body{background:var(--bg);color:var(--fg);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0}
main{max-width:760px;margin:0 auto;padding:40px 20px 80px}
h1{font-size:1.7em;margin:0 0 4px}
h2{font-size:1.05em;text-transform:uppercase;letter-spacing:.06em;color:var(--accent);border-top:1px solid var(--line);padding-top:20px;margin-top:28px}
ul,ol{padding-left:1.3em}li{margin:6px 0}li.todo{list-style:none;margin-left:-1.3em}
code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:.9em}
a{color:var(--accent);word-break:break-all}p{margin:8px 0}
.meta{color:var(--muted);font-size:.85em;margin-bottom:8px}
</style></head><body><main><div class="meta">cos brief · %s</div>
%s
</main></body></html>
""" % (h.escape(title), h.escape(title), "\n".join(out))


def brief_html(md_path):
    html_path = md_path[:-3] + ".html"
    if not os.path.exists(html_path) or os.path.getmtime(html_path) < os.path.getmtime(md_path):
        with open(md_path) as f:
            md = f.read()
        with open(html_path, "w") as f:
            f.write(md_to_html(md, os.path.basename(md_path)[:-3]))
    return html_path


def open_brief():
    latest = sorted(glob.glob(os.path.join(BRIEFS, "*.md")))
    if not latest:
        sys.exit("no briefs yet — run `cos brief`")
    subprocess.run(["open", brief_html(latest[-1])])   # .html opens in the default browser


def find_claude(required=True):
    for c in (shutil.which("claude"), os.path.join(HOME, ".local/bin/claude"), "/usr/local/bin/claude",
              "/opt/homebrew/bin/claude"):
        if c and os.path.exists(c):
            return c
    if required:
        sys.exit("claude CLI not found")
    return None


def brief(con, args):
    if not args.no_pull:
        pull(con, quiet=True)
    text = digest(con, args.hours)
    if args.dry_run:
        print(text)
        return
    env = dict(os.environ, COS_SKIP="1")
    if args.profile:
        env["CLAUDE_CONFIG_DIR"] = args.profile
    res = subprocess.run([find_claude(), "-p", "--model", args.model], input=BRIEF_PROMPT + text,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                         env=env, timeout=300)
    if res.returncode != 0:
        sys.exit("claude -p failed: " + res.stderr.strip())
    out = res.stdout.strip()
    os.makedirs(BRIEFS, exist_ok=True)
    path = os.path.join(BRIEFS, dt.datetime.now().strftime("%Y-%m-%d-%H%M.md"))
    with open(path, "w") as f:
        f.write(out + "\n")
    brief_html(path)
    print(out)
    if args.apply:
        sec = out.split("## Suggested to-dos", 1)[-1] if "## Suggested to-dos" in out else ""
        for m in re.finditer(r"^- \[ \] (.+)$", sec, re.M):
            add_todo(con, m.group(1), today(), source="brief")
    if args.notify:
        mac_notify("cos", "Your brief is ready")


def schedule(args):
    label = "club.cos.brief"
    plist = os.path.join(HOME, "Library/LaunchAgents/%s.plist" % label)
    subprocess.run(["launchctl", "bootout", "gui/%d" % os.getuid(), plist],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if args.off:
        if os.path.exists(plist):
            os.remove(plist)
        print("brief schedule removed")
        return
    times = ""
    for t in args.times:
        h, m = t.split(":")
        times += "<dict><key>Hour</key><integer>%d</integer><key>Minute</key><integer>%d</integer></dict>" % (int(h), int(m))
    wrapper = LAUNCHER
    with open(plist, "w") as f:
        f.write("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>%s</string>
<key>ProgramArguments</key><array><string>%s</string><string>brief</string><string>--notify</string></array>
<key>StartCalendarInterval</key><array>%s</array>
<key>StandardOutPath</key><string>%s/brief.log</string>
<key>StandardErrorPath</key><string>%s/brief.log</string>
<key>LowPriorityIO</key><true/><key>ProcessType</key><string>Background</string>
</dict></plist>
""" % (label, wrapper, times, COS, COS))
    subprocess.run(["launchctl", "bootstrap", "gui/%d" % os.getuid(), plist], check=True)
    print("brief scheduled at %s" % ", ".join(args.times))


# ---------------------------------------------------------------- setup / doctor / uninstall

PLUGIN = """#!/bin/sh
# <xbar.title>cos</xbar.title>
# <xbar.version>v1.0 · chief of staff</xbar.version>
# <xbar.author>Hanshika Gupta</xbar.author>
# <xbar.desc>One place for every agent session, open doc and to-do.</xbar.desc>
# <xbar.dependencies>python3, Claude Code</xbar.dependencies>
# <xbar.abouturl>%s</xbar.abouturl>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
exec "%s" bar
"""
SWIFTBAR_APP = "/Applications/SwiftBar.app"
SCHEDULE_PLIST = os.path.join(HOME, "Library/LaunchAgents/club.cos.brief.plist")


def plugin_path():
    res = subprocess.run(["defaults", "read", "com.ameba.SwiftBar", "PluginDirectory"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
    d = res.stdout.strip()
    if not d:
        d = os.path.join(COS, "swiftbar")
        subprocess.run(["defaults", "write", "com.ameba.SwiftBar", "PluginDirectory", "-string", d])
    return os.path.join(os.path.expanduser(d), "cos.1m.sh")


def install_plugin():
    path = plugin_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(PLUGIN % (REPO_URL, LAUNCHER))
    os.chmod(path, 0o755)
    print("menu bar plugin: %s" % path)


def live_ssh_targets():
    """VMs you're connected to right now: running `ssh … host` and `gcloud compute ssh …` processes."""
    res = subprocess.run(["ps", "-axo", "command"], stdout=subprocess.PIPE, universal_newlines=True)
    out = []
    for line in res.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        if os.path.basename(parts[0]) == "ssh":
            i = 1
            while i < len(parts):
                a = parts[i]
                if a.startswith("-"):
                    i += 2 if len(a) == 2 and a[1] in "BbcDEeFIiJLlmOoPpQRSWw" else 1
                    continue
                out.append(a)
                break
        elif "compute" in parts and "ssh" in parts and "gcloud" in line:
            j = parts.index("ssh") + 1
            if j < len(parts) and not parts[j].startswith("-"):
                flag = lambda k: next((parts[i + 1] for i, x in enumerate(parts[:-1]) if x == k), "") or \
                    next((x.split("=", 1)[1] for x in parts if x.startswith(k + "=")), "")
                out.append("gcloud:%s:%s:%s" % (parts[j], flag("--zone"), flag("--project")))
    seen = set()
    return [t for t in out if not (t in seen or seen.add(t)) and t not in ("localhost", "git@github.com")]


def setup(args):
    yes = True if args.yes else None
    print("cos setup — answers can be changed later with `cos setup` or `cos config`.\n")
    dirs = config_dirs()
    agents = ["Claude Code (%s)" % d.replace(HOME, "~") for d in dirs]
    if os.path.isdir(GEMINI_DIR):
        agents.append("Gemini CLI (~/.gemini)")
    print("Agents found: %s" % (", ".join(agents) or "none (install Claude Code first: https://claude.com/code)"))
    if agents and ask("Install cos hooks for them? (adds entries; your other hooks are kept)", True, yes):
        install_hooks(argparse.Namespace(dir=None, remove=False, codex=False))
    if shutil.which("codex") and os.path.exists(CODEX_TOML) and \
            ask("Also track Codex? (wraps its `notify` setting; experimental)", False, yes):
        codex_hooks(False)

    if sys.platform == "darwin":
        if not os.path.exists(SWIFTBAR_APP) and shutil.which("brew") and \
                ask("\nInstall SwiftBar (free, open source) to show cos in the menu bar?", True, yes):
            subprocess.run(["brew", "install", "--cask", "swiftbar"])
        if os.path.exists(SWIFTBAR_APP):
            install_plugin()
            subprocess.run(["open", "-g", "-a", "SwiftBar"])
        else:
            print("No menu bar: install SwiftBar later and re-run `cos setup`. The `cos` CLI works without it.")

        cfg = load_config()
        cfg["snap"] = ask("\nTrack browser tabs, terminal tabs and app windows? Stored only on this Mac for 7 days;\n"
                          "mail, banking, password-manager and login pages are skipped.", True, yes)
        if cfg["snap"]:
            cfg["brief_windows"] = ask("Include those window titles in the AI brief?", True, yes)
        save_config(cfg)

    targets = live_ssh_targets()
    con = db()
    known = {r["ssh"] for r in con.execute("SELECT ssh FROM remotes")}
    for t in [t for t in targets if t not in known]:
        if ask("\nYou're connected to %s. Track agent sessions there too? (installs cos + hooks on it)" % t,
               True, False if yes else None):
            default = re.sub(r"[^\w-]", "-", t.split("@")[-1].split(":")[1 if t.startswith("gcloud:") else 0])[:24]
            name = (input("  Name for it [%s]: " % default).strip() if yes is None else "") or default
            with con:
                con.execute("INSERT OR REPLACE INTO remotes(name, ssh) VALUES(?,?)", (name, t))
            try:
                remote_install(t, [])
                pull(con, name)
            except (SystemExit, Exception) as ex:
                print("  couldn't set up %s: %s" % (t, ex))

    if sys.platform == "darwin" and find_claude(required=False) and not os.path.exists(SCHEDULE_PLIST) and \
            ask("\nSchedule a daily brief at 09:00 and 18:00? (one short Claude Haiku call each)", True,
                False if yes else None):
        schedule(argparse.Namespace(times=["09:00", "18:00"], off=False))
    print("\nDone. `cos` shows status, `cos doctor` checks everything, `cos --help` lists commands.")


def doctor():
    good, issues = "%s✓%s" % (C["done"], C["0"]), []

    def check(ok, msg, fix=""):
        print("  %s %s" % (good if ok else "%s!%s" % (C["needs_you"], C["0"]), msg))
        if not ok and fix:
            print("      → %s" % fix)
            issues.append(msg)

    print("cos doctor\n")
    check(sys.version_info >= (3, 6), "python %s" % sys.version.split()[0], "cos needs Python 3.6+")
    check(os.path.exists(LAUNCHER), "launcher %s" % LAUNCHER, "re-run install.sh")
    check(bool(shutil.which("cos")), "`cos` on PATH", "add ~/.local/bin to your PATH")
    for d in config_dirs():
        try:
            with open(os.path.join(d, "settings.json")) as f:
                hooks = json.load(f).get("hooks", {})
        except Exception:
            hooks = {}
        missing = [ev for ev in HOOK_EVENTS if not any("cos-hook.sh" in h.get("command", "")
                                                       for g in hooks.get(ev, []) for h in g.get("hooks", []))]
        check(not missing, "Claude hooks in %s" % d.replace(HOME, "~"),
              "missing %s — run `cos install-hooks`" % ", ".join(missing))
    if os.path.isdir(GEMINI_DIR):
        try:
            with open(os.path.join(GEMINI_DIR, "settings.json")) as f:
                gh = json.dumps(json.load(f).get("hooks", {}))
        except Exception:
            gh = ""
        check("cos-hook.sh" in gh, "Gemini CLI hooks", "run `cos install-hooks`")
    if os.path.exists(EVENTS):
        a = age(os.path.getmtime(EVENTS))
        check(True, "last agent event: %s" % ("just now" if a == "<1m" else a + " ago"))
    else:
        check(False, "no agent events yet", "send a prompt in any agent session, then run `cos`")
    check(bool(find_claude(required=False)), "claude CLI (used by `cos brief`)", "install Claude Code to get briefs")
    if sys.platform == "darwin":
        check(os.path.exists(SWIFTBAR_APP), "SwiftBar installed", "brew install --cask swiftbar && cos setup")
        if os.path.exists(SWIFTBAR_APP):
            check(app_running("SwiftBar"), "SwiftBar running", "open -a SwiftBar")
            check(os.path.exists(plugin_path()), "menu bar plugin", "cos setup")
        check(os.path.exists(SCHEDULE_PLIST), "daily brief scheduled", "cos schedule 09:00 18:00 (optional)")
        cfg = load_config()
        if cfg.get("snap", True):
            con = db()
            recent = dict(con.execute("SELECT kind, COUNT(*) FROM windows WHERE last_seen>? GROUP BY kind",
                                      (now() - 3600,)).fetchall())
            browsers = [b for b in ("Google Chrome", "Safari") if app_running(b)]
            check(recent.get("tab", 0) or not browsers, "browser tabs recorded (%d in last hour)" % recent.get("tab", 0),
                  "allow SwiftBar to control %s in System Settings → Privacy & Security → Automation"
                  % " and ".join(browsers))
            check(recent.get("win", 0) > 0, "app windows recorded (%d in last hour)" % recent.get("win", 0),
                  "enable SwiftBar in System Settings → Privacy & Security → Accessibility")
    for r in db().execute("SELECT * FROM remotes"):
        check(not r["last_error"], "VM %s (%s) pulled %s ago" % (r["name"], r["ssh"], age(r["last_pull"])),
              r["last_error"] or "")
    print("\n%s" % ("All good." if not issues else "%d thing(s) to fix." % len(issues)))


def uninstall(args):
    install_hooks(argparse.Namespace(dir=None, remove=True, codex=True))
    con = db()
    for r in con.execute("SELECT * FROM remotes").fetchall():
        try:
            res = ssh(r["ssh"], "python3 ~/.cos/bin/cos.py install-hooks --remove", timeout=60)
            print("VM %s: %s" % (r["name"], (res.stdout or res.stderr).strip().replace("\n", "; ")))
        except Exception as ex:
            print("VM %s: couldn't remove hooks (%s)" % (r["name"], ex))
    if sys.platform == "darwin":
        schedule(argparse.Namespace(off=True, times=[]))
        if os.path.exists(plugin_path()):
            os.remove(plugin_path())
            print("removed menu bar plugin")
    link = os.path.join(HOME, ".local", "bin", "cos")
    if os.path.islink(link) and os.path.realpath(link) == os.path.realpath(LAUNCHER):
        os.remove(link)
    if args.purge:
        shutil.rmtree(COS, ignore_errors=True)
        print("deleted %s" % COS)
    else:
        print("your data is kept in %s (`cos uninstall --purge` deletes it)" % COS)


# ---------------------------------------------------------------- output

C = {"needs_you": "\033[1;33m", "working": "\033[36m", "done": "\033[32m", "idle": "\033[2m",
     "ended": "\033[2m", "b": "\033[1m", "d": "\033[2m", "0": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}


def print_status(con, show_ended=False):
    ss = sessions(con, include_ended=show_ended)
    counts = {k: sum(1 for s in ss if s["st"] == k) for k in STATE_ORDER}
    print("%s%s%s  " % (C["b"], dt.datetime.now().strftime("%a %d %b %H:%M"), C["0"]) + "  ".join(
        "%s%s %d %s%s" % (C[k], STATE_ICON[k], counts[k], STATE_LABEL[k].lower(), C["0"])
        for k in STATE_ORDER[:4] if counts[k]))
    by_stream = {}
    for s in ss:
        by_stream.setdefault(s["sname"], []).append(s)
    for name, group in by_stream.items():
        print("\n%s%s%s %s%s%s" % (C["b"], name, C["0"], C["d"], group[0]["cwd"], C["0"]))
        for s in group:
            what = s["needs_msg"] if s["st"] == "needs_you" else s["last_prompt"] or s["title"] or ""
            print("  %s%s %-10s%s %-22s %-4s %s%s%s  %s[%s]%s" % (
                C[s["st"]], STATE_ICON[s["st"]], STATE_LABEL[s["st"]], C["0"], clip(s["label"], 22),
                age(s["act_ts"] or s["last_ts"]), C["d"], clip(what, 60), C["0"], C["d"], s["id"][:6], C["0"]))
    t = todos(con, "today")
    if t:
        print("\n%sToday%s" % (C["b"], C["0"]))
        print_todos(t)
    for r in con.execute("SELECT * FROM remotes WHERE last_error IS NOT NULL"):
        print("\n%s! remote %s: %s%s" % (C["needs_you"], r["name"], r["last_error"], C["0"]))


def print_todos(ts):
    for t in ts:
        late = t["due"] and t["due"] < today()
        print("  %s#%-3d%s ☐ %s%s%s" % (C["d"], t["id"], C["0"], t["text"],
                                        " %s(%s)%s" % (C["needs_you"], t["due"], C["0"]) if late else "",
                                        " %s[%s]%s" % (C["d"], t["stream"], C["0"]) if t["stream"] else ""))
    if not ts:
        print("  %snothing%s" % (C["d"], C["0"]))


def bar(con):
    """SwiftBar plugin output."""
    cfg = load_config()
    if now() - cfg.get("last_tick", 0) > TICK_EVERY:
        cfg["last_tick"] = now()
        save_config(cfg)
        subprocess.Popen([sys.executable, __file__, "tick"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    ss = sessions(con)
    t = todos(con, "today")
    n = {k: sum(1 for s in ss if s["st"] == k) for k in STATE_ORDER}
    me = '"%s"' % LAUNCHER if " " in LAUNCHER else LAUNCHER
    act = lambda *a: "bash=%s %s terminal=false refresh=true" % (
        me, " ".join("param%d=%s" % (i + 1, x) for i, x in enumerate(a)))
    safe = lambda s: clip(s, 70).replace("|", "¦").lstrip("-")
    # only non-zero counts: ⏳ needs you · ⚙️ working · ✅ your turn · 📝 to-dos due today
    title = ["%s%d" % (icon, c) for icon, c in (("⏳", n["needs_you"]), ("⚙️", n["working"]),
                                                ("✅", n["done"]), ("📝", len(t))) if c]
    print(" ".join(title) or "cos")
    print("---")
    print("⏳ needs you · ⚙️ working · ✅ your turn · 📝 to-dos | size=11 color=gray")
    print("---")
    for k in ("needs_you", "working", "done", "idle"):
        group = [s for s in ss if s["st"] == k]
        if not group:
            continue
        if k == "idle":
            print("💤 Idle (%d)" % len(group))
        else:
            print("%s %s | size=12 color=gray" % (STATE_ICON[k], STATE_LABEL[k]))
        for s in group:
            pre = "--" if k == "idle" else ""
            print("%s%s — %s · %s | %s" % (pre, safe(s["label"]), safe(s["sname"]), age(s["act_ts"] or s["last_ts"]),
                                          act("focus", s["id"])))
            what = s["needs_msg"] if k == "needs_you" else s["last_prompt"]
            if what:
                print("%s--%s | size=11" % (pre, safe(what)))
                print("%s--Hide this session | %s" % (pre, act("hide", s["id"])))
    print("---")
    print("To-dos today (%d) | size=12 color=gray" % len(t))
    for x in t:
        print("☐ %s%s | %s" % (safe(x["text"]), " (%s)" % x["due"] if x["due"] < today() else "", act("done", str(x["id"]))))
        print("--Move to tomorrow | %s" % act("move", str(x["id"]), "tomorrow"))
        print("--Drop | %s" % act("drop", str(x["id"])))
    tm = todos(con, "tomorrow")
    if tm:
        print("Tomorrow (%d)" % len(tm))
        for x in tm:
            print("--%s | %s" % (safe(x["text"]), act("move", str(x["id"]), "today")))
    print("➕ Add to-do… | %s" % act("add-dialog"))
    docs = [dict(r) for r in con.execute("SELECT * FROM windows WHERE url!='' AND last_seen>? ORDER BY last_seen DESC",
                                         (now() - 3 * 86400,))]
    docs = [d for d in docs if doc_kind(d["url"])][:10]
    if docs:
        print("---")
        print("📄 Recent docs (%d)" % len(docs))
        for d in docs:
            print("--%s · %s · %s | href=%s" % (doc_kind(d["url"]), safe(d["title"]), age(d["last_seen"]), d["url"]))
    print("---")
    latest = sorted(glob.glob(os.path.join(BRIEFS, "*.md")))
    if latest:
        print("📋 Open latest brief (%s) | %s" % (os.path.basename(latest[-1])[:-3], act("open-brief")))
    print("✨ Run brief now (Haiku) | bash=%s param1=brief param2=--notify terminal=false refresh=true" % me)
    print("🖥 Full status in terminal | bash=%s param1=status terminal=true" % me)
    mins = cfg.get("notify_done_min", 5)
    print("⚙️ Settings")
    print("--%s Notify when tasks over %d min finish | %s" % ("✓" if mins else "  ", mins or 5,
          act("config", "notify_done_min", "0" if mins else "5")))
    snap_on = cfg.get("snap", True)
    print("--%s Track browser tabs & app windows | %s" % ("✓" if snap_on else "  ",
          act("config", "snap", "false" if snap_on else "true")))
    bw = cfg.get("brief_windows", True)
    print("--%s Include window titles in the AI brief | %s" % ("✓" if bw else "  ",
          act("config", "brief_windows", "false" if bw else "true")))
    print("--Run health check (cos doctor) | bash=%s param1=doctor terminal=true" % me)
    for r in con.execute("SELECT * FROM remotes"):
        print("🔗 %s · pulled %s ago%s | %s" % (r["name"], age(r["last_pull"]),
                                            " ⚠ " + safe(r["last_error"]) if r["last_error"] else "", act("pull")))


# ---------------------------------------------------------------- cli

def main():
    if sys.argv[1:2] == ["codex-notify"]:     # raw argv: the wrapped command may contain anything
        return codex_notify(sys.argv[2:])
    ap = argparse.ArgumentParser(prog="cos", description="chief of staff for parallel coding-agent sessions")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("status", help="sessions by work stream + today's to-dos (default)")
    s.add_argument("-a", "--all", action="store_true", help="include ended sessions")
    for name in ("add",):
        a = sub.add_parser(name, help="add a to-do (default due today; prefix 'tmr:' for tomorrow)")
        a.add_argument("text", nargs="+")
        a.add_argument("-t", "--tomorrow", action="store_true")
        a.add_argument("-w", "--someday", action="store_true")
        a.add_argument("-d", "--date", help="YYYY-MM-DD")
        a.add_argument("-s", "--stream")
    sub.add_parser("add-dialog")
    for name in ("done", "drop"):
        a = sub.add_parser(name)
        a.add_argument("ids", nargs="+", type=int)
    a = sub.add_parser("move", help="move a to-do: today|tomorrow|someday|YYYY-MM-DD")
    a.add_argument("id", type=int)
    a.add_argument("when")
    sub.add_parser("today")
    sub.add_parser("tomorrow")
    sub.add_parser("todos", help="all open to-dos")
    sub.add_parser("log", help="what got done today")
    a = sub.add_parser("focus")
    a.add_argument("sid")
    a = sub.add_parser("hide")
    a.add_argument("sid")
    sub.add_parser("bar")
    sub.add_parser("open-brief", help="open the latest brief in your browser")
    sub.add_parser("snap", help="record browser tabs, terminal tabs and app windows now")
    sub.add_parser("tick")
    a = sub.add_parser("windows", help="what tabs/windows cos has seen (last 24h)")
    a.add_argument("--hours", type=int, default=24)
    a = sub.add_parser("config", help="show or set a setting: notify_done_min N | snap true/false | "
                                      "snap_exclude add/rm TEXT")
    a.add_argument("key", nargs="?")
    a.add_argument("value", nargs="*")
    sub.add_parser("notify")
    a = sub.add_parser("brief", help="LLM brief of all work streams (claude -p, Haiku)")
    a.add_argument("--hours", type=int, default=24)
    a.add_argument("--model", default="haiku")
    a.add_argument("--profile", help="CLAUDE_CONFIG_DIR to bill the brief to")
    a.add_argument("--apply", action="store_true", help="add suggested to-dos")
    a.add_argument("--notify", action="store_true")
    a.add_argument("--no-pull", action="store_true")
    a.add_argument("--dry-run", action="store_true", help="print the digest, no LLM call")
    a = sub.add_parser("schedule", help="run the brief via launchd, e.g. `cos schedule 09:00 18:00`")
    a.add_argument("times", nargs="*", default=["09:00", "18:00"])
    a.add_argument("--off", action="store_true")
    a = sub.add_parser("install-hooks", help="add cos hooks to every Claude config dir (and Gemini CLI)")
    a.add_argument("--dir", action="append", help="extra CLAUDE_CONFIG_DIR (repeatable)")
    a.add_argument("--codex", action="store_true", help="also wrap Codex's notify setting (experimental)")
    a.add_argument("--remove", action="store_true")
    a = sub.add_parser("setup", help="guided setup: hooks, menu bar, window tracking, VMs, daily brief")
    a.add_argument("-y", "--yes", action="store_true", help="accept defaults without asking")
    sub.add_parser("doctor", help="check that everything is wired up")
    a = sub.add_parser("uninstall", help="remove hooks, menu bar plugin and schedule")
    a.add_argument("--purge", action="store_true", help="also delete ~/.cos data")
    r = sub.add_parser("remote", help="VMs: add NAME TARGET [--dir D] | list | rm NAME | install NAME. "
                                      "TARGET is user@host, an ssh alias, or gcloud:INSTANCE:ZONE:PROJECT")
    r.add_argument("action", choices=["add", "list", "rm", "install"])
    r.add_argument("name", nargs="?")
    r.add_argument("target", nargs="?")
    r.add_argument("--dir", action="append", default=[])
    a = sub.add_parser("pull")
    a.add_argument("name", nargs="?")
    a.add_argument("-q", action="store_true")
    a = sub.add_parser("export")
    a.add_argument("offset", type=int, nargs="?", default=0)
    args = ap.parse_args()
    cmd = args.cmd or "status"

    if cmd == "export":
        return export(args)
    if cmd == "install-hooks":
        return install_hooks(args)
    if cmd == "setup":
        return setup(args)
    if cmd == "doctor":
        return doctor()
    if cmd == "uninstall":
        return uninstall(args)
    if cmd == "notify":
        try:
            notify_line(json.loads(sys.stdin.read()))
        except Exception:
            pass
        return
    if cmd == "schedule":
        return schedule(args)
    if cmd == "open-brief":
        return open_brief()
    if cmd == "config":
        cfg = load_config()
        if args.key == "snap_exclude" and len(args.value) == 2:
            lst = cfg.setdefault("snap_exclude", [])
            if args.value[0] == "add" and args.value[1] not in lst:
                lst.append(args.value[1])
            elif args.value[0] == "rm" and args.value[1] in lst:
                lst.remove(args.value[1])
        elif args.key and args.value:
            v = args.value[0]
            cfg[args.key] = int(v) if v.isdigit() else {"true": True, "false": False}.get(v, v)
        if args.key:
            save_config(cfg)
        print(json.dumps({k: v for k, v in cfg.items() if not k.startswith("last_")}, indent=2))
        return

    con = refresh()
    if cmd == "status":
        print_status(con, getattr(args, "all", False))
    elif cmd == "add":
        i = add_todo(con, " ".join(args.text), parse_due(args), args.stream)
        print("added #%s" % i)
    elif cmd == "add-dialog":
        res = subprocess.run(["osascript", "-e", 'text returned of (display dialog "New to-do (start with tmr: for tomorrow)" '
                              'default answer "" with title "cos")'], stdout=subprocess.PIPE, universal_newlines=True)
        if res.returncode == 0:
            add_todo(con, res.stdout, today())
    elif cmd in ("done", "drop"):
        with con:
            for i in args.ids:
                con.execute("UPDATE todos SET status=?, done_at=? WHERE id=?",
                            ("done" if cmd == "done" else "dropped", now(), i))
    elif cmd == "move":
        due = {"today": today(), "tomorrow": today(1), "someday": None}.get(args.when, args.when)
        with con:
            con.execute("UPDATE todos SET due=? WHERE id=?", (due, args.id))
    elif cmd in ("today", "tomorrow"):
        print_todos(todos(con, cmd))
    elif cmd == "todos":
        for t in todos(con, "all"):
            print("  #%-3d %-10s %s" % (t["id"], t["due"] or "someday", t["text"]))
    elif cmd == "log":
        start = int(time.mktime(dt.date.today().timetuple()))
        for t in con.execute("SELECT * FROM todos WHERE status='done' AND done_at>=?", (start,)):
            print("  ✔ %s" % t["text"])
    elif cmd == "focus":
        focus(con, args.sid)
    elif cmd == "hide":
        with con:
            con.execute("UPDATE sessions SET hidden=1 WHERE id LIKE ?", (args.sid + "%",))
    elif cmd == "bar":
        bar(con)
    elif cmd == "snap":
        print("recorded %d tabs/windows" % snap(con))
    elif cmd == "tick":
        tick(con)
    elif cmd == "windows":
        print("\n".join(windows_digest(con, now() - args.hours * 3600)).strip() or "nothing recorded yet")
    elif cmd == "brief":
        brief(con, args)
    elif cmd == "pull":
        pull(con, args.name, args.q)
    elif cmd == "remote":
        if args.action == "list":
            for r in con.execute("SELECT * FROM remotes"):
                print("%-12s %-30s pulled %s ago %s" % (r["name"], r["ssh"], age(r["last_pull"]), r["last_error"] or ""))
        elif args.action == "rm":
            with con:
                con.execute("DELETE FROM remotes WHERE name=?", (args.name,))
        else:
            if args.action == "add":
                if not (args.name and args.target):
                    sys.exit("usage: cos remote add NAME TARGET [--dir CLAUDE_CONFIG_DIR ...]")
                with con:
                    con.execute("INSERT OR REPLACE INTO remotes(name, ssh) VALUES(?,?)", (args.name, args.target))
            r = con.execute("SELECT * FROM remotes WHERE name=?", (args.name,)).fetchone()
            if not r:
                sys.exit("unknown remote")
            remote_install(r["ssh"], args.dir)
            pull(con, args.name)


if __name__ == "__main__":
    main()
