"""HandBrakeCLI wrapper: detection, preset listing, probing and process control."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from typing import Callable, Iterable

IS_WINDOWS = sys.platform == "win32"
FLATPAK_CMD = ["flatpak", "run", "--command=HandBrakeCLI", "fr.handbrake.ghb"]

# Keep HandBrakeCLI from popping up console windows on Windows.
_POPEN_FLAGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}

_JSON_BLOCK_START = re.compile(r"^([A-Za-z][A-Za-z ]*): \{\s*$")
_DURATION_LOG = re.compile(r"^\s*\+ duration: (\d+):(\d\d):(\d\d)")


def split_args(s: str) -> list[str]:
    """shlex.split that keeps Windows backslashes intact."""
    if not IS_WINDOWS:
        return shlex.split(s)
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t
            for t in shlex.split(s, posix=False)]


def detect_command() -> list[str] | None:
    """Find a usable HandBrakeCLI: native binary on PATH first, then the Flatpak."""
    exe = shutil.which("HandBrakeCLI")
    if exe:
        return [exe]
    if shutil.which("flatpak"):
        try:
            r = subprocess.run(["flatpak", "info", "fr.handbrake.ghb"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            if r.returncode == 0:
                return list(FLATPAK_CMD)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def list_presets(cmd: list[str]) -> list[dict]:
    """Return [{"category": str, "name": str, "description": str}] from --preset-list."""
    r = subprocess.run(cmd + ["--preset-import-gui", "--preset-list"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, encoding="utf-8", errors="replace", timeout=120, **_POPEN_FLAGS)
    presets: list[dict] = []
    category = None
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        if not line.startswith(" ") and line.rstrip().endswith("/") and not line.startswith("["):
            category = line.rstrip()[:-1]
        elif category is not None and line.startswith("        "):
            if presets:
                d = presets[-1]["description"]
                presets[-1]["description"] = (d + " " + line.strip()).strip()
        elif category is not None and line.startswith("    ") and not line.startswith("     "):
            presets.append({"category": category, "name": line.strip(), "description": ""})
        elif not line.startswith(" "):
            category = None  # log noise, e.g. "HandBrake has exited."
    return presets


def probe_duration(cmd: list[str], path: str, method: str = "auto") -> float | None:
    """Media duration in seconds, via ffprobe (fast) or a HandBrake scan."""
    if method in ("auto", "ffprobe"):
        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            try:
                r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                    "-of", "default=nw=1:nk=1", path],
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   timeout=60, **_POPEN_FLAGS)
                value = float(r.stdout.strip().splitlines()[0])
                if value > 0:
                    return value
            except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
                pass
        if method == "ffprobe":
            return None
    if method in ("auto", "handbrake"):
        try:
            r = subprocess.run(cmd + ["--json", "--scan", "-i", path],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                               errors="replace", timeout=300, **_POPEN_FLAGS)
        except (OSError, subprocess.TimeoutExpired):
            return None
        for name, obj in iter_json_blocks(r.stdout.splitlines()):
            if name == "JSON Title Set":
                try:
                    ticks = obj["TitleList"][0]["Duration"]["Ticks"]
                    return ticks / 90000.0 if ticks > 0 else None
                except (KeyError, IndexError, TypeError):
                    return None
    return None


def iter_json_blocks(lines: Iterable[str]):
    """Yield (name, obj) for the 'Name: {...}' blocks HandBrakeCLI --json prints."""
    name = None
    buf: list[str] = []
    for line in lines:
        line = line.rstrip("\r\n")
        if name is None:
            m = _JSON_BLOCK_START.match(line)
            if m:
                name, buf = m.group(1), ["{"]
            continue
        buf.append(line)
        if line == "}":
            try:
                yield name, json.loads("\n".join(buf))
            except json.JSONDecodeError:
                pass
            name, buf = None, []


def parse_log_duration(line: str) -> float | None:
    m = _DURATION_LOG.match(line)
    if m:
        h, mi, s = map(int, m.groups())
        return h * 3600 + mi * 60 + s
    return None


def _kill_with_parent_windows():
    """A job object that kills its processes when hbmon exits, however it exits."""
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMITS(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC_LIMITS), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    info = EXTENDED_LIMITS()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    return lambda proc: k32.AssignProcessToJobObject(job, int(proc._handle))


def _kill_with_parent_linux():
    """preexec_fn setting PR_SET_PDEATHSIG, resolved up front so the child only makes a syscall."""
    import ctypes
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    return lambda: prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


try:
    _assign_job = _kill_with_parent_windows() if IS_WINDOWS else None
    _preexec = _kill_with_parent_linux() if sys.platform.startswith("linux") else None
except (OSError, AttributeError):
    _assign_job = _preexec = None


class EncodeProcess:
    """A running HandBrakeCLI encode with pause/resume/abort support.

    Pausing suspends the whole process tree (NtSuspendProcess on Windows,
    SIGSTOP on POSIX), which also works through `flatpak run`'s sandbox.
    """

    def __init__(self, argv: list[str], on_progress: Callable[[str, dict], None],
                 on_log: Callable[[str], None]):
        kwargs = dict(_POPEN_FLAGS)
        if not IS_WINDOWS:
            kwargs["start_new_session"] = True  # keep Ctrl+C on the server from hitting it
        if _preexec:
            kwargs["preexec_fn"] = _preexec
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                     errors="replace", bufsize=1, **kwargs)
        if _assign_job:
            _assign_job(self.proc)
        self.paused = False
        self.aborted = False
        self._lock = threading.Lock()
        self._threads = [
            threading.Thread(target=self._read_stdout, args=(on_progress,), daemon=True),
            threading.Thread(target=self._read_stderr, args=(on_log,), daemon=True),
        ]
        for t in self._threads:
            t.start()

    def _read_stdout(self, on_progress):
        for name, obj in iter_json_blocks(self.proc.stdout):
            on_progress(name, obj)

    def _read_stderr(self, on_log):
        for line in self.proc.stderr:
            on_log(line.rstrip("\r\n"))

    def wait(self) -> int:
        rc = self.proc.wait()
        for t in self._threads:
            t.join(timeout=5)
        return rc

    # -- process tree control -------------------------------------------------

    def _tree_pids(self) -> list[int]:
        """The process and all its descendants (POSIX only)."""
        root = self.proc.pid
        try:
            out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True,
                                 text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            return [root]
        children: dict[int, list[int]] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                children.setdefault(int(parts[1]), []).append(int(parts[0]))
        pids, stack = [], [root]
        while stack:
            pid = stack.pop()
            pids.append(pid)
            stack.extend(children.get(pid, []))
        return pids

    def _signal_tree(self, sig) -> None:
        for pid in self._tree_pids():
            try:
                os.kill(pid, sig)
            except OSError:
                pass

    def _nt_call(self, fn_name: str) -> None:
        import ctypes
        fn = getattr(ctypes.windll.ntdll, fn_name)
        fn.argtypes = [ctypes.c_void_p]
        fn(ctypes.c_void_p(int(self.proc._handle)))

    def pause(self) -> None:
        with self._lock:
            if self.paused or self.proc.poll() is not None:
                return
            if IS_WINDOWS:
                self._nt_call("NtSuspendProcess")
            else:
                self._signal_tree(signal.SIGSTOP)
            self.paused = True

    def resume(self) -> None:
        with self._lock:
            if not self.paused:
                return
            if IS_WINDOWS:
                self._nt_call("NtResumeProcess")
            else:
                self._signal_tree(signal.SIGCONT)
            self.paused = False

    def abort(self, grace: float = 8.0) -> None:
        """Ask HandBrakeCLI to quit, then escalate to terminate/kill."""
        self.aborted = True
        if self.proc.poll() is not None:
            return
        if IS_WINDOWS:
            # The Windows CLI reads keys from the console, not stdin; just terminate.
            self.proc.kill()
            return
        self.resume()
        try:
            self.proc.stdin.write("q\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=grace)
            return
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        self._signal_tree(signal.SIGTERM)
        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self._signal_tree(signal.SIGKILL)
