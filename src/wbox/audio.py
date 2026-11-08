"""Per-session audio server: the app's sound goes nowhere, or to a file (#4702).

wbox keeps an app's windows off the desktop; without this its sound still
reached the desktop's speakers, because the app inherits the user's
XDG_RUNTIME_DIR and finds the desktop's PipeWire and Pulse sockets there.

A session with `audio: null` or `capture` gets its own audio server instead:

- pipewire, with the stock configuration plus a null sink (`wbox-null`);
- wireplumber, with a profile that has no ALSA, Bluetooth or camera monitor,
  so it links streams to that sink and never touches the hardware;
- pipewire-pulse, for Pulse clients.

The app's environment points to it (PULSE_SERVER, PIPEWIRE_RUNTIME_DIR), which
covers Pulse, native PipeWire, ALSA's `default` device (pipewire-alsa) and
JACK (pipewire-jack). An app opening `hw:N` itself bypasses every audio
server; that gap is documented, not covered.

With `capture`, the sink's monitor is recorded as raw s16 stereo 48 kHz while
the session runs, so `stats()` can read it live; `stop()` wraps it in a WAV.
"""

from __future__ import annotations

import logging
import math
import shutil
import subprocess
import tempfile
import time
import wave
from array import array
from pathlib import Path

log = logging.getLogger(__name__)

MODES = ("null", "capture", "host")
DAEMONS = ("pipewire", "wireplumber", "pipewire-pulse")
SINK = "wbox-null"
RATE, CHANNELS, WIDTH = 48000, 2, 2  # what the capture is recorded as

_SINK_CONF = """\
context.objects = [
  { factory = adapter
    args = {
      factory.name = support.null-audio-sink
      node.name = %s
      node.description = "wbox null sink"
      media.class = Audio/Sink
      audio.position = [ FL FR ]
      monitor.channel-volumes = true
      object.linger = true
      priority.session = 2000
    }
  }
]
""" % SINK

# WirePlumber 0.4 has no profiles: its Lua config is overridden by later
# files, and each hardware monitor has its own switch.
_WP04_LUA = {
    "main.lua.d/90-wbox.lua":
        "alsa_monitor.enabled = false\n"
        "v4l2_monitor.enabled = false\n"
        "libcamera_monitor.enabled = false\n",
    "bluetooth.lua.d/90-wbox.lua": "bluez_monitor.enabled = false\n",
}

# The stock "policy" profile links streams; the two mixins drop what needs a
# user session (logind, portals, device reservation) and any saved state.
# Without hardware.audio, no ALSA device is ever opened.
_WP_CONF = """\
wireplumber.profiles = {
  wbox = { inherits = [ policy, mixin.systemwide-session, mixin.stateless ] }
}
"""


