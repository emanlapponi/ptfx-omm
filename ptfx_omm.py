#!/usr/bin/env python3
"""PTFX-OMM: turn one plugin.c into an ad-hoc browser app.

Usage:
    python3 ptfx_omm.py PLUGIN.c [--wav INPUT.WAV] [--length SECONDS]

Deps (ptfx_builder.py, tracker_plugin.h, effects/) are resolved from the
tracker-ce checkout at ../tracker-ce/editor-fx-sdk (override: PTFX_SDK).

The plugin is compiled natively with host.c (freestanding C, PtfxHostApi
supplied by the host). A localhost server runs it on demand; the generated
page shows a stereo waveform (raw/processed toggle, scrub, play), one
slider or select per inferred parameter, and a Run button.
"""
import argparse
import array
import base64
import io
import itertools
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import threading
import wave
import webbrowser
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))  # ptfx-omm (ptfx_omm.py, host.c)
SDK_DIR = os.environ.get("PTFX_SDK") or os.path.normpath(
    os.path.join(HERE, "..", "tracker-ce", "editor-fx-sdk"))  # ptfx_builder.py, tracker_plugin.h, effects/
sys.path.insert(0, SDK_DIR)
from ptfx_builder import FLAG_MAP, build_ptfx, parse_comments, parse_params_from_source  # noqa: E402

FLAG_GEN = 0x0001
FLAG_STEREO = 0x0002
FLAG_MONO = 0x0008
MK66_LIMIT = 32768
IMX_LIMIT = 131072
SCRATCH_LIMIT = 655360
CACHE_DIR = os.path.join(tempfile.gettempdir(), "ptfx-omm")

# Shared by the request handler; set by create_app().
APP = {}


def load_plugin(path):
    src = open(path).read()
    meta = parse_comments(src)
    flags = 0
    for part in meta.get("FLAGS", "EFFECT").replace("|", ",").split(","):
        flags |= FLAG_MAP.get(part.strip().upper(), 0)
    try:
        scratch = int(meta.get("SCRATCH", 0))
    except ValueError:
        scratch = 0
    return {
        "path": path,
        "name": meta.get("NAME") or os.path.splitext(os.path.basename(path))[0],
        "flags": flags,
        "flags_str": meta.get("FLAGS", "EFFECT"),
        "scratch": scratch,
        "is_gen": bool(flags & FLAG_GEN),
        "params": parse_params_from_source(src),
    }


def load_wav(path):
    w = wave.open(path, "rb")
    if w.getsampwidth() != 2:
        raise ValueError("need 16-bit PCM WAV")
    rate = w.getframerate()
    nch = w.getnchannels()
    a = array.array("h")
    a.frombytes(w.readframes(w.getnframes()))
    w.close()
    if nch == 2:
        return rate, [a[0::2], a[1::2]]
    return rate, [a]


