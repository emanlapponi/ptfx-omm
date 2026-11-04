"""Smoke test for host.c against the reference plugins that ship in tracker-ce.

Run: uv run pytest test_host.py
(needs ../tracker-ce/editor-fx-sdk, override: PTFX_SDK)
"""
import array
import math
import os
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))  # ptfx-omm (host.c lives here)
SDK = os.environ.get("PTFX_SDK") or os.path.normpath(
    os.path.join(HERE, "..", "tracker-ce", "editor-fx-sdk"))  # effects/, tracker_plugin.h
EFFECTS = os.path.join(SDK, "effects")
RATE = 44100
DURATION = 1.0
N = int(RATE * DURATION)
TMP = tempfile.mkdtemp(prefix="ptfx-test-")


def sine_stereo(frames=N, freq=440.0, level=0.5):
    out = array.array("h")
    for i in range(frames):
        v = int(level * 32767 * math.sin(2 * math.pi * freq * i / RATE))
        out.append(v)
        out.append(v)
    return out


def build(plugin):
    binary = os.path.join(TMP, plugin.replace(".c", ""))
    src = os.path.join(EFFECTS, plugin)
    r = subprocess.run(["cc", "-O2", "-I", SDK, src, os.path.join(HERE, "host.c"),
                        "-lm", "-o", binary], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"build {plugin}:\n{r.stderr.decode()}")
    return binary


def run(binary, in_frames, channels, *params, scratch=0, out_len=0, mono=0):
    r = subprocess.run(
        [binary, str(RATE), str(channels), str(len(in_frames) // channels),
         str(out_len), str(scratch), str(mono), *[f"{p:.6f}" for p in params]],
        input=in_frames.tobytes(), capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"run failed: {r.stderr.decode()}")
    line = [l for l in r.stderr.decode().splitlines() if l.startswith("PTFX_OUT_BYTES")]
    produced = int(line[-1].split()[-1]) if line else 0
    a = array.array("h")
    a.frombytes(r.stdout)
    return produced, a


SINE = sine_stereo()


def test_add_silence_start():
    # prepend 2.5s (param1 default 2500ms), passthrough body
    produced, out = run(build("add_silence.c"), SINE, 2, 0, 2500)
    prep = int(2.5 * RATE)
    assert produced == N + prep
    assert all(v == 0 for v in out[:prep * 2])
    assert out[prep * 2:] == SINE


def test_add_silence_end():
    produced, out = run(build("add_silence.c"), SINE, 2, 1, 2500)
    assert out[:len(SINE)] == SINE
    assert all(v == 0 for v in out[len(SINE):])


def test_multimode_filter():
    produced, out = run(build("multimode_filter.c"), SINE, 2, 0, 1200, 30, 0)
    peak = max(abs(v) for v in out)
    assert produced == N
    assert peak < 32000
    assert any(out[i] != SINE[i] for i in range(len(out)))


def test_stereo_to_mono():
    produced, out = run(build("stereo_to_mono.c"), SINE, 2, 0, 0, mono=1)
    expected = array.array("h", ((SINE[2 * i] + SINE[2 * i + 1]) // 2 for i in range(N)))
    assert produced == N and len(out) == N
    assert out == expected


def test_stereo_delay():
    produced, out = run(build("stereo_delay.c"), SINE, 2, 250, 40, 50, 0,
                        scratch=176404)
    assert produced > N  # tail appends samples
    assert out[:2 * N] != SINE and len(out) == produced * 2


def test_analog_kick_gen():
    produced, out = run(build("analog_kick_gen.c"), array.array("h"), 1,
                        140, 45, 350, 0.7, out_len=N)
    assert produced == N
    assert max(abs(v) for v in out) > 1000


def test_chiptune_gen():
    produced, out = run(build("chiptune_gen.c"), array.array("h"), 1,
                        0, 60, 0, 50, 350, 0, 0, 0, out_len=10 * RATE)
    assert 0 < produced < 10 * RATE  # ends early via return 0
    assert max(abs(v) for v in out) > 1000