def _pw_record_has_raw() -> bool:
    """pw-record --raw exists from PipeWire 1.2; 1.0 (Ubuntu 24.04) lacks it."""
    try:
        out = subprocess.run(["pw-record", "--help"], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return True
    return "--raw" in out.stdout + out.stderr


def _wireplumber_has_profiles() -> bool:
    """WirePlumber 0.5 takes `-p <profile>`; 0.4 (Ubuntu 24.04) does not."""
    import re
    try:
        out = subprocess.run(["wireplumber", "--version"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return True
    m = re.search(r"libwireplumber (\d+)\.(\d+)", out)
    return not m or (int(m.group(1)), int(m.group(2))) >= (0, 5)


class AudioSandbox:
    """The private audio server of one session."""

    def __init__(self, mode: str, capture_dir: Path | None = None):
        self.mode = mode
        self.capture_dir = capture_dir
        self.dir: Path | None = None
        self.capture_path: Path | None = None
        self._raw: Path | None = None
        self._procs: list[subprocess.Popen] = []
        self._recorder: subprocess.Popen | None = None
        self._started_at = 0.0

    @property
    def active(self) -> bool:
        return self.dir is not None

    def start(self) -> None:
        """Start the server; raise RuntimeError naming what is missing.

        Never falls back to the desktop's server: playing on its speakers is
        what this exists to prevent.
        """
        missing = [d for d in DAEMONS if not shutil.which(d)]
        if self.mode == "capture" and not shutil.which("pw-record"):
            missing.append("pw-record")
        # WirePlumber 0.4 has no profile to run without a session bus: with
        # none it exits at once ("Cannot autolaunch D-Bus without X11
        # $DISPLAY"), nothing links the streams, and the session is silent
        # AND, under mutter, without a single frame. It gets a private one.
        wp04 = shutil.which("wireplumber") and not _wireplumber_has_profiles()
        if wp04 and not shutil.which("dbus-daemon"):
            missing.append("dbus-daemon (for WirePlumber 0.4)")
        if missing:
            raise RuntimeError(
                f"audio: {self.mode} needs {', '.join(missing)} (PipeWire), "
                "not found in PATH. Install them, or set audio: host to let "
                "the app play on the desktop's speakers")

        # Short on purpose: a socket path is limited to 108 characters, and a
        # session directory can be much deeper than that.
        self.dir = Path(tempfile.mkdtemp(prefix="wbox-a-", dir="/tmp"))
        cfg = self.dir / "cfg"
        (cfg / "pipewire" / "pipewire.conf.d").mkdir(parents=True)
        (cfg / "pipewire" / "pipewire.conf.d" / "wbox-null.conf").write_text(_SINK_CONF)
        (cfg / "wireplumber" / "wireplumber.conf.d").mkdir(parents=True)
        (cfg / "wireplumber" / "wireplumber.conf.d" / "wbox.conf").write_text(_WP_CONF)
        for rel, text in _WP04_LUA.items():
            f = cfg / "wireplumber" / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        wp_cmd = ["wireplumber"] if wp04 else ["wireplumber", "-p", "wbox"]

        # The daemons read their configuration from this XDG_CONFIG_HOME
        # only, so the user's own PipeWire tweaks never reach them.
        env = self._daemon_env()
        try:
            if wp04:
                bus = self.dir / "audio-bus"
                self._spawn(["dbus-daemon", "--session", "--nofork", "--nopidfile",
                             f"--address=unix:path={bus}"], env)
                self._wait_socket(bus, "dbus-daemon")
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
            self._spawn(["pipewire"], env)
            self._wait_socket(self.dir / "pipewire-0", "pipewire")
            wp = self._spawn(wp_cmd, env)
            self._spawn(["pipewire-pulse"], env)
            self._wait_socket(self.dir / "pulse" / "native", "pipewire-pulse")
            # Without WirePlumber nothing is linked: no sound, no screen
            # cast frame, and no error from anything else. Say it here.
            time.sleep(0.2)
            if wp.poll() is not None:
                raise RuntimeError(self._died("wireplumber", wp.returncode))
            if self.mode == "capture":
                self._start_capture(env)
        except Exception:
            self.stop()
            raise
        self._started_at = time.monotonic()
        log.info("audio: %s server in %s", self.mode, self.dir)

    def _daemon_env(self) -> dict[str, str]:
        import os
        env = {k: v for k, v in os.environ.items()
               if k not in ("PULSE_SERVER", "PIPEWIRE_REMOTE",
                            "DBUS_SESSION_BUS_ADDRESS")}
        env["XDG_RUNTIME_DIR"] = str(self.dir)
        env["PIPEWIRE_RUNTIME_DIR"] = str(self.dir)
        env["XDG_CONFIG_HOME"] = str(self.dir / "cfg")
        return env

    def _spawn(self, cmd: list[str], env: dict[str, str]) -> subprocess.Popen:
        log_file = open(self.dir / f"{cmd[0]}.log", "w")
        try:
            proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=log_file, stderr=subprocess.STDOUT)
        finally:
            log_file.close()  # the child holds its own descriptor
        self._procs.append(proc)
        return proc

    def _died(self, who: str, code: int) -> str:
        tail = ""
        log_path = self.dir / f"{who}.log"
        if log_path.exists():
            tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-5:])
        return f"audio: {who} exited ({code}) at start" + (f":\n{tail}" if tail else "")

    def _wait_socket(self, path: Path, who: str, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            dead = [p for p in self._procs if p.poll() is not None]
            if dead:
                break
            time.sleep(0.05)
        tail = ""
        log_path = self.dir / f"{who}.log"
        if log_path.exists():
            lines = log_path.read_text(errors="replace").splitlines()
            tail = "\n".join(l for l in lines if "[E]" in l or " E " in l)[-600:]
        raise RuntimeError(f"audio: {who} did not start"
                           + (f":\n{tail}" if tail else ""))

    def _start_capture(self, env: dict[str, str]) -> None:
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.capture_path = self.capture_dir / f"audio-{stamp}.wav"
        self._raw = self.capture_path.with_suffix(".raw")
        raw = open(self._raw, "wb")
        err = open(self.dir / "pw-record.log", "w")
        # Raw on stdout rather than pw-record's WAV, whose header is only
        # right once it closes: stats() reads the capture while it grows.
        # From PipeWire 1.2 that takes --raw (without it, stdout gets a Sun
        # .au stream, which reads as loud noise); 1.0 (Ubuntu 24.04) has no
        # --raw, exits on it, and already writes raw samples to stdout.
        raw_flag = ["--raw"] if _pw_record_has_raw() else []
        try:
            self._recorder = subprocess.Popen(
                ["pw-record", *raw_flag, "--target", SINK,
                 "-P", "{ stream.capture.sink = true }",
                 "--format", "s16", "--rate", str(RATE),
                 "--channels", str(CHANNELS), "-"],
                env=env, stdin=subprocess.DEVNULL, stdout=raw, stderr=err)
        finally:
            raw.close()
            err.close()
        # A recorder that dies at once leaves a capture of 0 s, which reads
        # as "the app made no sound". Say why instead.
        time.sleep(0.2)
        if self._recorder.poll() is not None:
            code = self._recorder.returncode
            self._recorder = None
            raise RuntimeError(self._died("pw-record", code))

    def _pcm(self) -> bytes:
        """The capture so far as little-endian s16: raw as recorded, or, if
        a pw-record ever wrote a Sun .au stream, its header skipped and its
        big-endian samples swapped."""
        if not self._raw or not self._raw.exists():
            return b""
        data = self._raw.read_bytes()
        if data[:4] != b".snd" or len(data) < 8:
            return data
        data = data[int.from_bytes(data[4:8], "big"):]
        data = data[:len(data) - len(data) % WIDTH]
        samples = array("h", data)
        samples.byteswap()
        return samples.tobytes()

    def apply(self, env: dict[str, str]) -> None:
        """Point an app environment at this server, and only at it."""
        if not self.active:
            return
        env.pop("PIPEWIRE_REMOTE", None)
        env.pop("PULSE_SINK", None)
        env["PULSE_SERVER"] = f"unix:{self.dir / 'pulse' / 'native'}"
        env["PIPEWIRE_RUNTIME_DIR"] = str(self.dir)

    def stats(self, seconds: float | None = None) -> dict:
        """Level of what the app played: the whole capture or its last seconds."""
        if self.mode != "capture" or not self.capture_path:
            return {"error": f"audio is {self.mode}: nothing is captured "
                             "(set audio: capture)"}
        if self._raw is not None:
            data = self._pcm()
        else:  # the session has stopped: the capture is a WAV now
            with wave.open(str(self.capture_path)) as w:
                data = w.readframes(w.getnframes())
        frame = CHANNELS * WIDTH
        data = data[:len(data) - len(data) % frame]
        if seconds is not None:
            data = data[-int(seconds * RATE) * frame:]
        samples = array("h", data)
        n = len(samples)
        rms = math.sqrt(sum(s * s for s in samples) / n) / 32768 if n else 0.0
        peak = max((abs(s) for s in samples), default=0) / 32768
        return {
            "path": str(self.capture_path),
            "seconds": round(len(data) / frame / RATE, 2),
            "rms": round(rms, 4),
            "peak": round(peak, 4),
            "silent": peak < 1e-3,
        }

    def stop(self) -> None:
        if self._recorder is not None:
            self._recorder.terminate()
            try:
                self._recorder.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._recorder.kill()
            self._recorder = None
            self._finish_capture()
        for proc in reversed(self._procs):
            if proc.poll() is None:
                proc.terminate()
        for proc in reversed(self._procs):
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        self._procs.clear()
        if self.dir is not None:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.dir = None

    def _finish_capture(self) -> None:
        """Wrap the raw capture in a WAV, the file the session leaves behind."""
        if not self._raw or not self._raw.exists():
            return
        data = self._pcm()
        frame = CHANNELS * WIDTH
        with wave.open(str(self.capture_path), "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(WIDTH)
            w.setframerate(RATE)
            w.writeframes(data[:len(data) - len(data) % frame])
        self._raw.unlink()
        # stats() keeps answering after the session from the WAV's frames
        self._raw = None
