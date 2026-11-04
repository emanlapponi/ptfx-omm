"""Smoke test for ptfx_omm.py's HTTP endpoints (no browser needed).

Run: uv run pytest test_ptfx_omm.py
(needs ../tracker-ce/editor-fx-sdk, override: PTFX_SDK)
"""
import array
import base64
import json
import math
import os
import sys
import tempfile
import urllib.request
import wave

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))  # ptfx-omm (ptfx_omm.py lives here)
SDK = os.environ.get("PTFX_SDK") or os.path.normpath(
    os.path.join(HERE, "..", "tracker-ce", "editor-fx-sdk"))  # effects/
sys.path.insert(0, HERE)
import ptfx_omm as A  # noqa: E402

RATE = 44100
N = RATE  # 1 second


def make_sine_wav(path, channels=2):
    w = wave.open(path, "wb")
    w.setnchannels(channels)
    w.setsampwidth(2)
    w.setframerate(RATE)
    out = array.array("h")
    for i in range(N):
        v = int(0.5 * 32767 * math.sin(2 * math.pi * 440 * i / RATE))
        out.append(v)
        if channels == 2:
            out.append(v)
    w.writeframes(out.tobytes())
    w.close()
    return path


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.status, r.read()


def post(url, obj):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"http": e.code, "body": e.read().decode()[:200]}


@pytest.fixture
def wav(tmp_path):
    p = str(tmp_path / "sine.wav")
    make_sine_wav(p, 2)
    return p


@pytest.fixture
def app_factory(tmp_path):
    apps = []

    def factory(plugin, wav_path=None, length=2.0):
        app = A.create_app(os.path.join(SDK, "effects", plugin), wav_path, length)
        apps.append(app)
        return app

    yield factory
    for app in apps:
        app["server"].shutdown()


def test_add_silence(app_factory, wav):
    # 1) effect plugin: serves the page + meta, extends output by the added silence
    app = app_factory("add_silence.c", wav)
    base = f"http://127.0.0.1:{app['port']}"

    status, body = get(base + "/")
    assert status == 200 and b"<canvas" in body

    meta = json.loads(get(base + "/meta")[1])
    assert meta["name"] == "Add Silence"
    assert len(meta["params"]) == 2 and meta["params"][1]["label"] == "Length"
    assert meta["input"] and meta["input"]["rate"] == RATE and meta["input"]["channels"] == 2
    assert meta["isGen"] is False

    r = post(base + "/run", {"values": [0, 2500]})
    assert r.get("produced") == N + int(2.5 * RATE)
    assert r.get("channels") == 2
    data = base64.b64decode(r.get("b64", ""))
    assert len(data) == r["produced"] * 2 * 2

    r = post(base + "/run", {"values": [0, 100]})
    assert r.get("produced") == N + int(0.1 * RATE)
    r = post(base + "/run", {"values": [1, 100]})
    assert r.get("produced") == N + int(0.1 * RATE)  # Where=End still extends


def test_analog_kick_gen(app_factory):
    # 2) generator plugin: no input, mono output of the requested length
    app = app_factory("analog_kick_gen.c", None, 1.0)
    base = f"http://127.0.0.1:{app['port']}"
    meta = json.loads(get(base + "/meta")[1])
    assert meta["isGen"] is True and meta["input"] is None

    r = post(base + "/run", {"values": [140, 45, 350, 0.7]})
    assert r.get("produced") == N
    assert r.get("channels") == 1
    a = array.array("h")
    a.frombytes(base64.b64decode(r["b64"]))
    assert max(abs(v) for v in a) > 1000


def test_upload_path(app_factory, wav):
    # 3) no --wav: upload PCM via /input, then run
    app = app_factory("add_silence.c", None)
    base = f"http://127.0.0.1:{app['port']}"
    meta = json.loads(get(base + "/meta")[1])
    assert meta["input"] is None
    r = post(base + "/run", {"values": [0, 2500]})
    err = r.get("error") or r.get("body", "")
    assert "upload" in err

    pcm = base64.b64encode(A.interleave([array.array("h", [i % 1000 for i in range(N)]),
                                         array.array("h", [i % 1000 for i in range(N)])])).decode()
    r = post(base + "/input", {"rate": RATE, "channels": 2, "pcm": pcm})
    assert r.get("channels") == 2 and r.get("samples") == N
    meta = json.loads(get(base + "/meta")[1])
    assert meta["input"] and meta["input"]["rate"] == RATE
    r = post(base + "/run", {"values": [0, 2500]})
    assert r.get("produced") == N + int(2.5 * RATE)

    # trailing-garbage WAV: JS respects the declared data chunk size; some
    # writers append stray bytes. Guard an off-by-one payload at the Python layer.
    raw = open(wav, "rb").read()
    data_off = raw.index(b"data") + 8
    data_sz = int.from_bytes(raw[data_off - 4:data_off], "little")
    pcm = raw[data_off:data_off + data_sz]  # exactly what the JS parser sends
    r = post(base + "/input", {"rate": RATE, "channels": 2, "pcm": base64.b64encode(pcm).decode()})
    assert r.get("samples") == N
    r = post(base + "/run", {"values": [0, 2500]})
    assert r.get("produced") == N + int(2.5 * RATE)
    odd = base64.b64encode(pcm[:-1]).decode()  # simulate an off-by-one payload
    r = post(base + "/input", {"rate": RATE, "channels": 2, "pcm": odd})
    assert r.get("samples") == N - 1


def test_stereo_to_mono(app_factory, wav):
    # 4) 2ch input -> 1ch output
    app = app_factory("stereo_to_mono.c", wav)
    base = f"http://127.0.0.1:{app['port']}"
    r = post(base + "/run", {"values": [0, 0]})
    assert r.get("channels") == 1 and r.get("produced") == N


def test_mono_upmix(app_factory, tmp_path):
    # 5) mono input to a STEREO plugin: raw view stays mono, run is upmixed to
    # stereo. Uses stereo_delay in pure-delay mode (Fdbk=0, Mix=100, Tail=Cut
    # Off) so the output is exactly the delayed signal with no tail.
    mono_wav = str(tmp_path / "mono.wav")
    make_sine_wav(mono_wav, 1)
    with wave.open(mono_wav, "rb") as w:
        mono = array.array("h")
        mono.frombytes(w.readframes(w.getnframes()))

    app = app_factory("stereo_delay.c", mono_wav)
    base = f"http://127.0.0.1:{app['port']}"
    meta = json.loads(get(base + "/meta")[1])
    assert meta["input"]["channels"] == 1

    r = post(base + "/run", {"values": [40, 0, 100, 1]})  # 40 ms, Fdbk=0, Mix=100, Cut Off
    assert r.get("channels") == 2
    d = int(40 * RATE / 1000)
    assert r.get("produced") == N  # no tail (Cut Off)
    a = array.array("h")
    a.frombytes(base64.b64decode(r["b64"]))
    L, R = a[0::2], a[1::2]
    assert all(v == 0 for v in L[:d]) and all(v == 0 for v in R[:d])
    assert L[d:d + 5000] == mono[:5000] and R[d:d + 5000] == mono[:5000]
