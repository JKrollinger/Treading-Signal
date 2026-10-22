"""Cloudflare quick tunnel for the TradingView webhook.

``cloudflared tunnel --url http://127.0.0.1:<port>`` gives the local webhook a public
HTTPS address (``https://<random>.trycloudflare.com``) with no account. The address
changes every time the tunnel starts, so the scheduler runs the tunnel itself, keeps it
alive, stores the current address in ``data/tunnel_url.txt`` and sends the full webhook
URL to Telegram whenever it changes.

Lifetime: Task Scheduler stops the scheduler with TerminateProcess, so no Python cleanup
runs and a plain child process would outlive it (one orphaned cloudflared per restart).
Each cloudflared is therefore put in a Windows job object with KILL_ON_JOB_CLOSE, owned by
this process: when the scheduler dies for any reason the OS closes the handle and kills
the tunnel with it. On start, ``kill_stale()`` also removes any cloudflared left behind by
an earlier run (same config file or same port).
"""
from __future__ import annotations

import atexit
import ctypes
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
RESTART_DELAY_SECONDS = 20
CANDIDATES = [
    r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
    r"C:\Program Files\cloudflared\cloudflared.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\WinGet\Links\cloudflared.exe"),
]


def find_cloudflared() -> str | None:
    if config.CLOUDFLARED_PATH and Path(config.CLOUDFLARED_PATH).exists():
        return config.CLOUDFLARED_PATH
    found = shutil.which("cloudflared")
    if found:
        return found
    for cand in CANDIDATES:
        if cand and Path(cand).exists():
            return cand
    return None


def url_file() -> Path:
    return config.DATA_DIR / "tunnel_url.txt"


def config_path() -> Path:
    """Our own cloudflared config; its path also marks a cloudflared process as ours."""
    return config.DATA_DIR / "cloudflared_signalbot.yml"


# Windows job object: cloudflared dies with the scheduler -----------------------------
if os.name == "nt":
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_SET_QUOTA, _PROCESS_TERMINATE = 0x0100, 0x0001


def _job_for(pid: int):
    """Put ``pid`` in a fresh kill-on-close job. Returns the job handle (keep it alive) or None."""
    if os.name != "nt":
        return None
    job = _k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _k32.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info))
    hproc = _k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid) if ok else None
    if hproc:
        ok = _k32.AssignProcessToJobObject(job, hproc)
        _k32.CloseHandle(hproc)
    else:
        ok = False
    if not ok:
        log.warning("could not tie cloudflared (pid %d) to the scheduler's lifetime (WinError %d); "
                    "kill_stale() will clean it up on the next start", pid, ctypes.get_last_error())
        _k32.CloseHandle(job)
        return None
    return job


def _close_job(job) -> None:
    """Closing the last handle kills whatever is still running in the job."""
    if job and os.name == "nt":
        _k32.CloseHandle(job)


# Stale tunnels from an earlier run ---------------------------------------------------
def stale_pids(port: int = config.TV_WEBHOOK_PORT) -> list[int]:
    """PIDs of cloudflared processes started by this bot (our config file or our port)."""
    if os.name != "nt":
        return []
    ps = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
          "Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" | "
          "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not list cloudflared processes: %s", exc)
        return []
    marks = (str(config_path()).lower(), f"--url http://127.0.0.1:{port}".lower())
    pids = []
    for line in out.stdout.splitlines():
        pid_s, _, cmdline = line.partition("\t")
        if pid_s.strip().isdigit() and any(m in cmdline.lower() for m in marks):
            pids.append(int(pid_s))
    return pids


def kill_stale(port: int = config.TV_WEBHOOK_PORT) -> int:
    """Kill cloudflared processes left behind by an earlier scheduler run; returns the count."""
    pids = stale_pids(port)
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if pids:
        log.info("killed %d stale cloudflared process(es) from an earlier run: %s",
                 len(pids), ", ".join(str(p) for p in pids))
    return len(pids)


def current_url() -> str | None:
    try:
        text = url_file().read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


def webhook_url(public_url: str | None = None, with_key: bool = True) -> str | None:
    base = public_url or current_url()
    if not base:
        return None
    return f"{base}/tv?key={config.TV_WEBHOOK_SECRET}" if with_key else f"{base}/tv?key=<TV_WEBHOOK_SECRET>"


