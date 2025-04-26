#!/usr/bin/env python3
import asyncio
import time
import csv
from pathlib import Path
import argparse

from farm_ng.core.event_client import EventClient
from farm_ng.core.events_file_reader import proto_from_json_file
from farm_ng.core.event_service_pb2 import EventServiceConfig
from farm_ng.gps import gps_pb2

# ─── Globals ────────────────────────────────────────────────────
initial_x = initial_y = None
latest = {"x": 0.0, "y": 0.0}

# ─── CSV Setup ─────────────────────────────────────────────────
csv_path = Path(__file__).parent / "gps_log.csv"
csv_headers = ["time_sec", "x", "y"]

f_csv = open(csv_path, "w", newline="")
writer = csv.DictWriter(f_csv, fieldnames=csv_headers)
writer.writeheader()
f_csv.flush()
start_time = time.time()

# ─── Helper Functions ───────────────────────────────────────────
def log_row():
    row = {
        "time_sec": f"{time.time() - start_time:.3f}",
        "x": f"{latest['x']:.3f}",
        "y": f"{latest['y']:.3f}"
    }
    writer.writerow(row)
    f_csv.flush()

# ─── GPS Loop ───────────────────────────────────────────────────
async def gps_stream(gps_cfg):
    global initial_x, initial_y
    cfg = proto_from_json_file(gps_cfg, EventServiceConfig())
    async for _, msg in EventClient(cfg).subscribe(cfg.subscriptions[0]):
        if isinstance(msg, gps_pb2.RelativePositionFrame):
            x, y = msg.relative_pose_north, msg.relative_pose_east
            if initial_x is None:
                initial_x, initial_y = x, y
            rel_x, rel_y = x - initial_x, y - initial_y
            latest["x"] = rel_x
            latest["y"] = rel_y
            log_row()

# ─── Main ──────────────────────────────────────────────────────
async def main(gps_cfg):
    await gps_stream(gps_cfg)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--gps-service-config", type=Path, required=True)
    args = p.parse_args()
    asyncio.run(main(args.gps_service_config))
