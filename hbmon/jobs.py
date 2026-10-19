"""Job queue, runner thread and persistence."""

from __future__ import annotations

import collections
import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import handbrake

VIDEO_EXTS = {
    ".3gp", ".asf", ".avi", ".divx", ".dv", ".f4v", ".flv", ".m2t", ".m2ts", ".m4v", ".mkv",
    ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".mxf", ".ogm", ".ogv", ".rm", ".rmvb", ".ts",
    ".vob", ".webm", ".wmv", ".y4m",
}
PART_TAG = ".hbmon-part"
# For a failed encode, the last log line that looks like this is shown as the reason.
_ERROR_HINT = re.compile(r"invalid|no title found|error|fail|cannot|unable|not found", re.I)
_ERROR_NOISE = re.compile(r"nvEncodeAPI|libnvidia|qsv:|vcn:|mfx", re.I)
LOG_TAIL = 300


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def is_hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attrs & 0x2)  # FILE_ATTRIBUTE_HIDDEN


def part_path(job: "Job") -> Path:
    """Encodes go to a temp name next to the target and are renamed on success."""
    dst = Path(job.dst)
    return dst.with_name(dst.stem + PART_TAG + dst.suffix)


@dataclass
class Job:
    id: str
    src: str
    dst: str
    preset: str
    extra_args: list[str] = field(default_factory=list)
    overwrite: bool = False
    status: str = "queued"  # queued | running | done | failed
    progress: float = 0.0
    size: int = 0
    duration: float | None = None
    added_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    elapsed: float = 0.0  # active (non-paused) encoding seconds
    out_size: int = 0
    eta: float | None = None
    fps: float | None = None
    fps_avg: float | None = None
    pass_no: int = 0
    pass_count: int = 0
    error: str | None = None
    exit_code: int | None = None

    def public(self) -> dict:
        d = asdict(self)
        d["name"] = os.path.basename(self.src)
        return d


