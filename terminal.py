"""The ONE MT5 terminal this bot owns: find it, stop it, start it. Used only by guardian.py, only when
MT5_TERMINAL_RESTART_ENABLED=true and MT5_TERMINAL_PATH is set.

Ownership: a process is ours only if its executable path is exactly MT5_TERMINAL_PATH (case-insensitive, normalised)
and, when MT5_TERMINAL_DATA_DIR is a different folder from the install folder, its command line names that data
folder. Every other terminal64.exe (other brokers, other accounts) is never touched. No credentials are passed or
logged: the terminal logs in with the account saved in it. Restarting a terminal does not touch the broker-side
SL/TP of open positions (they live on the broker's server); the bot sends no order while recovering.
"""
from __future__ import annotations
import json, logging, ntpath, os, subprocess, time

log = logging.getLogger("terminal")


def _norm(p: str) -> str:
    """A Windows path, compared the Windows way (case-insensitive, / and \\ alike) on any OS."""
    return ntpath.normcase(ntpath.normpath(p)) if p else ""


def list_processes() -> list[dict]:
    """[{pid, exe, cmdline}] of running terminal processes. psutil if installed, else PowerShell (Windows)."""
    try:
        import psutil
        out = []
        for p in psutil.process_iter(["pid", "exe", "cmdline", "name"]):
            if (p.info.get("name") or "").lower().startswith("terminal"):
                out.append({"pid": p.info["pid"], "exe": p.info.get("exe") or "", "cmdline": " ".join(p.info.get("cmdline") or [])})
        return out
    except ImportError:
        pass
    if os.name != "nt":
        return []
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'terminal%'\" | "
          "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30)
    if r.returncode or not r.stdout.strip():
        return []
    rows = json.loads(r.stdout)
    rows = rows if isinstance(rows, list) else [rows]
    return [{"pid": int(x["ProcessId"]), "exe": x.get("ExecutablePath") or "", "cmdline": x.get("CommandLine") or ""}
            for x in rows]


class TerminalProcess:
    def __init__(self, path: str, data_dir: str = "", lister=list_processes, runner=subprocess, sleep=time.sleep,
                 clock=time.monotonic):
        self.path, self.data_dir = path, data_dir
        self.lister, self.runner, self.sleep, self.clock = lister, runner, sleep, clock

    @property
    def portable(self) -> bool:
        return bool(self.data_dir) and _norm(self.data_dir) == _norm(ntpath.dirname(self.path))

    def owns(self, proc: dict) -> bool:
        if not self.path or _norm(proc.get("exe", "")) != _norm(self.path):
            return False
        if self.data_dir and not self.portable:
            return _norm(self.data_dir) in _norm(proc.get("cmdline", ""))
        return True

    def owned_pids(self) -> list[int]:
        return [p["pid"] for p in self.lister() if self.owns(p)]

    def stop(self, timeout: float = 20.0) -> bool:
        """Close our terminal (gracefully first). True when none of ours is left."""
        pids = self.owned_pids()
        for pid in pids:
            self._kill(pid, force=False)
        end = self.clock() + timeout
        while self.owned_pids() and self.clock() < end:
            self.sleep(1)
        for pid in self.owned_pids():
            log.warning("terminal PID %s did not close; forcing it", pid)
            self._kill(pid, force=True)
        return not self.owned_pids()

    def _kill(self, pid: int, force: bool):
        if os.name == "nt":
            args = ["taskkill", "/PID", str(pid)] + (["/F"] if force else [])
            self.runner.run(args, capture_output=True, timeout=30)
        else:
            import signal
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)

    def start(self) -> int | None:
        if not self.path or not os.path.exists(self.path):
            log.error("MT5_TERMINAL_PATH %r does not exist", self.path)
            return None
        args = [self.path] + (["/portable"] if self.portable else [])
        p = self.runner.Popen(args, cwd=ntpath.dirname(self.path))
        log.warning("started the owned MT5 terminal (PID %s)", getattr(p, "pid", "?"))
        return getattr(p, "pid", None)

    def restart(self) -> tuple[bool, str]:
        if not self.path:
            return False, "MT5_TERMINAL_PATH is not set"
        stopped = self.stop()
        if not stopped:
            return False, "the owned terminal could not be stopped"
        pid = self.start()
        return (pid is not None), (f"restarted (PID {pid})" if pid else "the terminal could not be started")
