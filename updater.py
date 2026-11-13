"""Auto update from git with rollback (spec section 14). Used by watchdog.py; the bot only cooperates.

    plan()      git fetch; is there a new commit we can take?  -> ("none"|"apply"|"refuse", reason, info)
    apply()     git pull --ff-only (+ pip install if requirements.txt changed). Fails closed, aborts partial merges.
    rollback()  git reset --hard <previous commit>

Rules: only a clean working tree and a fast-forward are ever applied. A dirty tree, local commits, or a failing
pull keep the running version; the reason (and the conflicting files) is reported once per remote head. The
restart happens only at a SAFE moment chosen by the bot itself (update.request -> bot exits cleanly at the end of a
pass with an empty inbox -> update.ready). Positions are never touched: they sit at the broker with their SL/TP.

Notices for Discord are written to logs/state/notices/*.json; the bot delivers them (one sender).
"""
from __future__ import annotations
import json, logging, os, subprocess, sys, time, uuid
import config as C

log = logging.getLogger("aureon.updater")
UPDATE_ENABLED = C.env("UPDATE_ENABLED", "1") == "1"
UPDATE_BRANCH = C.env("UPDATE_BRANCH", "main")
try:
    UPDATE_CHECK_MIN = float(C.env("UPDATE_CHECK_MIN", "5"))
except ValueError:
    raise SystemExit("UPDATE_CHECK_MIN in .env must be a number of minutes")
HEALTH_SECONDS = 90                                   # the new version must beat within this, and not crash
STATE_FILE = os.path.join(C.STATE_DIR, "update.json")
REQUEST_FILE = os.path.join(C.STATE_DIR, "update.request")
READY_FILE = os.path.join(C.STATE_DIR, "update.ready")
NOTICE_DIR = os.path.join(C.DATA_DIR, "notices")


def git(root: str, *args: str, timeout: int = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).rstrip()        # not strip(): porcelain lines start with a space
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(d: dict):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, STATE_FILE)


def notice(kind: str, title: str, desc: str = "", **extra):
    """Queue a message for the bot to deliver. kind: updated | update_skipped | rolled_back | crashed."""
    os.makedirs(NOTICE_DIR, exist_ok=True)
    path = os.path.join(NOTICE_DIR, f"{time.time():017.6f}-{uuid.uuid4().hex[:6]}")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"kind": kind, "title": title, "desc": desc, **extra}, f)
    os.replace(path + ".tmp", path + ".json")


def version_at(root: str) -> str:
    try:
        with open(os.path.join(root, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "?"


def plan(root: str, branch: str = UPDATE_BRANCH) -> tuple[str, str, dict]:
    code, out = git(root, "fetch", "--quiet", "origin", branch)
    if code:
        return "none", f"git fetch failed: {out[-300:]}", {}
    _, local = git(root, "rev-parse", "HEAD")
    code, remote = git(root, "rev-parse", f"origin/{branch}")
    local, remote = local.strip(), remote.strip()
    if code:
        return "none", f"no branch origin/{branch}", {}
    info = {"local": local, "remote": remote}
    if local == remote:
        return "none", "up to date", info
    if git(root, "merge-base", "--is-ancestor", remote, "HEAD")[0] == 0:
        return "none", "local is ahead of the remote", info
    _, changed = git(root, "diff", "--name-only", "HEAD", remote)
    changed_files = [x for x in changed.splitlines() if x]
    info["changed"] = changed_files
    _, st = git(root, "status", "--porcelain", "--untracked-files=all")
    dirty = [line[3:].split(" -> ")[-1] for line in st.splitlines() if line.strip()]
    if git(root, "merge-base", "--is-ancestor", "HEAD", remote)[0] != 0:
        _, mine = git(root, "log", "--oneline", f"{remote}..HEAD")
        return "refuse", "not a fast-forward: this machine has commits the remote does not", {
            **info, "conflicts": [], "local_commits": mine.splitlines()[:10]}
    tracked_dirty = [line[3:].split(" -> ")[-1] for line in st.splitlines() if line.strip() and not line.startswith("??")]
    blocking_untracked = [f for f in dirty if f in changed_files and f not in tracked_dirty]
    if tracked_dirty or blocking_untracked:
        return "refuse", "the working tree has local changes", {
            **info, "conflicts": sorted(set(tracked_dirty + blocking_untracked) & set(changed_files)),
            "dirty": sorted(set(tracked_dirty + blocking_untracked))}
    return "apply", f"{local[:7]} -> {remote[:7]}", info


def apply(root: str, branch: str = UPDATE_BRANCH, pip=None) -> tuple[bool, str, dict]:
    """Fast-forward to origin/<branch>. Returns (ok, reason, {"prev","new"}). On any failure the tree is left at
    the previous commit (partial merge aborted, or reset)."""
    prev = git(root, "rev-parse", "HEAD")[1].strip()
    code, out = git(root, "pull", "--ff-only", "origin", branch)
    if code:
        if os.path.exists(os.path.join(root, ".git", "MERGE_HEAD")):
            git(root, "merge", "--abort")
        if git(root, "rev-parse", "HEAD")[1].strip() != prev:
            git(root, "reset", "--hard", prev)
        _, st = git(root, "diff", "--name-only", "--diff-filter=U")
        return False, f"git pull --ff-only failed: {out[-300:]}", {"prev": prev, "conflicts": st.splitlines()}
    new = git(root, "rev-parse", "HEAD")[1].strip()
    _, changed = git(root, "diff", "--name-only", prev, new)
    if "requirements.txt" in changed.splitlines():
        ok, msg = (pip or _pip)(root)
        if not ok:
            rollback(root, prev)
            return False, f"pip install failed, update undone: {msg[-300:]}", {"prev": prev, "new": new}
    return True, f"{prev[:7]} -> {new[:7]}", {"prev": prev, "new": new}


def _pip(root: str) -> tuple[bool, str]:
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], cwd=root,
                           capture_output=True, text=True, timeout=900)
        return r.returncode == 0, (r.stdout + r.stderr)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def rollback(root: str, sha: str) -> bool:
    if os.path.exists(os.path.join(root, ".git", "MERGE_HEAD")):
        git(root, "merge", "--abort")
    return git(root, "reset", "--hard", sha)[0] == 0


# ---------------------------------------------------------------------------- bot side of the handshake
def restart_requested() -> bool:
    return os.path.exists(REQUEST_FILE)


def ready_for_restart():
    """Called by the bot at a safe moment: it writes update.ready and then exits cleanly."""
    with open(READY_FILE, "w") as f:
        f.write(str(time.time()))


def pop_notices() -> list[dict]:
    out = []
    if not os.path.isdir(NOTICE_DIR):
        return out
    for name in sorted(os.listdir(NOTICE_DIR)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(NOTICE_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            pass
        try:
            os.remove(path)
        except OSError:
            pass
    return out
