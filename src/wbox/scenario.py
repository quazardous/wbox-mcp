"""
scenario.py — a run of an app that can be played again, and compared.

Driving a game by hand gives one-off pictures. Comparing a fix, two drivers or
two GPUs needs the same run played again and again, with what it produced
kept side by side. A scenario is that run, written down:

    name: kokiri-walk
    config: wbox/config.yaml          # the box (relative to this file)
    overrides: {screen: 1280x720}     # over the config, for this scenario
    setup:
      copy: {saves/start.sav: ~/.local/share/game/save.sav}  # restored after
      env:  {RX_VK_FRAME_LOG: "{run}/frames.csv"}
    masks:
      hud:   [{rect: [0, 0, 320, 90]}]
      clock: [{rect: ["85%", "2%", "13%", "6%"]}]
    default_mask: [hud, clock]
    steps:
      - wait: {stable: 2.0, timeout: 120}
      - wait: {match: refs/title.png, threshold: 0.05}
      - checkpoint: title
      - pad_script: "0:a,2:a"
      - wait: {log: "scene loaded", timeout: 60}
      - record: {seconds: 10, name: walk, during: {type: pad_script, script: "0:lup/10000"}}
      - checkpoint: {name: after-walk, mask: [hud]}
    collect: ["~/Games/game/game_log.txt"]
    metrics:
      - {name: frametime, file: "{run}/frames.csv", column: 2, unit: ms}
    manifest:
      app_version: git -C ~/src/game rev-parse --short HEAD

Why it waits instead of sleeping: loading takes 4 s on one run and 6 s on the
next, so inputs at fixed times land on different frames and captures at
"t = 10 s" show different things. Each wait is a point where every run is
back in step, and checkpoints are taken at those points.

Times (steps, checkpoints) count from just before the launch, so a
start-up is measured too; results.json keeps launch_seconds apart.

Each run writes runs/<name>/<date>/: manifest.json (what it ran on: wbox,
the GPU and its driver, the kernel, hashes of the scenario, the config,
the setup files and the masks), results.json (each step, its duration,
the time each checkpoint was reached), checkpoints/*.png (raw: masks apply
at comparison), app.log, the collected files and the metrics.
"""

from __future__ import annotations

import datetime as _dt
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import time
from pathlib import Path

import yaml

from wbox import imaging

# step name → (box method, [(argument, default)], the argument a bare value
# fills in). Same names and arguments as the MCP tools.
_REQUIRED = object()
ACTIONS = {
    "key": ("key", [("shortcut", _REQUIRED), ("hold_ms", None)], "shortcut"),
    "keys": ("keys", [("shortcuts", _REQUIRED), ("delay_ms", 100), ("hold_ms", None)], "shortcuts"),
    "press": ("press", [("keys", _REQUIRED), ("hold_ms", 50)], "keys"),
    "key_down": ("key_down", [("keys", _REQUIRED)], "keys"),
    "key_up": ("key_up", [("keys", None)], "keys"),
    "type_text": ("type_text", [("text", _REQUIRED)], "text"),
    "click": ("click", [("x", _REQUIRED), ("y", _REQUIRED), ("button", 1)], None),
    "dblclick": ("double_click", [("x", _REQUIRED), ("y", _REQUIRED), ("button", 1)], None),
    "mouse_move": ("mouse_move", [("x", _REQUIRED), ("y", _REQUIRED)], None),
    "scroll": ("scroll", [("x", _REQUIRED), ("y", _REQUIRED), ("notches", _REQUIRED),
                          ("horizontal", False)], None),
    "drag": ("drag", [("x1", _REQUIRED), ("y1", _REQUIRED), ("x2", _REQUIRED),
                      ("y2", _REQUIRED), ("button", 1), ("steps", 10), ("seconds", 0.3)], None),
    "pad_press": ("pad_press", [("buttons", _REQUIRED), ("hold_ms", 100)], "buttons"),
    "pad_hold": ("pad_hold", [("buttons", _REQUIRED), ("down", True)], "buttons"),
    "pad_stick": ("pad_stick", [("stick", _REQUIRED), ("x", _REQUIRED), ("y", _REQUIRED),
                                ("hold_ms", None)], None),
    "pad_trigger": ("pad_trigger", [("trigger", _REQUIRED), ("value", 1.0),
                                    ("hold_ms", None)], "trigger"),
    "pad_script": ("pad_script", [("script", _REQUIRED)], "script"),
    "pad_neutral": ("pad_neutral", [], None),
}
SPECIAL = ("wait", "checkpoint", "record", "sleep")
_TOP_KEYS = {"name", "description", "config", "overrides", "setup", "masks",
             "default_mask", "steps", "collect", "metrics", "manifest", "tolerance"}


