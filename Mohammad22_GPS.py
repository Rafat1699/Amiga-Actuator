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
csv_path = Path(__file__).parent / "gps_log.csv"
csv_headers = ["time_sec", "x", "y"]
f_csv = open(csv_path, "w", newline="")
writer = csv.DictWriter(f_csv, fieldnames=csv_headers)
writer.writeheader()
f_csv.flush()
start_time = time.time()

initial_x = initial_y = None

# ─── Functions ──────────────────────────────────────────────────
def log_row(x, y):
    elapsed = time.time() - start_time
    row = {
        "time_sec": f"{elapsed:.3f}",
        "x": f"{x:.3f}",
        "y": f"{y:.3f}"
    }
    writer.writerow(row)
    f_csv.flush()

async def gps_stream(gps_cfg):
    global initial_x, initial_y
    cfg = proto_from_json_file(gps_cfg, EventServiceConfig())
    async for _, msg in EventClient(cfg).subscribe(cfg.subscriptions[0]):
        if isinstance(msg, gps_pb2.RelativePositionFrame):
            x, y = msg.relative_pose_north, msg.relative_pose_east
            if initial_x is None:
                initial_x, initial_y = x, y
            rel_x, rel_y = x - initial_x, y - initial_y

            # Print to terminal
            print(f"📡 Position - X: {rel_x:.3f} m, Y: {rel_y:.3f} m")

            # Save to CSV
            log_row(rel_x, rel_y)

# ─── Main ───────────────────────────────────────────────────────
async def main(gps_cfg):
    await gps_stream(gps_cfg)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--gps-service-config", type=Path, required=True)
    args = p.parse_args()
    asyncio.run(main(args.gps_service_config))