def downmix(chans):
    a, b = chans
    n = min(len(a), len(b))
    return [array.array("h", ((a[i] + b[i]) // 2 for i in range(n)))]


def upmix(chans):
    a = chans[0]
    return [a, array.array("h", a)]


def interleave(chans):
    if len(chans) == 1:
        return chans[0].tobytes()
    n = min(len(chans[0]), len(chans[1]))
    a, b = chans[0][:n], chans[1][:n]
    return array.array("h", itertools.chain.from_iterable(zip(a, b))).tobytes()


def compile_plugin(plugin):
    os.makedirs(CACHE_DIR, exist_ok=True)
    binary = os.path.join(CACHE_DIR, os.path.splitext(os.path.basename(plugin["path"]))[0] + ".bin")
    src = plugin["path"]
    if os.path.exists(binary) and os.path.getmtime(binary) >= os.path.getmtime(src):
        return binary
    r = subprocess.run(
        ["cc", "-O2", "-I", SDK_DIR, src, os.path.join(HERE, "host.c"), "-lm", "-o", binary],
        capture_output=True, timeout=60,
    )
    if r.returncode != 0:
        err = r.stderr.decode().strip()
        raise RuntimeError(err.splitlines()[-1] if err else "cc failed")
    return binary


_TOOLCHAIN_PROBE = "unprobed"


def _arm_toolchain_ok():
    global _TOOLCHAIN_PROBE
    if _TOOLCHAIN_PROBE == "unprobed":
        if not shutil.which("arm-none-eabi-gcc"):
            _TOOLCHAIN_PROBE = "no arm-none-eabi-gcc"
        else:
            with tempfile.TemporaryDirectory() as d:
                c = os.path.join(d, "t.c")
                open(c, "w").write("#include <stdint.h>\nint main(void){return (int)0;}\n")
                r = subprocess.run(
                    ["arm-none-eabi-gcc", "-mcpu=cortex-m4", "-mthumb", "-mfloat-abi=hard",
                     "-mfpu=fpv4-sp-d16", "-c", c, "-o", os.path.join(d, "t.o")],
                    capture_output=True)
                _TOOLCHAIN_PROBE = "ok" if r.returncode == 0 else "arm toolchain missing newlib headers"
    return None if _TOOLCHAIN_PROBE == "ok" else _TOOLCHAIN_PROBE


def size_check(plugin):
    probe = _arm_toolchain_ok()
    if probe:
        return f"size check skipped ({probe})"
    try:
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
            out = os.path.join(d, "check.ptfx")
            build_ptfx(plugin["path"], out)
            data = open(out, "rb").read()
            scratch = struct.unpack_from("<I", data, 0x3C)[0]
            code = struct.unpack_from("<I", data, 0x44)[0]
            bss = struct.unpack_from("<I", data, 0x48)[0]
    except BaseException as e:
        return f"size check failed: {type(e).__name__}"
    kb = lambda v: f"{v // 1024}.{(v % 1024) // 100}K"
    total = code + bss
    if total > IMX_LIMIT:
        msg = f"code {kb(code)} + bss {kb(bss)} — EXCEEDS IMX 128KB"
    elif total > MK66_LIMIT:
        msg = f"code {kb(code)} + bss {kb(bss)} — IMX-only (add PTFX_FLAG_IMX_ONLY)"
    else:
        msg = f"code {kb(code)} + bss {kb(bss)} — OK (MK66+IMX)"
    if scratch > SCRATCH_LIMIT:
        msg += " | scratch > 640KB"
    return msg


def run_host(plugin, binary, values, rate, in_chans, out_len, mono):
    in_len = len(in_chans[0])
    args = [binary, str(rate), str(len(in_chans)), str(in_len), str(out_len),
            str(plugin["scratch"]), str(mono)]
    args += [f"{v:.6f}" for v in values]
    stdin_data = interleave(in_chans) if in_len else b""
    r = subprocess.run(args, input=stdin_data, capture_output=True, timeout=60)
    if r.returncode != 0:
        err = r.stderr.decode().strip()
        raise RuntimeError(err.splitlines()[-1] if err else f"host exited {r.returncode}")
    m = re.search(rb"PTFX_OUT_BYTES (\d+)", r.stderr)
    return (int(m.group(1)) if m else 0), r.stdout


def store_input(rate, channels, raw):
    a = array.array("h")
    a.frombytes(raw[:len(raw) - len(raw) % 2])
    if channels > 2:
        raise RuntimeError("only mono/stereo input supported")
    # Drop any partial frame so every channel has the same length.
    if channels == 2 and len(a) % 2:
        a = a[:-1]
    chans = [a[0::2], a[1::2]] if channels == 2 else [a]
    if not (APP["plugin"]["flags"] & FLAG_STEREO) and len(chans) == 2:
        chans = downmix(chans)
    data = {
        "rate": rate,
        "chans": chans,
        "channels": len(chans),
        "samples": len(chans[0]),
        "b64": base64.b64encode(interleave(chans)).decode(),
    }
    APP["input"] = data
    APP["meta"]["input"] = {k: v for k, v in data.items() if k != "chans"}
    APP["meta"]["inputDesc"] = f"{data['samples'] / data['rate']:.2f}s {rate}Hz {data['channels']}ch (uploaded)"
    return APP["meta"]["input"]


def do_run(plugin, binary, values, gen_len):
    duplicated_mono = False
    if plugin["is_gen"]:
        rate = 44100
        n = 2 if plugin["flags"] & FLAG_STEREO else 1
        in_chans = [array.array("h") for _ in range(n)]
        out_len = int(gen_len * rate)
        max_len = out_len
    else:
        inp = APP["input"]
        if not inp:
            raise RuntimeError("no input yet — upload a WAV first")
        rate, in_chans, out_len = inp["rate"], inp["chans"], 0
        max_len = len(in_chans[0])
        # Stereo plugins run on both channels; a mono upload is duplicated for
        # the run only — the stored/raw view keeps the original (mono) layout.
        if (plugin["flags"] & FLAG_STEREO) and len(in_chans) == 1:
            in_chans = upmix(in_chans)
            duplicated_mono = True
    mono = 1 if plugin["flags"] & FLAG_MONO else 0
    produced, out = run_host(plugin, binary, values, rate, in_chans, out_len, mono)
    nch_out = 1 if mono else len(in_chans)
    if duplicated_mono and nch_out == 2:
        pcm = array.array("h")
        pcm.frombytes(out)
        left, right = pcm[0::2], pcm[1::2]
        if left == right:
            out = left.tobytes()
            nch_out = 1
    return produced, max_len, rate, nch_out, out


def create_app(plugin_path, wav_arg=None, length=2.0):
    plugin = load_plugin(os.path.abspath(plugin_path))
    binary = compile_plugin(plugin)
    size_msg = size_check(plugin)

    input_data = None
    if plugin["is_gen"]:
        input_desc = f"generator {max(0.01, length):g}s"
    elif wav_arg:
        path = os.path.expanduser(wav_arg)
        if not os.path.exists(path):
            raise SystemExit(f"no such file: {path}")
        rate, chans = load_wav(path)
        if not (plugin["flags"] & FLAG_STEREO) and len(chans) == 2:
            chans = downmix(chans)
        input_data = {
            "rate": rate,
            "chans": chans,
            "channels": len(chans),
            "samples": len(chans[0]),
            "b64": base64.b64encode(interleave(chans)).decode(),
        }
        input_desc = f"{os.path.basename(path)} {rate}Hz {len(chans[0]) / rate:.2f}s"
    else:
        input_desc = "no input yet — upload a WAV (or relaunch with --wav <file>)"

    meta = {
        "name": plugin["name"],
        "flagsStr": plugin["flags_str"],
        "scratch": plugin["scratch"],
        "isGen": plugin["is_gen"],
        "monoOut": bool(plugin["flags"] & FLAG_MONO),
        "params": plugin["params"],
        "sizeMsg": size_msg,
        "input": {k: v for k, v in input_data.items() if k != "chans"} if input_data else None,
        "inputDesc": input_desc,
        "genLen": max(0.01, length),
    }
    APP.clear()
    APP.update(plugin=plugin, binary=binary, meta=meta, input=input_data)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                data = APP_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif self.path == "/meta":
                self._json(APP["meta"])
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            ln = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(ln) if ln else b""
            if self.path == "/run":
                try:
                    body = json.loads(raw or b"{}")
                    values = [float(v) for v in body.get("values", [])]
                except Exception:
                    self._json({"error": "bad request"})
                    return
                try:
                    produced, max_len, rate, nch, out = do_run(
                        APP["plugin"], APP["binary"], values, APP["meta"]["genLen"])
                    self._json({
                        "produced": produced,
                        "maxLen": max_len,
                        "rate": rate,
                        "channels": nch,
                        "b64": base64.b64encode(out).decode(),
                    })
                except Exception as e:
                    self._json({"error": str(e)}, 500)
            elif self.path == "/input":
                try:
                    body = json.loads(raw or b"{}")
                    rate = int(body["rate"])
                    channels = int(body["channels"])
                    pcm = base64.b64decode(body["pcm"])
                    if not (8000 <= rate <= 192000) or channels not in (1, 2):
                        raise ValueError("unsupported rate or channels")
                    meta_in = store_input(rate, channels, pcm)
                    self._json(meta_in)
                except Exception as e:
                    self._json({"error": f"bad input: {e}"}, 400)
            else:
                self._json({"error": "not found"}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return {"server": server, "port": server.server_address[1], "meta": meta}


APP_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PTFX-OMM</title>
<style>
  :root { color-scheme: dark; --bg: #111719; --surface: #192124; --line: #334044; --text: #e6eeec; --muted: #a2b3b0; --accent: #b6dfcf; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.5 system-ui, -apple-system, sans-serif; }
  .shell { max-width: 1440px; margin: 0 auto; padding: 32px; }
  header { display: flex; flex-wrap: wrap; align-items: end; justify-content: space-between; gap: 20px; padding-bottom: 24px; border-bottom: 1px solid var(--line); }
  .eyebrow, .section-label { font: 600 11px/1.4 ui-monospace, Menlo, monospace; letter-spacing: .12em; text-transform: uppercase; color: var(--muted); }
  .eyebrow { color: var(--accent); margin-bottom: 8px; }
  h1 { margin: 0; font-size: clamp(24px, 3vw, 36px); letter-spacing: -.035em; font-weight: 600; line-height: 1.15; }
  #badges { display: flex; flex-wrap: wrap; gap: 8px; }
  .badge { padding: 5px 8px; border: 1px solid var(--line); color: var(--muted); font: 11px/1.4 ui-monospace, Menlo, monospace; border-radius: 3px; }
  .controls { display: flex; flex-wrap: wrap; gap: 8px; padding: 20px 0; }
  button, select { font: inherit; color: var(--text); background: #222c2f; border: 1px solid #415054; border-radius: 4px; }
  button { min-height: 40px; padding: 8px 14px; cursor: pointer; font-weight: 550; }
  button:hover { background: #303e41; border-color: #6a807c; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #14221e; }
  button.primary:hover { background: #d3eee3; border-color: #d3eee3; }
  button:disabled { opacity: .5; cursor: default; }
  :focus-visible { outline: 2px solid #c5ebdc; outline-offset: 3px; }
  .workspace { display: grid; grid-template-columns: minmax(0, 1fr) minmax(240px, 300px); border: 1px solid var(--line); background: var(--surface); }
  .wave-panel, .param-panel { min-width: 0; padding: 20px; }
  .param-panel { border-left: 1px solid var(--line); }
  .panel-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 16px; }
  .panel-hint { color: var(--muted); font: 11px/1.4 ui-monospace, Menlo, monospace; }
  #wave { display: block; width: 100%; height: 240px; background: #101719; border: 1px solid #303d40; cursor: crosshair; touch-action: none; }
  #params { display: flex; flex-direction: column; gap: 20px; }
  .param { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px 12px; align-items: center; }
  .plabel { color: var(--text); font-size: 13px; }
  .pout { color: var(--accent); font: 12px/1.4 ui-monospace, Menlo, monospace; text-align: right; }
  .param input, .param select { grid-column: 1 / -1; grid-row: 2; width: 100%; }
  input[type=range] { margin: 2px 0; accent-color: var(--accent); cursor: pointer; }
  select { min-height: 36px; padding: 6px 8px; }
  #status { min-height: 20px; padding: 16px 0; color: var(--muted); font: 12px/1.5 ui-monospace, Menlo, monospace; overflow-wrap: anywhere; }
  @media (max-width: 720px) {
    .shell { padding: 20px 16px; }
    .workspace { grid-template-columns: minmax(0, 1fr); }
    .param-panel { border-left: 0; border-top: 1px solid var(--line); }
    .wave-panel, .param-panel { padding: 16px; }
  }
</style>
</head>
<body>
<main class="shell">
<header>
  <div><div class="eyebrow">PTFX-OMM / On My Machine</div><h1 id="title"></h1></div>
  <span id="badges"></span>
</header>
<div class="controls">
  <button id="playBtn">▶ Play</button>
  <button id="viewBtn"></button>
  <button id="uploadBtn">⬆ Upload WAV</button>
  <button id="runBtn" class="primary">Run</button>
  <button id="dlBtn">Download .wav</button>
  <input type="file" id="fileInp" accept="audio/wav,.wav" style="display:none">
</div>
<div class="workspace">
  <section class="wave-panel" aria-label="Waveform">
    <div class="panel-head"><span class="section-label">Waveform</span><span class="panel-hint">Click or drag to scrub</span></div>
    <canvas id="wave" height="240" aria-label="Audio waveform. Click or drag to scrub."></canvas>
  </section>
  <section class="param-panel" aria-label="Plugin parameters">
    <div class="panel-head"><span class="section-label">Parameters</span></div>
    <div id="params"></div>
  </section>
</div>
<div id="status" role="status" aria-live="polite"></div>
</main>
<script>
"use strict";
const $ = id => document.getElementById(id);
const canvas = $("wave"), c2d = canvas.getContext("2d");
let META = null;
let rawBuf = null;      // {chans: Float32Array[], rate}
let procBuf = null;
let view = "raw";
let audioCtx = null, currentSrc = null, playing = false;
let playStart = 0, playOffset = 0;
const paramEls = [];

function b64ToArray(b64) {
  const bin = atob(b64);
  const u = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
  return u;
}
function b64FromBytes(u8) {
  let s = "";
  for (let i = 0; i < u8.length; i += 0x8000)
    s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
  return btoa(s);
}
function int16ToFloat(bytes, nch) {
  const i16 = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength >> 1);
  const n = Math.floor(i16.length / nch);
  const chans = [];
  for (let c = 0; c < nch; c++) {
    const f = new Float32Array(n);
    for (let i = 0; i < n; i++) f[i] = i16[i * nch + c] / 32768;
    chans.push(f);
  }
  return { chans, rate: 0, n };
}
function fmtVal(p, v) {
  if (p.type === "d") return String(Math.round(v)) + (p.unit ? " " + p.unit : "");
  return v.toFixed(2) + (p.unit ? " " + p.unit : "");
}

function resizeCanvas() {
  canvas.width = canvas.clientWidth * (window.devicePixelRatio || 1);
  draw();
}
window.addEventListener("resize", resizeCanvas);

function activeBuf() { return view === "raw" ? rawBuf : procBuf; }
function activeChans() { const b = activeBuf(); return b ? b.chans : []; }
function activeRate() { const b = activeBuf(); return b ? b.rate : 44100; }
function activeN() { const c = activeChans(); return c.length ? c[0].length : 0; }

function pos() {
  if (!playing) return playOffset;
  return Math.min(activeN() / activeRate(), playOffset + (audioCtx.currentTime - playStart));
}

function draw() {
  const w = canvas.width, h = canvas.height;
  c2d.fillStyle = "#101719";
  c2d.fillRect(0, 0, w, h);
  const chans = activeChans();
  if (!chans.length) {
    c2d.fillStyle = "#a2b3b0";
    c2d.font = "13px monospace";
    c2d.fillText(META.isGen ? "generator — press Run" : (view === "raw" ? "no input" : "press Run"), 10, h / 2);
    return;
  }
  const per = h / chans.length;
  const n = chans[0].length;
  const cols = [[ "L", "#85c9b2" ], [ "R", "#d8b890" ]];
  for (let c = 0; c < chans.length; c++) {
    const top0 = c * per;
    const mid = top0 + per / 2;
    const half = per / 2 - 3;
    c2d.strokeStyle = "#334044";
    c2d.beginPath(); c2d.moveTo(0, mid + 0.5); c2d.lineTo(w, mid + 0.5); c2d.stroke();
    c2d.fillStyle = cols[c][1];
    const data = chans[c];
    const step = Math.max(1, Math.floor(n / w));
    for (let x = 0; x < w; x++) {
      let mn = 1, mx = -1;
      const end = Math.min(n, (x + 1) * step);
      for (let i = x * step; i < end; i++) {
        const v = data[i];
        if (v < mn) mn = v;
        if (v > mx) mx = v;
      }
      if (mn > mx) continue;
      const y1 = mid - mx * half;
      const y2 = mid - mn * half;
      c2d.fillRect(x, y1, 1, Math.max(1, y2 - y1));
    }
    c2d.fillStyle = cols[c][1];
    c2d.font = "12px monospace";
    c2d.fillText(cols[c][0], 5, top0 + 13);
  }
  // playhead
  const x = Math.floor(pos() / (activeN() / activeRate()) * w);
  c2d.strokeStyle = "#e6eeec";
  c2d.beginPath(); c2d.moveTo(x + 0.5, 0); c2d.lineTo(x + 0.5, h); c2d.stroke();
}

function animLoop() {
  if (!playing) return;
  draw();
  requestAnimationFrame(animLoop);
}

async function ensureCtx() {
  if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  if (audioCtx.state === "suspended") await audioCtx.resume();
}
function makeBuffer(chans, rate) {
  const n = chans[0].length;
  const buf = audioCtx.createBuffer(chans.length, n, rate);
  for (let c = 0; c < chans.length; c++) buf.copyToChannel(chans[c], c);
  return buf;
}
function stopSource() {
  if (currentSrc) { currentSrc.onended = null; try { currentSrc.stop(); } catch (e) {} currentSrc = null; }
}
function updatePlayBtn() { $("playBtn").textContent = playing ? "⏸ Pause" : "▶ Play"; }

async function togglePlay() {
  const b = activeBuf();
  if (!b) { setStatus("nothing to play — press Run"); return; }
  if (playing) {
    playOffset = pos();
    stopSource();
    playing = false;
    updatePlayBtn();
    draw();
    return;
  }
  await ensureCtx();
  stopSource();
  const buf = makeBuffer(b.chans, b.rate);
  const src = audioCtx.createBufferSource();
  src.buffer = buf;
  src.connect(audioCtx.destination);
  const at = Math.min(pos(), buf.duration - 0.002);
  src.start(0, at);
  currentSrc = src;
  playOffset = at;
  playStart = audioCtx.currentTime;
  playing = true;
  updatePlayBtn();
  src.onended = () => {
    if (currentSrc !== src) return;
    playing = false;
    playOffset = 0;
    stopSource();
    updatePlayBtn();
    draw();
  };
  animLoop();
}

// scrub: click or drag on the waveform
let dragging = false;
function seekTo(e) {
  const rect = canvas.getBoundingClientRect();
  const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
  playOffset = frac * (activeN() / activeRate());
  if (playing) { playing = false; stopSource(); updatePlayBtn(); }
  draw();
}
canvas.addEventListener("pointerdown", e => { dragging = true; canvas.setPointerCapture(e.pointerId); seekTo(e); });
canvas.addEventListener("pointermove", e => { if (dragging) seekTo(e); });
canvas.addEventListener("pointerup", () => { dragging = false; });

function toggleView() {
  view = view === "raw" ? "processed" : "raw";
  playing = false; stopSource(); updatePlayBtn();
  playOffset = 0;
  $("viewBtn").textContent = "View: " + (view === "raw" ? "Raw" : "Processed");
  draw();
}

function buildParams() {
  const div = $("params");
  META.params.forEach((p, index) => {
    const row = document.createElement("div"); row.className = "param";
    const label = document.createElement("label"); label.className = "plabel"; label.textContent = p.label;
    const out = document.createElement("span"); out.className = "pout";
    let el;
    if (p.type === "l") {
      el = document.createElement("select");
      p.options.forEach((o, j) => { const opt = document.createElement("option"); opt.value = j; opt.textContent = o; el.appendChild(opt); });
      el.value = String(Math.min(p.options.length - 1, Math.round(p.default)));
      out.textContent = p.options[+el.value];
      el.addEventListener("change", () => out.textContent = p.options[+el.value]);
    } else {
      el = document.createElement("input"); el.type = "range";
      el.min = String(p.min); el.max = String(p.max); el.step = String(p.step);
      el.value = String(p.default);
      out.textContent = fmtVal(p, p.default);
      el.addEventListener("input", () => out.textContent = fmtVal(p, parseFloat(el.value)));
    }
    el.id = "param-" + index;
    label.htmlFor = el.id;
    row.append(label, el, out);
    div.appendChild(row);
    paramEls.push(el);
  });
}

function setStatus(t) { $("status").textContent = t; }

async function run() {
  const values = paramEls.map(el => parseFloat(el.value));
  setStatus("processing…");
  $("runBtn").disabled = true;
  try {
    const r = await (await fetch("/run", { method: "POST", body: JSON.stringify({ values }) })).json();
    if (r.error) { setStatus("error: " + r.error); return; }
    const d = int16ToFloat(b64ToArray(r.b64), r.channels);
    d.rate = r.rate;
    procBuf = d;
    if (META.isGen) { // raw for a generator = silence of the processed length
      rawBuf = { chans: d.chans.map(() => new Float32Array(d.n)), rate: r.rate };
    }
    playing = false; stopSource(); updatePlayBtn();
    view = "processed";
    $("viewBtn").textContent = "View: Processed";
    playOffset = 0;
    draw();
    setStatus(r.produced + " samples" + (r.produced < r.maxLen ? " (ended early: " + r.produced + "/" + r.maxLen + ")" : ""));
  } catch (e) {
    setStatus("error: " + e);
  }
  $("runBtn").disabled = false;
}

function parseWav(u8) {
  const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
  const tag = (o) => String.fromCharCode(u8[o], u8[o + 1], u8[o + 2], u8[o + 3]);
  if (tag(0) !== "RIFF" || tag(8) !== "WAVE") throw new Error("not a RIFF/WAVE file");
  let off = 12, rate = 0, nch = 0, bps = 0, dataOff = -1, dataSz = 0;
  while (off + 8 <= u8.length) {
    const id = tag(off), sz = dv.getUint32(off + 4, true);
    if (id === "fmt ") { nch = dv.getUint16(off + 10, true); rate = dv.getUint32(off + 12, true); bps = dv.getUint16(off + 22, true); }
    else if (id === "data") { dataOff = off + 8; dataSz = sz; }
    off += 8 + sz + (sz & 1);
  }
  if (dataOff < 0 || !rate || !nch) throw new Error("no data chunk");
  if (bps !== 16) throw new Error("need 16-bit PCM WAV (got " + bps + "-bit)");
  // Respect the declared data chunk size; some writers append trailing junk.
  return { rate, nch, pcm: u8.subarray(dataOff, Math.min(u8.length, dataOff + dataSz)) };
}

async function onFile(e) {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  setStatus("loading " + file.name + "…");
  try {
    const u8 = new Uint8Array(await file.arrayBuffer());
    const w = parseWav(u8);
    const r = await (await fetch("/input", {
      method: "POST",
      body: JSON.stringify({ rate: w.rate, channels: w.nch, pcm: b64FromBytes(w.pcm) }),
    })).json();
    if (r.error) { setStatus("upload error: " + r.error); return; }
    const d = int16ToFloat(b64ToArray(r.b64), r.channels);
    d.rate = r.rate;
    rawBuf = d;
    procBuf = null;
    view = "raw";
    $("viewBtn").textContent = "View: Raw";
    playing = false; stopSource(); updatePlayBtn();
    playOffset = 0;
    META.input = r;
    draw();
    setStatus("loaded " + r.samples + " samples — set parameters, then Run");
  } catch (err) {
    setStatus("upload failed: " + err);
  }
}

function download() {
  if (!procBuf) { setStatus("run first, then download"); return; }
  const { chans, rate } = procBuf;
  const n = chans[0].length, nch = chans.length;
  const bytes = new Uint8Array(44 + n * nch * 2);
  const dv = new DataView(bytes.buffer);
  const wstr = (o, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i)); };
  wstr(0, "RIFF"); dv.setUint32(4, 36 + n * nch * 2, true); wstr(8, "WAVE");
  wstr(12, "fmt "); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true);
  dv.setUint16(22, nch, true); dv.setUint32(24, rate, true);
  dv.setUint32(28, rate * nch * 2, true); dv.setUint16(32, nch * 2, true); dv.setUint16(34, 16, true);
  wstr(36, "data"); dv.setUint32(40, n * nch * 2, true);
  const i16 = new Int16Array(bytes.buffer, 44);
  for (let i = 0; i < n; i++)
    for (let c = 0; c < nch; c++)
      i16[i * nch + c] = Math.max(-32768, Math.min(32767, Math.round(chans[c][i] * 32767)));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([bytes], { type: "audio/wav" }));
  a.download = META.name.replace(/[^A-Za-z0-9._-]+/g, "_") + "_processed.wav";
  a.click();
}

async function init() {
  META = await (await fetch("/meta")).json();
  $("title").textContent = META.name;
  const badges = $("badges");
  [META.flagsStr, META.scratch + " B scratch", META.sizeMsg].forEach(t => {
    const s = document.createElement("span"); s.className = "badge"; s.textContent = t; badges.appendChild(s);
  });
  if (META.input) {
    const d = int16ToFloat(b64ToArray(META.input.b64), META.input.channels);
    d.rate = META.input.rate;
    rawBuf = d;
  }
  view = META.isGen ? "processed" : "raw";
  $("viewBtn").textContent = "View: " + (view === "raw" ? "Raw" : "Processed");
  if (META.isGen) $("uploadBtn").style.display = "none";
  buildParams();
  resizeCanvas();
  $("playBtn").addEventListener("click", togglePlay);
  $("viewBtn").addEventListener("click", toggleView);
  $("uploadBtn").addEventListener("click", () => $("fileInp").click());
  $("fileInp").addEventListener("change", onFile);
  $("runBtn").addEventListener("click", run);
  $("dlBtn").addEventListener("click", download);
  setStatus(META.inputDesc + " — set parameters, then Run");
}
init();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="PTFX-OMM plugin tester (browser app)")
    ap.add_argument("plugin", help="path to plugin .c file")
    ap.add_argument("--wav", help="input WAV (16-bit PCM) for effect plugins")
    ap.add_argument("--length", type=float, default=2.0,
                    help="generation length in seconds for GENERATOR plugins (default 2)")
    ap.add_argument("--port", type=int, default=0, help="port (default: pick a free one)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = ap.parse_args()

    app = create_app(args.plugin, args.wav, args.length)
    port = app["port"]
    url = f"http://127.0.0.1:{port}"
    print(f"PTFX-OMM: {app['meta']['name']} → {url}")
    print(f"  {app['meta']['sizeMsg']}")
    print("Ctrl-C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        app["server"].shutdown()


if __name__ == "__main__":
    main()