class QueueManager:
    def __init__(self, hb_cmd: list[str], state_dir: Path, probe: str = "auto"):
        self.hb_cmd = hb_cmd
        self.state_dir = state_dir
        self.log_dir = state_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = state_dir / "state.json"
        self.probe_method = probe

        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.jobs: list[Job] = []
        self.state = "idle"  # idle | running | paused
        self.stop_after_current = False
        self.session_started: float | None = None
        self.session_finished: float | None = None
        self.current: Job | None = None
        self.proc: handbrake.EncodeProcess | None = None
        self.logs: dict[str, collections.deque] = {}
        self._run_since: float | None = None
        self._dirty = False
        self._shutdown = False

        self._probe_q: collections.deque[str] = collections.deque()
        self._probe_cond = threading.Condition()
        self._presets: list[dict] | None = None
        self._presets_lock = threading.Lock()

        self._load()
        self._runner_thread = threading.Thread(target=self._runner, daemon=True)
        self._runner_thread.start()
        for target in (self._prober, self._saver):
            threading.Thread(target=target, daemon=True).start()
        with self._probe_cond:
            self._probe_q.extend(j.id for j in self.jobs if j.duration is None)
            self._probe_cond.notify()

    # -- persistence ------------------------------------------------------------

    def _load(self):
        try:
            data = json.loads(self.state_file.read_text("utf-8"))
        except (OSError, ValueError):
            return
        names = Job.__dataclass_fields__.keys()
        for raw in data.get("jobs", []):
            job = Job(**{k: v for k, v in raw.items() if k in names})
            if job.status == "running":  # interrupted by a server crash
                job.status, job.progress, job.elapsed, job.eta = "queued", 0.0, 0.0, None
                try:
                    part_path(job).unlink()
                except OSError:
                    pass
            self.jobs.append(job)

    def _save(self):
        with self.lock:
            data = {"jobs": [asdict(j) for j in self.jobs]}
            self._dirty = False
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), "utf-8")
        os.replace(tmp, self.state_file)

    def _saver(self):
        while not self._shutdown:
            time.sleep(2)
            if self._dirty:
                try:
                    self._save()
                except OSError:
                    pass

    def mark_dirty(self):
        self._dirty = True

    # -- presets / probing --------------------------------------------------------

    def presets(self, refresh: bool = False) -> list[dict]:
        with self._presets_lock:
            if self._presets is None or refresh:
                self._presets = handbrake.list_presets(self.hb_cmd)
            return self._presets

    def _prober(self):
        while not self._shutdown:
            with self._probe_cond:
                while not self._probe_q:
                    self._probe_cond.wait()
                job_id = self._probe_q.popleft()
            job = self.find(job_id)
            if job is None or job.duration is not None:
                continue
            dur = handbrake.probe_duration(self.hb_cmd, job.src, self.probe_method)
            with self.lock:
                if job.duration is None and dur:
                    job.duration = dur
                    self.mark_dirty()

    # -- queue editing ------------------------------------------------------------

    def find(self, job_id: str) -> Job | None:
        with self.lock:
            return next((j for j in self.jobs if j.id == job_id), None)

    def add(self, paths: list[str], dest: str, preset: str, ext: str = "mkv",
            keep_structure: bool = True, existing: str = "skip", extra_args: str = "",
            exts: set[str] | None = None) -> dict:
        exts = exts or VIDEO_EXTS
        ext = "." + ext.lstrip(".")
        extra = handbrake.split_args(extra_args) if extra_args.strip() else []
        dest_root = Path(os.path.expanduser(dest)).absolute()
        added, skipped = [], []

        # (source file, output dir) pairs
        plan: list[tuple[Path, Path]] = []
        for p in paths:
            src = Path(p)
            if src.is_dir():
                base = dest_root / src.name if keep_structure else dest_root
                for f in self._walk_videos(src, exts):
                    rel = f.parent.relative_to(src)
                    plan.append((f, base / rel if keep_structure else base))
            elif src.is_file():
                plan.append((src, dest_root))
            else:
                skipped.append({"src": p, "reason": "not found"})

        with self.lock:
            taken = {os.path.normcase(j.dst) for j in self.jobs if j.status in ("queued", "running")}
            for src, out_dir in plan:
                dst = out_dir / (src.stem + ext)
                key = os.path.normcase(str(dst))
                same_as_src = key == os.path.normcase(str(src.absolute()))
                if key in taken and existing != "rename":
                    skipped.append({"src": str(src), "reason": "already queued to same output"})
                    continue
                if (dst.exists() or same_as_src) and existing == "skip":
                    skipped.append({"src": str(src), "reason": "output exists"})
                    continue
                if existing == "rename" or same_as_src:
                    n = 1
                    while key in taken or dst.exists() or key == os.path.normcase(str(src.absolute())):
                        dst = out_dir / f"{src.stem} ({n}){ext}"
                        key = os.path.normcase(str(dst))
                        n += 1
                try:
                    size = src.stat().st_size
                except OSError:
                    size = 0
                job = Job(id=uuid.uuid4().hex[:12], src=str(src.absolute()), dst=str(dst),
                          preset=preset, extra_args=extra, overwrite=existing == "overwrite",
                          size=size, added_at=time.time())
                self.jobs.append(job)
                taken.add(key)
                added.append(job.id)
            self.mark_dirty()
            self.cond.notify_all()
        with self._probe_cond:
            self._probe_q.extend(added)
            self._probe_cond.notify()
        return {"added": len(added), "skipped": skipped}

    @staticmethod
    def _walk_videos(root: Path, exts: set[str]) -> list[Path]:
        out: list[Path] = []
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                entries = sorted(os.scandir(d), key=lambda e: natural_key(e.name))
            except OSError:
                continue
            subdirs = []
            for e in entries:
                try:
                    if is_hidden(e):
                        continue
                    if e.is_dir():
                        subdirs.append(Path(e.path))
                    elif e.is_file() and os.path.splitext(e.name)[1].lower() in exts:
                        out.append(Path(e.path))
                except OSError:
                    continue
            stack.extend(reversed(subdirs))
        return out

    def remove(self, ids: list[str]) -> None:
        ids_set = set(ids)
        with self.lock:
            if self.current and self.current.id in ids_set:
                self._abort_current_locked("Removed by user")
            for j in self.jobs:
                if j.id in ids_set:
                    self.logs.pop(j.id, None)
                    try:
                        (self.log_dir / f"{j.id}.log").unlink()
                    except OSError:
                        pass
            self.jobs = [j for j in self.jobs if j.id not in ids_set]
            self.mark_dirty()

    def move(self, ids: list[str], before: str | None) -> None:
        with self.lock:
            ids_set = set(ids) - ({self.current.id} if self.current else set())
            moving = [j for j in self.jobs if j.id in ids_set]
            rest = [j for j in self.jobs if j.id not in ids_set]
            idx = next((i for i, j in enumerate(rest) if j.id == before), len(rest))
            self.jobs = rest[:idx] + moving + rest[idx:]
            self.mark_dirty()

    def retry(self, ids: list[str]) -> None:
        with self.lock:
            for j in self.jobs:
                if j.id in ids and j.status in ("done", "failed"):
                    j.status, j.progress, j.error, j.exit_code = "queued", 0.0, None, None
                    j.elapsed, j.eta, j.out_size, j.started_at, j.finished_at = 0.0, None, 0, None, None
                    j.fps = j.fps_avg = None
                    if os.path.exists(j.dst):
                        j.overwrite = True  # retrying a finished job means "redo it"
            self.mark_dirty()
            self.cond.notify_all()

    def clear_done(self) -> None:
        with self.lock:
            done = [j.id for j in self.jobs if j.status == "done"]
        self.remove(done)

    # -- runner control -----------------------------------------------------------

    def control(self, action: str) -> None:
        with self.lock:
            if action == "start":
                if self.state == "paused":
                    self._resume_locked()
                elif self.state == "idle":
                    self.state = "running"
                    self.session_started = time.time()
                    self.session_finished = None
                self.stop_after_current = False
            elif action == "pause":
                if self.state == "running":
                    if self.proc:
                        self.proc.pause()
                    if self._run_since is not None and self.current:
                        self.current.elapsed += time.time() - self._run_since
                    self._run_since = None
                    self.state = "paused"
            elif action == "stop_after":
                self.stop_after_current = not self.stop_after_current
            elif action == "abort":
                if self.current:
                    self.stop_after_current = True  # abort halts the queue
                    self._abort_current_locked("Aborted by user")
            self.cond.notify_all()

    def _resume_locked(self):
        if self.proc:
            self.proc.resume()
        self._run_since = time.time()
        self.state = "running"

    def _abort_current_locked(self, reason: str):
        job, proc = self.current, self.proc
        if not job or not proc:
            return
        job.error = reason
        if self.state == "paused":
            self._resume_locked()
        # abort() can block for a few seconds; don't hold the lock meanwhile
        threading.Thread(target=proc.abort, daemon=True).start()

    def shutdown(self):
        """Stop the current encode (it is requeued on next start) and save."""
        self._shutdown = True
        with self.lock:
            proc = self.proc
            self.cond.notify_all()
        if proc:
            proc.abort(grace=5)
            self._runner_thread.join(timeout=10)
        try:
            self._save()
        except OSError:
            pass

    # -- runner -------------------------------------------------------------------

    def _next_job(self) -> Job | None:
        return next((j for j in self.jobs if j.status == "queued"), None)

    def _runner(self):
        while not self._shutdown:
            with self.cond:
                while self.state == "idle" or self._next_job() is None:
                    if self.state != "idle" and self._next_job() is None:
                        self.state, self.session_finished = "idle", time.time()
                        self.stop_after_current = False
                    self.cond.wait(timeout=5)
                    if self._shutdown:
                        return
                job = self._next_job()
                job.status, job.started_at, job.finished_at = "running", time.time(), None
                job.progress, job.elapsed, job.error, job.exit_code = 0.0, 0.0, None, None
                job.out_size, job.eta = 0, None
                self.current = job
                self.mark_dirty()
            self._run_job(job)
            with self.cond:
                self.current, self.proc, self._run_since = None, None, None
                if self.stop_after_current:
                    self.state, self.session_finished = "idle", time.time()
                    self.stop_after_current = False
                self.mark_dirty()

    def _run_job(self, job: Job):
        dst, part = Path(job.dst), part_path(job)
        log = collections.deque(maxlen=LOG_TAIL)
        self.logs[job.id] = log
        logfile = None
        try:
            if not os.path.isfile(job.src):
                raise RuntimeError("Source file not found")
            if dst.exists() and not job.overwrite:
                raise RuntimeError("Output file already exists")
            dst.parent.mkdir(parents=True, exist_ok=True)
            argv = list(self.hb_cmd) + ["--json", "--preset-import-gui"]
            if job.preset:
                argv += ["-Z", job.preset]
            argv += ["-i", job.src, "-o", str(part)] + list(job.extra_args)
            logfile = open(self.log_dir / f"{job.id}.log", "w", encoding="utf-8", errors="replace")
            logfile.write("$ " + " ".join(shlex.quote(a) for a in argv) + "\n")

            hints: list[str] = []

            def on_log(line: str):
                log.append(line)
                if _ERROR_HINT.search(line) and not _ERROR_NOISE.search(line):
                    hints.append(line.strip())
                logfile.write(line + "\n")
                if job.duration is None:
                    d = handbrake.parse_log_duration(line)
                    if d:
                        job.duration = d

            def on_progress(name: str, obj: dict):
                if name != "Progress":
                    return
                w = obj.get("Working")
                if obj.get("State") == "WORKING" and w:
                    with self.lock:
                        passes = max(1, w.get("PassCount", 1))
                        p = (w.get("Pass", 1) - 1 + w.get("Progress", 0.0)) / passes
                        job.progress = min(max(p, job.progress), 1.0)
                        job.pass_no, job.pass_count = w.get("Pass", 1), w.get("PassCount", 1)
                        # Rate is garbage (~1e-7) for the first seconds of an encode.
                        job.fps = w["Rate"] if w.get("Rate", 0) > 0.05 else None
                        job.fps_avg = w["RateAvg"] if w.get("RateAvg", 0) > 0.05 else None
                        # HandBrake's ETA covers the current pass only.
                        eta = w.get("ETASeconds", 0)
                        job.eta = float(eta) if eta and eta > 0 and passes == 1 else None

            with self.lock:
                self.proc = handbrake.EncodeProcess(argv, on_progress, on_log)
                self._run_since = time.time()
                if self.state == "paused":  # paused between jobs: start suspended
                    self.proc.pause()
                    self._run_since = None
            proc = self.proc
            while True:
                try:
                    proc.proc.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    self._tick(job, part)
            rc = proc.wait()
            self._tick(job, part)
            job.exit_code = rc
            if proc.aborted:
                raise RuntimeError(job.error or "Aborted")
            if rc != 0:
                hint = hints[-1] if hints else None
                raise RuntimeError(f"{hint} (exit code {rc})" if hint else f"HandBrakeCLI exited with code {rc}")
            if not part.exists() or part.stat().st_size == 0:
                raise RuntimeError("No output produced")
            os.replace(part, dst)
            with self.lock:
                job.status, job.progress, job.eta = "done", 1.0, 0.0
                job.out_size = dst.stat().st_size
        except Exception as e:  # noqa: BLE001 - every failure ends up in the job
            with self.lock:
                if self._shutdown:  # server stopping: run it again next time
                    job.status, job.progress, job.error, job.eta = "queued", 0.0, None, None
                else:
                    job.status, job.eta = "failed", None
                    job.error = job.error or str(e)
            try:
                if part.exists():
                    part.unlink()
            except OSError:
                pass
        finally:
            with self.lock:
                if self._run_since is not None:
                    job.elapsed += time.time() - self._run_since
                    self._run_since = None
                job.finished_at = time.time()
            if logfile:
                logfile.write(f"\n# hbmon: {job.status}" + (f" ({job.error})" if job.error else "") + "\n")
                logfile.close()

    def _tick(self, job: Job, part: Path):
        try:
            size = part.stat().st_size
        except OSError:
            size = 0
        with self.lock:
            job.out_size = size

    # -- reporting ----------------------------------------------------------------

    def log_tail(self, job_id: str, lines: int = 80) -> list[str]:
        log = self.logs.get(job_id)
        if log is not None:
            return list(log)[-lines:]
        try:
            with open(self.log_dir / f"{job_id}.log", encoding="utf-8", errors="replace") as f:
                return list(collections.deque(f, maxlen=lines))
        except OSError:
            return []

    def snapshot(self) -> dict:
        now = time.time()
        with self.lock:
            jobs = []
            for j in self.jobs:
                d = j.public()
                if j is self.current and self._run_since is not None:
                    d["elapsed"] = j.elapsed + (now - self._run_since)
                # Our own estimate beats HandBrake's once going: its average is skewed by pauses.
                if j.status == "running" and j.progress > 0.01:
                    d["eta"] = d["elapsed"] / j.progress * (1 - j.progress)
                jobs.append(d)
            return {
                "now": now,
                "state": self.state,
                "stop_after_current": self.stop_after_current,
                "session_started": self.session_started,
                "session_finished": self.session_finished,
                "current": self.current.id if self.current else None,
                "jobs": jobs,
                "summary": self._summary(jobs),
            }

    @staticmethod
    def _summary(jobs: list[dict]) -> dict:
        counts = collections.Counter(j["status"] for j in jobs)
        live = [j for j in jobs if j["status"] != "failed"]
        size_total = sum(j["size"] for j in live)
        size_done = sum(j["size"] * j["progress"] for j in live)
        dur_known = [j for j in live if j["duration"]]
        dur_total = sum(j["duration"] for j in dur_known)
        dur_done = sum(j["duration"] * j["progress"] for j in dur_known)
        # Weight work by media length when every job has one, by file size otherwise.
        by_dur = live and len(dur_known) == len(live)
        weight = (lambda j: j["duration"]) if by_dur else (lambda j: j["size"])
        w_total = sum(weight(j) for j in live)
        w_done = sum(weight(j) * j["progress"] for j in live)
        # Throughput (weight per active second) from everything encoded so far.
        worked = [j for j in live if j["elapsed"] > 0 and j["progress"] > 0]
        w_worked = sum(weight(j) * j["progress"] for j in worked)
        t_worked = sum(j["elapsed"] for j in worked)
        eta = None
        if w_total and t_worked > 5 and w_worked > 0:
            rate = w_worked / t_worked
            running = next((j for j in live if j["status"] == "running"), None)
            queued_w = sum(weight(j) for j in live if j["status"] == "queued")
            if running and running["eta"] is not None:
                eta = running["eta"] + queued_w / rate
            else:
                eta = (w_total - w_done) / rate
        done = [j for j in jobs if j["status"] == "done"]
        return {
            "counts": {s: counts.get(s, 0) for s in ("queued", "running", "done", "failed")},
            "total": len(jobs),
            "size_total": size_total,
            "size_done": size_done,
            "dur_total": dur_total,
            "dur_done": dur_done,
            "dur_unknown": len(live) - len(dur_known),
            "progress": (w_done / w_total) if w_total else 0.0,
            "eta": eta,
            "encode_time": t_worked,
            "out_size_done": sum(j["out_size"] for j in done),
            "src_size_done": sum(j["size"] for j in done),
        }
