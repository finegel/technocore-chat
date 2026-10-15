"""Run: uv run --group dev python -m pytest tests

`bench/rooms.py --writers N` reports a contended arm with `writers: N` in its meta, so the
writers it starts have to be N, and each of them has to churn a deep room the store already
holds. Before this, `contended()` clamped both away: `deep == 0` still started one writer, on
`bench-d000000`, a room `build()` never wrote (so the arm created a room the report does not
count), and `writers > deep` started only `deep` threads while the meta kept `writers`.

Probed in a fresh interpreter, the way tests/unit/test_config_knobs.py checks a knob:
`bench/rooms.py` is a script, not a package — it sets its own environment and inserts `src`
on the path at import — so importing it in-process would leave that behind for the next test.
"""

import functools
import json
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[2] / "bench" / "rooms.py"

# %r is the script's path. Each scenario builds its own store in its own tempdir, so the
# rooms counted afterwards are that scenario's and nobody else's.
PROBE = """
import importlib.util, json, pathlib, sys, tempfile

spec = importlib.util.spec_from_file_location("bench_rooms", %r)
bench = importlib.util.module_from_spec(spec)
# Registered before it runs: the script's own `@dataclass` resolves its annotations
# through sys.modules[cls.__module__].
sys.modules["bench_rooms"] = bench
spec.loader.exec_module(bench)


def names(root):
    # rooms/<shard>/<name>.jsonl — walk rather than glob one level, the shard is a level.
    return sorted(p.stem for p in (root / "rooms").rglob("bench-*.jsonl"))


out = {}
for label, (rooms, deep, writers) in {
    "no_deep_room": (3, 0, 1),
    "more_writers_than_deep": (3, 2, 5),
    "one_writer_per_deep_room": (3, 2, 2),
}.items():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        bench.build(root, rooms, deep, 1)
        before = names(root)
        try:
            arm = bench.contended(root, 1, 1, writers, deep)
        except ValueError as exc:
            out[label] = {"refused": str(exc)}
        else:
            out[label] = {"refused": None, "median": arm["median"] >= 0}
        out[label]["rooms_added"] = sorted(set(names(root)) - set(before))
print(json.dumps(out))
"""


@functools.cache
def probe() -> dict:
    """What `contended` did for each scenario above, in one fresh interpreter."""
    run = subprocess.run([sys.executable, "-c", PROBE % str(BENCH)], capture_output=True, text=True)
    assert run.returncode == 0, f"probe failed: {run.stderr}"
    return json.loads(run.stdout)


def test_a_store_with_no_deep_room_is_refused_and_gains_no_room() -> None:
    """`deep == 0` leaves no room for a writer to churn. Clamped to one writer, it wrote
    `bench-d000000` into existence: one room more than the report says it measured."""
    result = probe()["no_deep_room"]
    assert result["refused"] is not None, "deep=0 ran a contended arm anyway"
    assert "writers (1)" in result["refused"] and "deep (0)" in result["refused"]
    assert result["rooms_added"] == []


def test_more_writers_than_deep_rooms_is_refused_not_capped() -> None:
    """Capping to `deep` threads ran 2 writers under a report that says 5."""
    result = probe()["more_writers_than_deep"]
    assert result["refused"] is not None, "writers=5 with deep=2 ran with fewer writers"
    assert "writers (5)" in result["refused"] and "deep (2)" in result["refused"]


def test_one_writer_per_deep_room_still_runs_and_creates_nothing() -> None:
    """The boundary the check allows: every writer has its own deep room, so the arm runs
    and every write lands in a room `build()` already made."""
    result = probe()["one_writer_per_deep_room"]
    assert result["refused"] is None
    assert result["median"] is True
    assert result["rooms_added"] == []


def test_the_cli_refuses_before_it_pays_for_the_setup() -> None:
    """`main` refuses the pair itself, before `build`, rather than letting `contended`
    raise after the whole store has been written."""
    run = subprocess.run(
        [sys.executable, str(BENCH), "--rooms", "3", "--deep", "0", "--writers", "1"],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 2, run.stderr
    assert "--writers 1" in run.stderr and "--deep 0" in run.stderr