def notify_url(public_url: str) -> None:
    from . import telegram_notify as tg

    full = webhook_url(public_url)
    if tg.is_thai():
        text = "\n".join([
            "🔗 URL สำหรับ webhook ของ TradingView (เปลี่ยนใหม่)",
            full or public_url,
            "นำไปวางในช่อง Webhook URL ของ alert ใน TradingView (ถ้าเคยตั้งไว้แล้วต้องแก้เป็น URL นี้ เพราะ URL เปลี่ยนทุกครั้งที่บอทเริ่มใหม่)",
        ])
    else:
        text = "\n".join([
            "TradingView webhook URL (changed)",
            full or public_url,
            "Paste it into the alert's Webhook URL field; it changes every time the bot restarts.",
        ])
    tg.send_message(text, admin_only=True)   # contains the secret: owner chat only


class QuickTunnel:
    def __init__(self, port: int, binary: str, notify: bool = True) -> None:
        self.port = port
        self.binary = binary
        self.notify = notify
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None
        self._stop = threading.Event()
        self._log_path = config.PROJECT_ROOT / "logs" / "cloudflared.log"
        self._job = None   # kill-on-close job holding the current cloudflared

    def start(self) -> None:
        threading.Thread(target=self._supervise, name="cloudflared", daemon=True).start()

    def stop(self) -> None:
        """Stop supervising and end the tunnel process (safe to call more than once)."""
        self._stop.set()
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.info("cloudflared stopped (pid %s)", proc.pid)
        _close_job(self._job)
        self._job = None

    def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                self._run_once()
            except Exception:  # noqa: BLE001
                log.exception("cloudflared supervisor error")
            if self._stop.is_set():
                break
            log.warning("cloudflared exited; restarting in %d s", RESTART_DELAY_SECONDS)
            time.sleep(RESTART_DELAY_SECONDS)

    def _config_file(self) -> Path:
        """Own config so cloudflared never picks up another project's ~/.cloudflared/config.yml.

        Without this, ingress rules from an existing config win over ``--url`` and every
        request ends in that config's catch-all (usually an empty 404).
        """
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Generated by Signal Bot; isolated from any other cloudflared config on this PC\n"
            f"url: http://127.0.0.1:{self.port}\n"
            "no-autoupdate: true\n",
            encoding="utf-8",
        )
        return path

    def _run_once(self) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [self.binary, "--config", str(self._config_file()), "tunnel",
             "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", creationflags=flags,
        )
        self.proc = proc
        _close_job(self._job)                 # previous (already exited) tunnel's job
        self._job = _job_for(proc.pid)        # this one dies with us, even on a hard kill
        log.info("cloudflared started (pid %s) for port %d%s", proc.pid, self.port,
                 "" if self._job else " (NOT tied to the scheduler's lifetime)")
        assert proc.stderr is not None
        with self._log_path.open("a", encoding="utf-8") as logfile:
            for line in proc.stderr:
                logfile.write(line)
                logfile.flush()
                m = URL_RE.search(line)
                if m:
                    self._on_url(m.group(0))
        proc.wait()

    def _on_url(self, url: str) -> None:
        if url == self.url:
            return
        self.url = url
        previous = current_url()
        url_file().parent.mkdir(parents=True, exist_ok=True)
        url_file().write_text(url + "\n", encoding="utf-8")
        log.info("tunnel URL: %s  ->  webhook %s", url, webhook_url(url, with_key=False))
        if self.notify and url != previous:
            try:
                notify_url(url)
            except Exception:  # noqa: BLE001
                log.exception("could not send the tunnel URL to Telegram")


def start_in_background(port: int = config.TV_WEBHOOK_PORT, notify: bool = config.TUNNEL_NOTIFY) -> QuickTunnel | None:
    binary = find_cloudflared()
    if not binary:
        log.warning("cloudflared not found; tunnel not started (winget install Cloudflare.cloudflared)")
        return None
    kill_stale(port)                      # tunnels an earlier run left behind
    tunnel = QuickTunnel(port, binary, notify=notify)
    atexit.register(tunnel.stop)          # normal exit and Ctrl+C; a hard kill is covered by the job
    tunnel.start()
    return tunnel
