"""hbmon: a small web UI to queue and monitor HandBrakeCLI encodes."""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from . import handbrake
from .jobs import QueueManager
from .server import make_server


def default_state_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "hbmon"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "hbmon"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hbmon", description=__doc__)
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to bind (default: 127.0.0.1, i.e. localhost only)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--handbrake", metavar="CMD",
                    help="HandBrakeCLI command line, e.g. "
                         f"'{' '.join(handbrake.FLATPAK_CMD)}' (default: autodetect)")
    ap.add_argument("--state-dir", type=Path, default=default_state_dir(),
                    help="where the queue and encode logs are kept (default: %(default)s)")
    ap.add_argument("--probe", choices=["auto", "ffprobe", "handbrake", "none"], default="auto",
                    help="how to read source durations (default: ffprobe if found, else HandBrake scan)")
    ap.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="extra Host header names to accept, e.g. a Tailscale MagicDNS name")
    ap.add_argument("--dest", default="", help="default target directory shown in the UI")
    ap.add_argument("--preset", default="", help="default preset shown in the UI")
    args = ap.parse_args(argv)

    if args.handbrake:
        hb_cmd = handbrake.split_args(args.handbrake)
    else:
        hb_cmd = handbrake.detect_command()
        if not hb_cmd:
            ap.error("HandBrakeCLI not found on PATH and no fr.handbrake.ghb Flatpak; use --handbrake")

    qm = QueueManager(hb_cmd, args.state_dir, probe=args.probe)
    httpd = make_server(args.host, args.port, qm, set(args.allow_host),
                        {"dest": args.dest, "preset": args.preset})
    shown = f"[{args.host}]" if ":" in args.host else args.host
    print(f"hbmon: serving on http://{shown}:{args.port}/  (HandBrake: {' '.join(hb_cmd)})", flush=True)
    print(f"hbmon: state in {args.state_dir}", flush=True)
    def graceful(signum, frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), graceful)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nhbmon: shutting down (an encode in progress is requeued)")
    finally:
        httpd.server_close()
        qm.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
