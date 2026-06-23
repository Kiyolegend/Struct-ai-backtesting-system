"""
STRUCT.ai Backtest Dashboard  —  local web server (Flask)
=========================================================
Launch via:  start_dashboard.bat
             http://localhost:5050
"""
import os
import re
import sys
import json
import uuid
import queue
import threading
import subprocess
import webbrowser
from pathlib import Path

from flask import Flask, render_template, request, jsonify, Response

ROOT = Path(__file__).parent.parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not VENV_PY.exists():
    VENV_PY = Path(sys.executable)

app = Flask(__name__)

# ── In-memory job registry ────────────────────────────────────────────────────
_jobs: dict = {}   # job_id → {queue, done, error, result_path, result_html}

STRATEGY_CODES = ["SC1", "SC2", "SC3", "SC4", "SC5", "SC6",
                  "SW1", "SW2", "SW3", "SW4",
                  "CUSTOM1"]   # add new custom codes here too
SYMBOLS        = ["USD/JPY", "EUR/USD", "GBP/USD", "AUD/USD", "USD/CHF"]

# ── Chart-line regex ──────────────────────────────────────────────────────────
# Matches lines like:
#   [FULL]  30.0%  2025-10-09  cumPnL=$+72.34  trades=26
#   [IS]    50.0%  2025-08-01  cumPnL=$-21.46  trades=14
_CHART_RE = re.compile(
    r'\[(\w+)\]\s+([\d.]+)%\s+(\d{4}-\d{2}-\d{2})'
    r'.*?cumPnL=\$([+\-]?[\d.]+).*?trades=(\d+)'
)


# ── Subprocess runner (streams stdout into job queue) ─────────────────────────

