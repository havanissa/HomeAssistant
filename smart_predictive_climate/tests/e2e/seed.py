"""Seed the e2e App's data folder with a simulated history (never a production instance).

A fresh disposable Home Assistant has only minutes of Recorder history, so the App's own observation store is
pre-filled with a synthetic room (``thermal_sim``, known truth) ending now, under the real entity ids of the
disposable instance, and ``progress.json`` says those days were read. The App then reads only the remaining
minutes from the real Recorder (the real ``history/history_during_period`` path), learns and publishes.

    python seed.py <data_dir> [--days 21]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "backend")]

from thermal_sim import Scenario, Sim, Truth  # noqa: E402

MAP = {"sensor.kamar_temperature": "sensor.kamar_temperature", "climate.kamar_trv": "climate.heatpump",
       "sensor.outdoor_temperature": "sensor.outdoor_temperature", "binary_sensor.kamar_window": "binary_sensor.kamar_window"}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("data_dir", type=Path)
    p.add_argument("--days", type=float, default=21.0)
    args = p.parse_args()
    end = time.time() - 300
    start = end - args.days * 86400
    sim = Sim(Scenario(start=start, days=args.days, seed=11, truth=Truth(), windows_open_per_day=0.3)).run()
    sim.records = [{**r, "e": MAP[r["e"]]} for r in sim.records if r["e"] in MAP]
    obs = args.data_dir / "history" / "obs"
    state = sim.write(obs)
    progress = {e: {"read_from": start, "read_to": end, "limit": start, "live_from": end} for e in state["coverage"]}
    (args.data_dir / "history" / "progress.json").write_text(json.dumps(progress), encoding="utf-8")
    last = {}
    for r in sim.records:
        last[r["e"]] = r
    print(json.dumps({"records": len(sim.records), "entities": sorted(state["coverage"]), "start": start, "end": end,
                      "last_room_c": last["sensor.kamar_temperature"]["v"],
                      "last_outdoor_c": last["sensor.outdoor_temperature"]["v"]}))


if __name__ == "__main__":
    main()