class ScenarioError(Exception):
    """A scenario that cannot be run as written."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


# ── Loading ──────────────────────────────────────────────────────────


def load_scenario(path) -> dict:
    """Read and check a scenario: every error is found before anything runs."""
    path = Path(path).expanduser().resolve()
    try:
        raw = path.read_bytes()
        sc = yaml.safe_load(raw) or {}
    except OSError as exc:
        raise ScenarioError(f"cannot read {path}: {exc}") from None
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path}: invalid YAML: {exc}") from None
    if not isinstance(sc, dict):
        raise ScenarioError(f"{path}: expected a mapping at the top")
    unknown = set(sc) - _TOP_KEYS
    if unknown:
        raise ScenarioError(f"unknown keys {sorted(unknown)}; expected {sorted(_TOP_KEYS)}")
    sc.setdefault("name", path.stem)
    if not re.fullmatch(r"[\w.-]+", str(sc["name"])):
        raise ScenarioError(f"name {sc['name']!r}: letters, digits, '.', '-', '_' only")
    sc["_path"] = str(path)
    sc["_dir"] = str(path.parent)
    sc["_digest"] = _sha(raw)
    masks = sc.get("masks") or {}
    if not isinstance(masks, dict):
        raise ScenarioError("masks: expected name: [zones]")
    for name, zones in masks.items():
        try:
            imaging.resolve_mask(zones)
        except ValueError as exc:
            raise ScenarioError(f"mask {name}: {exc}") from None
    try:
        imaging.resolve_mask(sc.get("default_mask"), masks)
    except ValueError as exc:
        raise ScenarioError(f"default_mask: {exc}") from None
    steps = sc.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ScenarioError("steps: expected a non-empty list")
    names = set()
    sc["steps"] = [_check_step(i, s, masks, names) for i, s in enumerate(steps, 1)]
    setup = sc.get("setup") or {}
    for src in (setup.get("copy") or {}):
        p = Path(sc["_dir"]) / os.path.expanduser(src)
        if not p.exists():
            raise ScenarioError(f"setup.copy: {src} does not exist (relative to {sc['_dir']})")
    return sc


def _check_step(i: int, step, masks: dict, checkpoint_names: set) -> dict:
    where = f"step {i}"
    if not isinstance(step, dict) or len(step) != 1:
        raise ScenarioError(f"{where}: expected one action, e.g. `- key: a`, got {step!r}")
    (kind, args), = step.items()
    if kind not in ACTIONS and kind not in SPECIAL:
        raise ScenarioError(f"{where}: unknown action {kind!r}; expected one of "
                            f"{sorted(ACTIONS) + list(SPECIAL)}")
    if kind == "checkpoint":
        args = {"name": args} if isinstance(args, str) else dict(args or {})
        name = args.get("name")
        if not name or not re.fullmatch(r"[\w.-]+", str(name)):
            raise ScenarioError(f"{where}: checkpoint needs a name (letters, digits, . - _)")
        if name in checkpoint_names:
            raise ScenarioError(f"{where}: checkpoint {name!r} appears twice")
        checkpoint_names.add(name)
    elif kind == "wait":
        args = dict(args or {})
        conds = [k for k in ("stable", "match", "log", "seconds") if k in args]
        if len(conds) != 1:
            raise ScenarioError(f"{where}: wait needs exactly one of stable, match, log, seconds")
    elif kind == "sleep":
        args = {"seconds": args} if not isinstance(args, dict) else dict(args)
    elif kind == "record":
        args = dict(args or {})
        if "seconds" not in args:
            raise ScenarioError(f"{where}: record needs seconds")
    else:
        args = _action_args(kind, args, where)
    if isinstance(args, dict) and "mask" in args:
        try:
            imaging.resolve_mask(args["mask"], masks)
        except ValueError as exc:
            raise ScenarioError(f"{where}: {exc}") from None
    return {"kind": kind, "args": args}


def _action_args(kind: str, args, where: str) -> dict:
    _, params, primary = ACTIONS[kind]
    if not isinstance(args, dict):
        if primary is None:
            raise ScenarioError(f"{where}: {kind} takes named arguments "
                                f"({', '.join(p for p, _ in params)})")
        args = {primary: args}
    known = {p for p, _ in params}
    extra = set(args) - known
    if extra:
        raise ScenarioError(f"{where}: {kind} has no argument {sorted(extra)}; "
                            f"expected {sorted(known)}")
    out = {}
    for p, default in params:
        if p in args:
            out[p] = args[p]
        elif default is _REQUIRED:
            raise ScenarioError(f"{where}: {kind} needs {p}")
        else:
            out[p] = default
    # The list-taking actions accept one name as well as a list.
    for p in ("keys", "buttons", "shortcuts"):
        if isinstance(out.get(p), str):
            out[p] = [out[p]]
    return out


# ── Running ──────────────────────────────────────────────────────────


def _expand(value: str, run_dir: Path, sc: dict) -> str:
    """{run} and {scenario} in paths and env values; ~ in paths."""
    return str(value).replace("{run}", str(run_dir)).replace("{scenario}", sc["_dir"])


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Run:
    """One play of a scenario, into a directory of its own."""

    def __init__(self, sc: dict, runs_root: Path | None = None,
                 config_path: str | None = None, overrides: list[str] | None = None,
                 label: str = "", box_factory=None, log=print):
        self.sc = sc
        self.log = log
        # Absolute: captures are written by path, from wherever the box runs
        root = (Path(runs_root).expanduser() if runs_root else Path(sc["_dir"]) / "runs").resolve()
        stamp = _now_stamp() + (f"-{label}" if label else "")
        base = root / sc["name"] / stamp
        n, self.dir = 1, base
        while self.dir.exists():
            n += 1
            self.dir = base.with_name(f"{base.name}-{n}")
        self.config_path = config_path
        self.cli_overrides = overrides or []
        self.box_factory = box_factory
        self.masks = sc.get("masks") or {}
        self.default_mask = sc.get("default_mask")
        self.tolerance = int(sc.get("tolerance", 8))
        self.steps: list[dict] = []
        self.checkpoints: dict[str, dict] = {}
        self.records: list[dict] = []
        self._restore: list[tuple[Path, Path | None]] = []
        self._t0 = 0.0
        self.box = None
        self.cfg: dict = {}

    # ── helpers ──

    def _zones(self, spec=None, use_default=True) -> list[dict]:
        if spec is None and use_default:
            spec = self.default_mask
        return imaging.resolve_mask(spec, self.masks)

    def _t(self) -> float:
        return round(time.monotonic() - self._t0, 3)

    def _load_config(self) -> dict:
        from wbox.config import apply_overrides, load_config
        path = self.config_path or self.sc.get("config")
        if not path:
            raise ScenarioError("no wbox config: set `config:` in the scenario or pass --config")
        p = Path(os.path.expanduser(path))
        if not p.is_absolute():
            p = Path(self.sc["_dir"]) / p if not self.config_path else p.resolve()
        cfg = load_config(p)
        if not cfg:
            raise ScenarioError(f"cannot read the wbox config {p}")
        cfg = _deep_merge(cfg, self.sc.get("overrides") or {})
        if self.cli_overrides:
            apply_overrides(cfg, self.cli_overrides)
        return cfg

    def _setup_files(self) -> None:
        """Put the scenario's files in place, keeping what they replace."""
        copies = (self.sc.get("setup") or {}).get("copy") or {}
        backup = self.dir / "_backup"
        for i, (src, dst) in enumerate(copies.items()):
            src_p = Path(self.sc["_dir"]) / os.path.expanduser(src)
            dst_p = Path(os.path.expanduser(_expand(dst, self.dir, self.sc)))
            saved = None
            if dst_p.exists() or dst_p.is_symlink():
                backup.mkdir(parents=True, exist_ok=True)
                saved = backup / f"{i}_{dst_p.name}"
                shutil.move(str(dst_p), str(saved))
            self._restore.append((dst_p, saved))
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            if src_p.is_dir():
                shutil.copytree(src_p, dst_p)
            else:
                shutil.copy2(src_p, dst_p)

    def _restore_files(self) -> list[str]:
        errors = []
        for dst, saved in reversed(self._restore):
            try:
                if dst.is_dir() and not dst.is_symlink():
                    shutil.rmtree(dst)
                elif dst.exists() or dst.is_symlink():
                    dst.unlink()
                if saved is not None:
                    shutil.move(str(saved), str(dst))
            except OSError as exc:
                errors.append(f"{dst}: {exc} (the original is in {saved})")
        self._restore.clear()
        if not errors:
            shutil.rmtree(self.dir / "_backup", ignore_errors=True)
        return errors

    def _shot(self, name: str):
        Path(name).parent.mkdir(parents=True, exist_ok=True)
        r = self.box.screenshot(name)
        if "error" in r:
            raise ScenarioError(f"screenshot failed: {r['error']}")
        return Path(r["path"])

    # ── steps ──

    def _wait(self, a: dict) -> dict:
        timeout = float(a.get("timeout", 60))
        deadline = time.monotonic() + timeout
        tol = int(a.get("tolerance", self.tolerance))
        zones = self._zones(a.get("mask"))
        if "seconds" in a:
            time.sleep(float(a["seconds"]))
            return {}
        if "log" in a:
            path = Path(os.path.expanduser(_expand(a.get("file", "{run}/app.log"), self.dir, self.sc)))
            pattern = re.compile(a["log"])
            start = 0
            if a.get("since") == "step" and path.exists():
                start = path.stat().st_size
            while time.monotonic() < deadline:
                try:
                    with open(path, "rb") as f:
                        f.seek(start)
                        text = f.read().decode("utf-8", "replace")
                    m = pattern.search(text)
                    if m:
                        return {"matched": m.group(0)[:200]}
                except OSError:
                    pass
                time.sleep(0.2)
            raise ScenarioError(f"no line matching {a['log']!r} in {path} after {timeout:g} s")
        interval = float(a.get("interval", 0.2))
        scratch = self.dir / "_wait"
        if "match" in a:
            ref = Path(os.path.expanduser(a["match"]))
            if not ref.is_absolute():
                ref = Path(self.sc["_dir"]) / ref
            ref_img = imaging.load_rgb(ref)
            threshold = float(a.get("threshold", 0.02))
            best = None
            while True:
                d = imaging.diff(ref_img, imaging.load_rgb(self._shot(str(scratch / "now.png"))),
                                 zones, tol, Path(self.sc["_dir"]))
                if "error" in d:
                    raise ScenarioError(f"wait match {ref.name}: {d['error']}")
                best = d["differ"] if best is None else min(best, d["differ"])
                if d["differ"] <= threshold:
                    return {"differ": d["differ"]}
                if time.monotonic() >= deadline:
                    raise ScenarioError(
                        f"the screen never matched {ref.name}: closest {best:.2%} of pixels "
                        f"differ, threshold {threshold:.2%} (see {scratch / 'now.png'})")
                time.sleep(interval)
        # stable
        need = float(a["stable"])
        threshold = float(a.get("threshold", 0.0))
        prev, since = None, None
        while True:
            now_t = time.monotonic()
            img = imaging.load_rgb(self._shot(str(scratch / "now.png")))
            if prev is not None:
                d = imaging.diff(prev, img, zones, tol, Path(self.sc["_dir"]))
                if "error" in d:
                    raise ScenarioError(f"wait stable: {d['error']}")
                if d["differ"] <= threshold:
                    since = since if since is not None else prev_t
                    if now_t - since >= need:
                        return {"stable_for": round(now_t - since, 2)}
                else:
                    since = None
            prev, prev_t = img, now_t
            if now_t >= deadline:
                raise ScenarioError(f"the screen did not hold still for {need:g} s within "
                                    f"{timeout:g} s (mask: {a.get('mask', self.default_mask)})")
            time.sleep(interval)

    def _checkpoint(self, a: dict) -> dict:
        name = a["name"]
        t = self._t()
        path = self._shot(str(self.dir / "checkpoints" / f"{name}.png"))
        spec = a.get("mask", self.default_mask)
        self.checkpoints[name] = {
            "t": t, "path": str(path.relative_to(self.dir)),
            "mask": imaging.resolve_mask(spec, self.masks),
            "mask_spec": spec,
            # what compare uses for this checkpoint unless told otherwise:
            # a shimmering logo needs more slack than a menu
            "tolerance": int(a.get("tolerance", self.tolerance)),
        }
        return {"t": t}

    def _record(self, a: dict) -> dict:
        args = {k: a[k] for k in ("seconds", "region", "fps", "name", "during", "assemble") if k in a}
        r = self.box.record(**args)
        if "error" in r:
            raise ScenarioError(f"record: {r['error']}")
        if "mask" in a and r.get("frames"):
            # Which frames differ from the one before, outside the mask
            zones = self._zones(a["mask"], use_default=False)
            frames = sorted(Path(r["frames_dir"]).glob("frame_*.png"))
            changed, prev = [], None
            for i, f in enumerate(frames):
                img = imaging.load_rgb(f)
                if prev is not None and imaging.diff(prev, img, zones, self.tolerance,
                                                     Path(self.sc["_dir"]))["differ"] > 0:
                    changed.append(i)
                prev = img
            r["changed_frames"] = changed
            r["changes"] = len(changed)
        r["frames_dir"] = str(Path(r["frames_dir"]).resolve().relative_to(self.dir.resolve()))
        self.records.append(r)
        return {k: r[k] for k in ("frames", "fps", "changes") if k in r}

    def _action(self, kind: str, a: dict) -> dict:
        method = ACTIONS[kind][0]
        r = getattr(self.box, method)(*a.values())
        if isinstance(r, dict) and "error" in r:
            raise ScenarioError(f"{kind}: {r['error']}")
        return {}

    def _do(self, step: dict) -> dict:
        kind, a = step["kind"], step["args"]
        if kind == "wait":
            return self._wait(a)
        if kind == "sleep":
            time.sleep(float(a["seconds"]))
            return {}
        if kind == "checkpoint":
            return self._checkpoint(a)
        if kind == "record":
            return self._record(a)
        return self._action(kind, a)

    # ── the run ──

    def execute(self) -> dict:
        from wbox.server import _build_app_cmd, _build_app_env, build_compositor
        self.dir.mkdir(parents=True)
        status, reason = "ok", ""
        restore_errors: list[str] = []
        try:
            self.cfg = self._load_config()
            factory = self.box_factory or build_compositor
            self.box = factory(self.cfg)
            if self.box.is_running():
                raise ScenarioError(
                    f"a box named {self.cfg.get('name')!r} is already running: stop it first")
            self.box.state.screenshot_dir = self.dir / "shots"
            self.box.app_output = self.dir / "app.log"
            self._setup_files()
            env = _build_app_env(self.cfg)
            for k, v in ((self.sc.get("setup") or {}).get("env") or {}).items():
                env[str(k)] = _expand(v, self.dir, self.sc)
            for k, v in list(env.items()):
                env[k] = _expand(v, self.dir, self.sc)
            cmd = _build_app_cmd(self.cfg)
            if not cmd:
                raise ScenarioError("the wbox config has no app.command")
            self.log(f"[{self.sc['name']}] launch → {self.dir}")
            # The clock starts before the launch: start-up is often what is
            # measured, and launch() itself waits (timeouts.app_render). A
            # scenario timing a start-up sets that wait low and waits on
            # the app's log instead.
            self._t0 = time.monotonic()
            launched = self.box.launch(cmd, env)
            self.launch_seconds = self._t()
            if "error" in launched:
                raise ScenarioError(f"launch: {launched['error']}")
            for i, step in enumerate(self.sc["steps"], 1):
                t = self._t()
                entry = {"n": i, "kind": step["kind"], "args": step["args"], "t": t}
                self.steps.append(entry)
                try:
                    entry.update(self._do(step) or {})
                finally:
                    entry["seconds"] = round(self._t() - t, 3)
                self.log(f"[{self.sc['name']}] {i:>3} {step['kind']:<11} {entry['seconds']:>7.2f} s")
                if self.box.state.app_proc is not None and self.box.state.app_proc.poll() is not None:
                    raise ScenarioError(f"the app exited (code {self.box.state.app_proc.returncode}) "
                                        f"after step {i}")
        except ScenarioError as exc:
            status, reason = "failed", str(exc)
        except Exception as exc:  # a bug, or the box falling over: still clean up
            status, reason = "error", f"{type(exc).__name__}: {exc}"
        finally:
            if status != "ok" and self.steps:
                self.steps[-1]["error"] = reason
            if status != "ok" and self.box is not None and self.box.is_running():
                try:
                    self._shot(str(self.dir / "failure.png"))
                except Exception:
                    pass
            collected = self._collect()
            if self.box is not None:
                try:
                    self.box.stop()
                except Exception:
                    pass
            restore_errors = self._restore_files()
            shutil.rmtree(self.dir / "_wait", ignore_errors=True)
        metrics = self._metrics()
        results = {
            "scenario": self.sc["name"], "status": status, "reason": reason,
            "seconds": self._t() if self._t0 else 0.0,
            "launch_seconds": getattr(self, "launch_seconds", None),
            "steps": self.steps, "checkpoints": self.checkpoints,
            "records": self.records, "metrics": metrics, "collected": collected,
            "scenario_dir": self.sc["_dir"], "tolerance": self.tolerance,
            **({"restore_errors": restore_errors} if restore_errors else {}),
        }
        (self.dir / "results.json").write_text(json.dumps(results, indent=1, default=str))
        (self.dir / "manifest.json").write_text(json.dumps(self._manifest(), indent=1))
        self.log(f"[{self.sc['name']}] {status}{': ' + reason if reason else ''} → {self.dir}")
        return {"run": str(self.dir), **results}

    def _collect(self) -> list[str]:
        out = []
        for pattern in self.sc.get("collect") or []:
            pat = os.path.expanduser(_expand(pattern, self.dir, self.sc))
            for f in glob.glob(pat):
                p = Path(f)
                if self.dir.resolve() in p.resolve().parents:
                    out.append(str(p.resolve().relative_to(self.dir.resolve())))
                    continue
                dest = self.dir / "collected" / p.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    (shutil.copytree if p.is_dir() else shutil.copy2)(p, dest)
                    out.append(str(dest.relative_to(self.dir)))
                except OSError:
                    pass
        return out

    def _metrics(self) -> dict:
        out = {}
        for m in self.sc.get("metrics") or []:
            name = m.get("name") or "metric"
            path = Path(os.path.expanduser(_expand(m.get("file", ""), self.dir, self.sc)))
            if not path.is_absolute():
                path = self.dir / path
            try:
                out[name] = csv_stats(path, m.get("column", 0), float(m.get("scale", 1.0)),
                                      int(m.get("skip", 0)))
                out[name]["unit"] = m.get("unit", "")
            except (OSError, ValueError) as exc:
                out[name] = {"error": str(exc)}
        return out

    def _manifest(self) -> dict:
        from wbox import __version__
        sc_dir = Path(self.sc["_dir"])
        files = {}
        for src in ((self.sc.get("setup") or {}).get("copy") or {}):
            p = sc_dir / os.path.expanduser(src)
            files[src] = _digest_path(p)
        masks = {n: imaging.mask_digest(imaging.resolve_mask(z), sc_dir)
                 for n, z in (self.sc.get("masks") or {}).items()}
        cfg_clean = {k: v for k, v in self.cfg.items() if not k.startswith("_")}
        extra = {}
        for key, command in (self.sc.get("manifest") or {}).items():
            try:
                r = subprocess.run(os.path.expanduser(command), shell=True, capture_output=True,
                                   text=True, timeout=15, cwd=sc_dir)
                extra[key] = (r.stdout or r.stderr).strip()[:500]
            except (OSError, subprocess.TimeoutExpired) as exc:
                extra[key] = f"<failed: {exc}>"
        u = platform.uname()
        return {
            "wbox": __version__,
            "date": _dt.datetime.now().isoformat(timespec="seconds"),
            "host": {"kernel": u.release, "machine": u.machine, "python": platform.python_version(),
                     "cpu": _cpu_name(), "gpus": _gpus()},
            "scenario": {"name": self.sc["name"], "path": self.sc["_path"],
                         "digest": self.sc["_digest"]},
            "config": {"digest": _sha(json.dumps(cfg_clean, sort_keys=True, default=str).encode()),
                       "compositor": cfg_clean.get("compositor"), "screen": cfg_clean.get("screen"),
                       "headless": cfg_clean.get("headless"), "gamepad": cfg_clean.get("gamepad"),
                       "app": cfg_clean.get("app")},
            "setup_files": files,
            "masks": masks,
            "extra": extra,
        }