def _stream_proc(cmd: list, job_id: str, on_done=None) -> None:
    job = _jobs[job_id]
    try:
        proc = subprocess.Popen(
            [str(c) for c in cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            # Fix: force UTF-8 in the subprocess so Unicode chars (→, ─, etc.)
            # don't crash on Windows where the default pipe encoding is charmap.
            # FIX B1: PYTHONUNBUFFERED=1 forces line-buffered output when stdout is piped.
            # Without this, Python block-buffers all print() calls (8 KB blocks) and
            # progress lines don't appear in the dashboard for 7-10 minutes.
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
            cwd=str(ROOT),
        )
        job["proc"] = proc
        for raw in iter(proc.stdout.readline, ""):
            line = raw.rstrip("\r\n")
            job["q"].put(("line", line))

            # Parse equity-curve data point from progress lines
            m = _CHART_RE.search(line)
            if m:
                chart_point = {
                    "segment": m.group(1),          # FULL | IS | OOS
                    "pct":     float(m.group(2)),
                    "date":    m.group(3),
                    "pnl":     float(m.group(4)),
                    "trades":  int(m.group(5)),
                }
                job["q"].put(("chart", chart_point))

        proc.wait()
        if proc.returncode != 0:
            job["q"].put(("error", f"Process exited with code {proc.returncode}"))
            job["error"] = True
        else:
            job["done"] = True
            if on_done:
                on_done(job_id)
    except Exception as exc:
        job["q"].put(("error", str(exc)))
        job["error"] = True
    finally:
        job["q"].put(("eof", ""))


def _make_job() -> tuple[str, dict]:
    jid = str(uuid.uuid4())[:8]
    job: dict = {"q": queue.Queue(), "done": False, "error": False,
                 "result_path": None, "result_html": None}
    _jobs[jid] = job
    return jid, job


def _find_latest_result() -> tuple[str | None, str | None]:
    results_dir = ROOT / "results"
    if not results_dir.exists():
        return None, None
    files = sorted(results_dir.glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None, None
    j = str(files[0])
    h = str(files[0].with_suffix(".html"))
    return j, (h if Path(h).exists() else None)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html",
                           strategies=STRATEGY_CODES,
                           symbols=SYMBOLS)


@app.route("/api/status")
def api_status():
    db_path = ROOT / "data" / "market_data.db"
    results_dir = ROOT / "results"
    recent = []
    if results_dir.exists():
        for f in sorted(results_dir.glob("*.html"),
                        key=lambda p: p.stat().st_mtime, reverse=True)[:8]:
            recent.append({
                "name": f.stem,
                "html": str(f),
                "mtime": f.stat().st_mtime,
            })
    return jsonify({
        "has_data":   db_path.exists(),
        "db_size_mb": round(db_path.stat().st_size / 1024 / 1024, 1)
                      if db_path.exists() else 0,
        "recent":     recent,
    })


@app.route("/api/run", methods=["POST"])
def api_run():
    d = request.get_json() or {}
    # Fix: treat empty list [] the same as omitted — default to all strategies.
    # An empty list is falsy in Python so `or` handles both None and [] correctly.
    strategies = d.get("strategies") or STRATEGY_CODES
    # Validate: reject any code not in the known set
    invalid = [s for s in strategies if s not in STRATEGY_CODES]
    if invalid:
        return jsonify({"error": f"Unknown strategy codes: {invalid}"}), 400
    symbol     = d.get("symbol", "USD/JPY")
    portfolio  = bool(d.get("portfolio", False))
    lot        = d.get("lot")
    wf         = bool(d.get("walk_forward", True))
    mc         = bool(d.get("monte_carlo", True))
    mc_iter    = int(d.get("mc_iter", 1000))
    wf_split   = float(d.get("wf_split", 0.70))

    cmd = [VENV_PY, ROOT / "run_backtest.py"]
    if portfolio:
        cmd.append("--portfolio")
    else:
        cmd += ["--symbol", symbol]
    if strategies:
        cmd += ["--strategies"] + strategies
    if lot:
        cmd += ["--lot", str(lot)]
    if not wf:
        cmd.append("--no-wf")
    if not mc:
        cmd.append("--no-mc")
    cmd += ["--mc-iter", str(mc_iter), "--wf-split", str(wf_split)]
    cmd.append("--no-viewer")   # dashboard opens viewer itself

    jid, _ = _make_job()

    def on_done(job_id):
        j, h = _find_latest_result()
        _jobs[job_id]["result_path"] = j
        _jobs[job_id]["result_html"] = h

    threading.Thread(target=_stream_proc, args=(cmd, jid, on_done),
                     daemon=True).start()
    return jsonify({"job_id": jid})


@app.route("/api/collect", methods=["POST"])
def api_collect():
    d      = request.get_json() or {}
    source  = d.get("source", "mt5")
    refresh = bool(d.get("refresh", False))

    cmd = [VENV_PY, ROOT / "collector" / "collect.py", "--source", source]
    if refresh:
        cmd.append("--refresh")

    jid, _ = _make_job()
    threading.Thread(target=_stream_proc, args=(cmd, jid),
                     daemon=True).start()
    return jsonify({"job_id": jid})


@app.route("/api/stream/<job_id>")
def api_stream(job_id: str):
    if job_id not in _jobs:
        return jsonify({"error": "unknown job"}), 404

    def generate():
        job = _jobs[job_id]
        while True:
            try:
                kind, content = job["q"].get(timeout=25)
            except queue.Empty:
                yield "data: {\"type\":\"ping\"}\n\n"
                continue

            yield f"data: {json.dumps({'type': kind, 'content': content})}\n\n"

            if kind == "eof":
                fin = {
                    "type":        "finished",
                    "result_html": job.get("result_html"),
                    "result_path": job.get("result_path"),
                    "error":       job.get("error", False),
                }
                yield f"data: {json.dumps(fin)}\n\n"
                break

    return Response(generate(),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.route("/api/open_viewer", methods=["POST"])
def api_open_viewer():
    path = (request.get_json() or {}).get("path", "")
    if path and Path(path).exists():
        try:
            webbrowser.open("file:///" + path.replace(os.sep, "/"))
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})
    return jsonify({"ok": False, "error": "file not found"})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    PORT = 5050
    print(f"\n{'='*58}")
    print(f"  STRUCT.ai Backtest Dashboard")
    print(f"  http://localhost:{PORT}")
    print(f"  Press Ctrl+C to stop")
    print(f"{'='*58}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
