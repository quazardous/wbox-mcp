#!/usr/bin/env python3
"""
test_scenario.py — scenarios: a run played again, and runs compared.

Without a box: loading and checking a scenario, masks, pixel diffs, the
CSV metrics. In a box (labwc, pygame, numpy and Pillow): the game dummy
played by a scenario twice, its checkpoints compared with and without the
mask that hides its clock, a mask suggested from the runs, a failed wait
that still cleans up, and the setup files put back.

Run:
    python -m pytest tests/test_scenario.py -v
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from wbox.scenario import (ScenarioError, Run, compare_runs, csv_stats,  # noqa: E402
                           format_report, load_scenario, suggest_masks)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux boxes")

GAME = Path(__file__).parent / "game-dummy" / "game_dummy.py"
HAVE_IMAGING = all(importlib.util.find_spec(m) for m in ("numpy", "PIL"))
needs_imaging = pytest.mark.skipif(not HAVE_IMAGING,
                                   reason='numpy and Pillow: pip install "wbox-mcp[scenario]"')


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False) if not isinstance(data, str) else data)
    return path


# ── Loading ──────────────────────────────────────────────────────────


class TestLoad:
    def base(self, **over):
        sc = {"name": "t", "config": "c.yaml", "steps": [{"key": "a"}]}
        sc.update(over)
        return sc

    def test_minimal_and_shorthands(self, tmp_path):
        sc = load_scenario(write(tmp_path / "s.yaml", self.base(steps=[
            {"key": "a"}, {"press": ["shift", "w"]}, {"pad_script": "0:a"},
            {"checkpoint": "one"}, {"sleep": 0.5}, {"pad_press": "a"},
        ])))
        kinds = [s["kind"] for s in sc["steps"]]
        assert kinds == ["key", "press", "pad_script", "checkpoint", "sleep", "pad_press"]
        assert sc["steps"][0]["args"] == {"shortcut": "a", "hold_ms": None}
        assert sc["steps"][1]["args"] == {"keys": ["shift", "w"], "hold_ms": 50}
        assert sc["steps"][5]["args"]["buttons"] == ["a"]
        assert len(sc["_digest"]) == 12

    @pytest.mark.parametrize("over, words", [
        ({"bogus": 1}, "unknown keys"),
        ({"steps": []}, "non-empty"),
        ({"steps": [{"jump": 1}]}, "unknown action"),
        ({"steps": [{"click": 3}]}, "named arguments"),
        ({"steps": [{"click": {"x": 1}}]}, "needs y"),
        ({"steps": [{"key": {"shortcut": "a", "hold": 3}}]}, "no argument"),
        ({"steps": [{"wait": {"timeout": 3}}]}, "exactly one"),
        ({"steps": [{"checkpoint": "x"}, {"checkpoint": "x"}]}, "twice"),
        ({"steps": [{"checkpoint": "a b"}]}, "checkpoint needs a name"),
        ({"steps": [{"checkpoint": {"name": "x", "mask": "nope"}}]}, "unknown mask"),
        ({"default_mask": ["hud"]}, "unknown mask"),
        ({"masks": {"hud": [{"rect": [0, 0, 1]}]}, "steps": [{"checkpoint": {"name": "x", "mask": "hud"}}]}, None),
        ({"setup": {"copy": {"missing.sav": "/tmp/x"}}}, "does not exist"),
    ])
    def test_errors_are_found_before_running(self, tmp_path, over, words):
        path = write(tmp_path / "s.yaml", self.base(**over))
        if words is None:
            load_scenario(path)  # a bad rect is caught when pixels are compared
            return
        with pytest.raises(ScenarioError, match=words):
            load_scenario(path)


# ── Pixels ───────────────────────────────────────────────────────────


@needs_imaging
class TestImaging:
    def img(self, w=100, h=50, color=(10, 10, 10)):
        import numpy as np
        a = np.zeros((h, w, 3), dtype=np.uint8)
        a[:] = color
        return a

    def test_mask_rects_in_pixels_and_percent(self):
        from wbox.imaging import mask_array
        keep = mask_array([{"rect": [0, 0, 10, 5]}, {"rect": ["90%", "50%", "10%", "50%"]}], 100, 50)
        assert not keep[0:5, 0:10].any() and keep[5:, 10:90].all()
        assert not keep[25:, 90:].any() and keep[:25, 90:].all()

    def test_mask_from_an_image(self, tmp_path):
        from PIL import Image
        from wbox.imaging import mask_array
        m = Image.new("L", (100, 50), 255)
        m.paste(0, (50, 0, 100, 50))
        m.save(tmp_path / "m.png")
        keep = mask_array([{"image": "m.png"}], 100, 50, tmp_path)
        assert keep[:, :50].all() and not keep[:, 50:].any()

    def test_diff_tolerance_and_mask(self):
        from wbox.imaging import diff
        a, b = self.img(), self.img()
        b[0:10, 0:10] = (200, 0, 0)       # a "clock"
        b[40:50, 90:100] = (14, 14, 14)   # dithering noise, under tolerance
        d = diff(a, b, [], tolerance=8)
        assert d["pixels"] == 100 and d["differ"] == pytest.approx(100 / 5000)
        assert diff(a, b, [{"rect": [0, 0, 10, 10]}])["pixels"] == 0
        assert diff(a, b, [], tolerance=2)["pixels"] == 200
        assert "error" in diff(a, self.img(w=99))

    def test_varying_rects_find_the_clock(self):
        from wbox.imaging import varying_rects
        frames = []
        for i in range(3):
            f = self.img(w=200, h=100)
            f[10:30, 150:190] = (i * 80, 0, 0)
            frames.append(f)
        assert varying_rects(frames, cell=16) == [[144, 0, 48, 32]]  # x 150..190 → cells 144..192


def test_csv_stats(tmp_path):
    p = tmp_path / "f.csv"
    p.write_text("frame,seconds,ms\n" + "".join(f"{i},{i/60:.3f},{16 + (i % 10 == 0) * 20}\n"
                                                for i in range(100)))
    by_name = csv_stats(p, "ms")
    by_index = csv_stats(p, 2)
    assert by_name == by_index
    assert by_name["n"] == 100 and by_name["p50"] == 16 and by_name["max"] == 36
    assert by_name["mean"] == pytest.approx(18.0)


# ── In a box ─────────────────────────────────────────────────────────


def _need_box():
    if not shutil.which("labwc"):
        pytest.skip("labwc not installed")
    if importlib.util.find_spec("pygame") is None:
        pytest.skip("pygame not installed (pip install pygame-ce)")
    if not HAVE_IMAGING:
        pytest.skip('numpy and Pillow: pip install "wbox-mcp[scenario]"')


CLOCK = {"rect": [560, 10, 70, 30]}


def make_scenario(tmp: Path, steps: list, **extra) -> Path:
    write(tmp / "box.yaml", {
        "name": f"scenario-test-{tmp.name[-12:]}", "compositor": "labwc", "headless": True,
        "screen": "640x360", "keyboard_layout": "us", "audio": "null",
        "app": {"command": [sys.executable, str(GAME), str(tmp / "state.json")],
                "env": {"GAME_DUMMY_FULLSCREEN": "1"}},
    })
    sc = {"name": "dummy", "config": "box.yaml", "masks": {"clock": [CLOCK]},
          "default_mask": ["clock"], "steps": steps}
    sc.update(extra)
    return write(tmp / "dummy.yaml", sc)


STEPS = [
    {"wait": {"log": "game ready", "timeout": 20}},
    {"wait": {"stable": 0.5, "timeout": 10}},
    {"checkpoint": "start"},
    {"key": {"shortcut": "a", "hold_ms": 80}},
    {"wait": {"log": "toggled 1", "timeout": 5}},
    {"wait": {"stable": 0.5, "timeout": 10}},
    {"checkpoint": "blue"},
]


@pytest.fixture(scope="module")
def two_runs(tmp_path_factory):
    _need_box()
    tmp = tmp_path_factory.mktemp("sc")
    sc = load_scenario(make_scenario(tmp, STEPS))
    runs = [Run(sc, log=lambda *_: None).execute() for _ in range(2)]
    return tmp, runs


class TestRuns:
    def test_runs_reach_every_checkpoint(self, two_runs):
        _, runs = two_runs
        for r in runs:
            assert r["status"] == "ok", r["reason"]
            assert list(r["checkpoints"]) == ["start", "blue"]
            d = Path(r["run"])
            assert (d / "checkpoints" / "start.png").exists()
            assert (d / "app.log").read_text().count("game ready") == 1
            man = json.loads((d / "manifest.json").read_text())
            assert man["scenario"]["name"] == "dummy" and man["masks"]["clock"]
            assert man["host"]["kernel"]

    def test_same_checkpoints_match_with_the_clock_masked(self, two_runs):
        _, runs = two_runs
        rep = compare_runs([r["run"] for r in runs])
        label = Path(runs[1]["run"]).name
        for cp in ("start", "blue"):
            assert rep["checkpoints"][cp]["differ"][label] == 0.0, cp
            assert Path(rep["checkpoints"][cp]["diff_images"][label]).exists()
        assert "dummy" in format_report(rep) or "start" in format_report(rep)

    def test_without_the_mask_the_clock_shows(self, two_runs):
        _, runs = two_runs
        rep = compare_runs([r["run"] for r in runs], mask="none")
        label = Path(runs[1]["run"]).name
        assert rep["checkpoints"]["start"]["differ"][label] > 0

    def test_the_toggle_is_seen_between_checkpoints(self, two_runs):
        from wbox.imaging import diff
        _, runs = two_runs
        d = Path(runs[0]["run"]) / "checkpoints"
        assert diff(d / "start.png", d / "blue.png", [CLOCK])["differ"] > 0.9

    def test_suggested_mask_covers_the_clock(self, two_runs):
        _, runs = two_runs
        rects = suggest_masks([r["run"] for r in runs], "start")["start"]
        assert len(rects) == 1
        x, y, w, h = rects[0]
        assert x <= 560 and y <= 10 and x + w >= 630 and y + h >= 40
        assert w * h < 640 * 360 * 0.05


def test_a_wait_that_cannot_succeed_fails_cleanly(tmp_path):
    _need_box()
    # Without the mask the clock never holds still
    sc = load_scenario(make_scenario(tmp_path, [
        {"wait": {"stable": 0.5, "timeout": 2, "mask": "none"}},
        {"checkpoint": "never"},
    ]))
    r = Run(sc, log=lambda *_: None).execute()
    assert r["status"] == "failed" and "hold still" in r["reason"]
    assert r["checkpoints"] == {}
    assert (Path(r["run"]) / "failure.png").exists()
    assert r["steps"][-1]["error"]


def test_setup_files_are_in_place_during_the_run_and_restored(tmp_path):
    _need_box()
    write(tmp_path / "start.sav", "scenario save")
    target = tmp_path / "home" / "save.sav"
    write(target, "the player's own save")
    sc = load_scenario(make_scenario(tmp_path, [{"wait": {"log": "game ready", "timeout": 20}}],
                                     setup={"copy": {"start.sav": str(target)}},
                                     collect=[str(target)]))
    r = Run(sc, log=lambda *_: None).execute()
    assert r["status"] == "ok", r["reason"]
    assert (Path(r["run"]) / "collected" / "save.sav").read_text() == "scenario save"
    assert target.read_text() == "the player's own save"
    assert not (Path(r["run"]) / "_backup").exists()