def _digest_path(p: Path) -> str:
    if p.is_dir():
        h = hashlib.sha256()
        for f in sorted(p.rglob("*")):
            if f.is_file():
                h.update(str(f.relative_to(p)).encode())
                h.update(f.read_bytes())
        return h.hexdigest()[:12]
    try:
        return _sha(p.read_bytes())
    except OSError:
        return "<missing>"


def _cpu_name() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor()


def _gpus() -> list[dict]:
    """The Vulkan devices and their drivers, as vulkaninfo reports them."""
    if not shutil.which("vulkaninfo"):
        return []
    try:
        r = subprocess.run(["vulkaninfo", "--summary"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    gpus, cur = [], None
    for line in r.stdout.splitlines():
        m = re.match(r"\s*(deviceName|driverName|driverInfo|deviceType|apiVersion)\s*=\s*(.*)", line)
        if not m:
            continue
        if m.group(1) == "apiVersion" and cur is not None and "deviceName" in cur:
            gpus.append(cur)
            cur = None
        if cur is None:
            cur = {}
        cur[m.group(1)] = m.group(2).strip()
    if cur and "deviceName" in cur:
        gpus.append(cur)
    return [g for g in gpus if g.get("deviceType") != "PHYSICAL_DEVICE_TYPE_CPU"] or gpus


def csv_stats(path: Path, column, scale: float = 1.0, skip: int = 0) -> dict:
    """Mean and percentiles of one column of a CSV (a frame-time log)."""
    values = []
    header = None
    with open(path, newline="") as f:
        for i, line in enumerate(f):
            if i < skip or not line.strip():
                continue
            cells = [c.strip() for c in line.split(",")]
            idx = column
            if isinstance(column, str):
                if header is None:
                    header = cells
                    if column not in header:
                        raise ValueError(f"no column {column!r} in {path.name}: {header}")
                    continue
                idx = header.index(column)
            try:
                values.append(float(cells[idx]) * scale)
            except (ValueError, IndexError):
                continue  # a header line, or a short one
    if not values:
        raise ValueError(f"no numbers in column {column} of {path}")
    values.sort()

    def pct(p):
        return round(values[min(len(values) - 1, int(p / 100 * len(values)))], 3)
    return {"n": len(values), "mean": round(statistics.fmean(values), 3),
            "p50": pct(50), "p95": pct(95), "p99": pct(99), "max": round(values[-1], 3)}


def run_scenario(path, repeat: int = 1, **kwargs) -> list[dict]:
    sc = load_scenario(path)
    return [Run(sc, **kwargs).execute() for _ in range(max(1, int(repeat)))]


# ── Comparing runs ───────────────────────────────────────────────────


def _load_run(run_dir) -> tuple[Path, dict, dict]:
    d = Path(run_dir).expanduser().resolve()
    try:
        results = json.loads((d / "results.json").read_text())
    except (OSError, ValueError) as exc:
        raise ScenarioError(f"{d} is not a run (no readable results.json: {exc})") from None
    try:
        manifest = json.loads((d / "manifest.json").read_text())
    except (OSError, ValueError):
        manifest = {}
    return d, results, manifest


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        elif isinstance(v, list):
            out[key] = json.dumps(v, sort_keys=True)
        else:
            out[key] = v
    return out


def compare_runs(run_dirs: list, mask=None, tolerance: int | None = None,
                 out_dir=None, masks_from: str | None = None) -> dict:
    """Compare runs of one scenario against the first.

    For each checkpoint: the share of pixels that differ outside the mask
    (each checkpoint's own mask unless `mask` overrides it: a list of names
    from the scenario, zones, or "none"), when it was reached, and a diff
    image. Then the metrics side by side, and what differs between the
    manifests: a GPU, a driver, a scenario edited between two runs.
    """
    if len(run_dirs) < 2:
        raise ScenarioError("compare needs at least two runs")
    runs = [_load_run(r) for r in run_dirs]
    base_dir, base, base_man = runs[0]
    names = {r["scenario"] for _, r, _ in runs}
    if len(names) > 1:
        raise ScenarioError(f"runs of different scenarios: {sorted(names)}")
    sc_dir = Path(base.get("scenario_dir", base_dir))
    named = {}
    sc_path = base_man.get("scenario", {}).get("path")
    if mask not in (None, "none") and sc_path and Path(sc_path).exists():
        named = (yaml.safe_load(Path(sc_path).read_text()) or {}).get("masks") or {}
    tol = tolerance if tolerance is not None else int(base.get("tolerance", 8))
    out = Path(out_dir) if out_dir else base_dir.parent / f"compare-{_now_stamp()}"
    labels = [d.name for d, _, _ in runs]
    report = {"baseline": str(base_dir), "runs": [str(d) for d, _, _ in runs],
              "status": {lbl: r["status"] for lbl, (_, r, _) in zip(labels, runs)},
              "checkpoints": {}, "metrics": {}, "manifest_differences": {}, "out": str(out)}
    for cp, info in base.get("checkpoints", {}).items():
        zones = imaging.resolve_mask(mask, named) if mask is not None else info.get("mask", [])
        row = {"mask": imaging.mask_digest(zones, sc_dir),
               "t": {labels[0]: info["t"]}, "differ": {}, "diff_images": {}}
        for lbl, (d, r, _) in zip(labels[1:], runs[1:]):
            other = r.get("checkpoints", {}).get(cp)
            if not other:
                row["differ"][lbl] = None
                continue
            row["t"][lbl] = other["t"]
            a, b = base_dir / info["path"], d / other["path"]
            cp_tol = tolerance if tolerance is not None else int(info.get("tolerance", tol))
            res = imaging.diff(a, b, zones, cp_tol, sc_dir)
            row["differ"][lbl] = res.get("differ", res.get("error"))
            if "error" not in res:
                img = imaging.diff_image(a, b, out / f"{cp}__{lbl}.png", zones, cp_tol, sc_dir)
                row["diff_images"][lbl] = str(img)
        report["checkpoints"][cp] = row
    metric_names = {m for _, r, _ in runs for m in r.get("metrics", {})}
    for m in sorted(metric_names):
        report["metrics"][m] = {lbl: r.get("metrics", {}).get(m) for lbl, (_, r, _) in zip(labels, runs)}
    flats = [_flatten({k: v for k, v in man.items() if k != "date"}) for _, _, man in runs]
    for key in sorted(set().union(*flats)):
        values = [f.get(key) for f in flats]
        if len(set(map(str, values))) > 1:
            report["manifest_differences"][key] = dict(zip(labels, values))
    out.mkdir(parents=True, exist_ok=True)
    (out / "compare.json").write_text(json.dumps(report, indent=1, default=str))
    return report


def format_report(report: dict) -> str:
    """The comparison as text, for a terminal or an agent."""
    labels = [Path(r).name for r in report["runs"]]
    lines = [f"baseline: {labels[0]}"]
    for lbl, st in report["status"].items():
        if st != "ok":
            lines.append(f"  ! {lbl}: {st}")
    if report["checkpoints"]:
        lines.append("")
        lines.append("checkpoint            " + "".join(f"{lbl[-18:]:>20}" for lbl in labels[1:]))
        for cp, row in report["checkpoints"].items():
            cells = []
            for lbl in labels[1:]:
                v = row["differ"].get(lbl)
                dt = (row["t"].get(lbl, 0) - row["t"][labels[0]]) if lbl in row["t"] else None
                if isinstance(v, float):
                    cells.append(f"{v:7.2%} {dt:+6.2f}s" if dt is not None else f"{v:7.2%}")
                else:
                    cells.append(str(v)[:18])
            lines.append(f"{cp[:20]:<22}" + "".join(f"{c:>20}" for c in cells))
        lines.append("  (pixels differing outside the mask; time reached vs baseline)")
    for m, per in report["metrics"].items():
        lines.append("")
        lines.append(f"metric {m}:")
        for lbl, st in per.items():
            if not st or "error" in st:
                lines.append(f"  {lbl}: {st.get('error') if st else 'missing'}")
            else:
                u = st.get("unit", "")
                lines.append(f"  {lbl}: mean {st['mean']}{u} p95 {st['p95']}{u} "
                             f"p99 {st['p99']}{u} max {st['max']}{u} (n={st['n']})")
    if report["manifest_differences"]:
        lines.append("")
        lines.append("manifests differ:")
        for key, per in report["manifest_differences"].items():
            lines.append(f"  {key}: " + "; ".join(f"{k}={str(v)[:60]}" for k, v in per.items()))
    lines.append("")
    lines.append(f"diff images and compare.json: {report['out']}")
    return "\n".join(lines)


def suggest_masks(run_dirs: list, checkpoint: str | None = None, tolerance: int = 8,
                  cell: int = 16) -> dict:
    """Zones that change between runs at the same checkpoint: mask candidates."""
    runs = [_load_run(r) for r in run_dirs]
    if len(runs) < 2:
        raise ScenarioError("suggesting a mask needs at least two runs of the same scenario")
    cps = list(runs[0][1].get("checkpoints", {}))
    if checkpoint:
        if checkpoint not in cps:
            raise ScenarioError(f"no checkpoint {checkpoint!r}; the runs have {cps}")
        cps = [checkpoint]
    out = {}
    for cp in cps:
        paths = [d / r["checkpoints"][cp]["path"] for d, r, _ in runs if cp in r.get("checkpoints", {})]
        if len(paths) >= 2:
            out[cp] = imaging.varying_rects(paths, tolerance, cell)
    return out
