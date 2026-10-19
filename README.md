# hbmon

A small web UI for queueing, controlling and monitoring HandBrakeCLI encodes. It uses only
the Python standard library (3.9+) and runs on Linux, Windows and macOS.

## Running it

```sh
python -m hbmon                 # from this directory, or after `pip install .`: hbmon
```

Then open <http://127.0.0.1:8765/>.

HandBrakeCLI is found automatically: `HandBrakeCLI` on PATH first, then the
`fr.handbrake.ghb` Flatpak (`flatpak run --command=HandBrakeCLI fr.handbrake.ghb`).
You can also set it explicitly:

```sh
hbmon --handbrake "flatpak run --command=HandBrakeCLI fr.handbrake.ghb"
```

Every encode runs with `--preset-import-gui`, so your HandBrake GUI presets are available.
The **Custom Presets** group is listed first.

Options: `--host`, `--port`, `--state-dir`, `--dest` / `--preset` (defaults for the UI),
`--probe auto|ffprobe|handbrake|none` (how source durations are read), and `--allow-host`.

### Reaching it from another machine

By default the server only listens on localhost. Over Tailscale (or any SSH connection),
forward the port:

```sh
ssh -L 8765:localhost:8765 office-pc      # then browse http://localhost:8765/ at home
```

To keep it running after you log out, start it inside `tmux`/`screen`, or as a systemd user
service:

```ini
# ~/.config/systemd/user/hbmon.service
[Service]
ExecStart=/usr/bin/python3 -m hbmon
WorkingDirectory=%h/hbmon
Restart=on-failure
[Install]
WantedBy=default.target
```

(`systemctl --user enable --now hbmon`, plus `loginctl enable-linger $USER` so it keeps
running while you're logged out.)

You can also bind it to the Tailscale address, e.g. `--host 100.x.y.z`. There is **no
authentication**, so only do this on a tailnet you trust. Requests are accepted only when the
Host header is an IP address, `localhost`, or a name given with `--allow-host` (needed for a
MagicDNS name). That check, plus a required custom header on POSTs, stops other websites
from driving the server through your browser.

## Using it

| Quadrant | What it does |
|---|---|
| **Files** (top left) | Lazy directory tree. Click, Ctrl+click and Shift+click to select; ▶ or double-click expands. Keys: arrows, Space toggles, Enter adds, Ctrl+A, Esc. Type a path in the box and press Enter to jump to it (hidden folders included). By default only video files are listed; tick *all* / *hidden* to show more. |
| **Target** (top right) | Output directory (**← tree** copies the focused folder), preset, container (`mkv`/`mp4`/`m4v`/`webm`, which also sets the format), what to do if the output already exists (skip, rename or overwrite), extra HandBrakeCLI args, and whether selected folders keep their subfolder structure under the target. Folders add their videos recursively. Settings are remembered in the browser. |
| **Queue** (bottom left) | `NN% filename`, coloured blue for queued, yellow for running, red for failed and green for done. Click, Ctrl and Shift select. Drag to reorder (the running job can't be moved), or use Alt+↑/↓. **Del** removes the selection and asks for confirmation unless every selected job is done. |
| **Status** (bottom right) | Start/Resume, Pause, Stop after current, Abort (aborts the current job and stops the queue), Retry, Remove and Clear done. The overview shows when the queue was started, a count per status, processed and total size and length, overall progress, encode time, ETA and finish time, and output vs. source size. Selecting one job shows its details (paths, preset, sizes, est. final size, elapsed, ETA, fps and ×realtime speed, timestamps, exit code) and a live tail of its HandBrake log. Selecting several shows totals. |

### Behaviour notes

- **Pause is a real pause.** The HandBrakeCLI process tree is suspended (SIGSTOP/SIGCONT on
  Linux, including through the Flatpak sandbox, and NtSuspendProcess on Windows), so CPU drops
  to zero and the encode resumes where it stopped. If you pause between jobs, the next job
  starts suspended.
- Encodes are written to `name.hbmon-part.ext` and renamed when they succeed, so a failed or
  aborted encode never leaves a file that looks finished. Retrying a finished job re-encodes it
  and replaces its output only on success.
- When an encode fails, the job shows the most relevant HandBrake error line, e.g.
  `Invalid preset … (exit code 2)`. Full logs are kept in `<state-dir>/logs/`.
- The queue is saved to `<state-dir>/state.json`: `~/.config/hbmon` on Linux,
  `%APPDATA%\hbmon` on Windows. If the server stops mid-encode, that job goes back in the
  queue. HandBrakeCLI is tied to the server process, so killing the server also kills the
  encode.
- Source durations come from `ffprobe` if it is installed, otherwise from a quick
  `HandBrakeCLI --scan`. Overall progress and ETA are weighted by duration when every job has
  one, and by file size otherwise.
