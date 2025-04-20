#!/usr/bin/env python3
import can
import asyncio
import math
import time
import csv
import numpy as np
from pathlib import Path
import argparse

from farm_ng.core.event_client import EventClient
from farm_ng.core.events_file_reader import proto_from_json_file
from farm_ng.core.event_service_pb2 import EventServiceConfig
from farm_ng.gps import gps_pb2

# ─── Globals ────────────────────────────────────────────────────
CAN_CHANNEL = 'can0'
ACTUATOR_IDS = [22, 24, 26]
TRIGGER_Y = []
SIGNALS = {}
trigger_indices = {"22": 0, "24": 0, "26": 0}

initial_x = initial_y = None
latest = {"x": 0.0, "y": 0.0, "vx": 0.0, "vy": 0.0}

# ─── CSV Setup ─────────────────────────────────────────────────
csv_path = Path(__file__).parent / "gps_can_log.csv"
csv_headers = ["time_sec", "command", "y", "vx", "vy"]
f_csv = open(csv_path, "w", newline="")
writer = csv.DictWriter(f_csv, fieldnames=csv_headers)
writer.writeheader()
f_csv.flush()
start_time = time.time()

def log_row(**kwargs):
    row = {h: "" for h in csv_headers}
    row["time_sec"] = f"{time.time() - start_time:.3f}"
    for k, v in kwargs.items():
        if k not in row:
            continue
        if isinstance(v, float):
            row[k] = f"{v:.3f}"
        else:
            row[k] = str(v)
    writer.writerow(row)
    f_csv.flush()

def update_latest(**kwargs):
    for k, v in kwargs.items():
        if k in latest:
            latest[k] = v

# ─── Load signal data ───────────────────────────────────────────
def load_signal_data():
    global TRIGGER_Y, SIGNALS
    path = Path(__file__).parent / "signals3.txt"
    raw = np.loadtxt(path)
    TRIGGER_Y = raw[0, :]
    SIGNALS = {
        22: raw[1, :].astype(int),
        24: raw[2, :].astype(int),
        26: raw[3, :].astype(int)
    }
    print("✅ Loaded signal triggers")

# ─── CAN Helpers ────────────────────────────────────────────────
def setup_can_bus():
    return can.interface.Bus(channel=CAN_CHANNEL, interface='socketcan')

async def send_message(bus, arb_id, data, command=None):
    msg = can.Message(arbitration_id=arb_id, data=bytes(data), is_extended_id=False)
    try:
        await asyncio.get_event_loop().run_in_executor(None, bus.send, msg)
        print(f"→ Sent 0x{arb_id:X}: {data} ({command})")
        if command:
            log_row(command=command, y=latest["y"], vx=latest["vx"], vy=latest["vy"])
    except Exception as e:
        print("CAN send error:", e)

async def send_initial(bus):
    sdo_commands = [
        (0x622, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
        (0x624, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
        (0x626, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00])
    ]
    for arb_id, data in sdo_commands:
        await send_message(bus, arb_id, data, "SDO_COMMAND")
        await asyncio.sleep(0.2)

    for node_id in ACTUATOR_IDS:
        await send_message(bus, 0x000, [0x01, node_id], f"NMT_START_{node_id}")
        await asyncio.sleep(0.1)

    clear_errors = [
        (0x222, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x224, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x226, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00])
    ]
    for arb_id, data in clear_errors:
        await send_message(bus, arb_id, data, "CLEAR_ERROR")
        await asyncio.sleep(0.2)

    print("✅ Init complete")

async def send_actuator_command(bus, actuator_id, action):
    arb_map = {22: 0x222, 24: 0x224, 26: 0x226}
    arb = arb_map.get(actuator_id)
    if arb is None:
        return
    if action == "open":
        cmd = [0xE8, 0x03, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]
    elif action == "close":
        cmd = [0x02, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]
    else:
        return
    await send_message(bus, arb, cmd, f"{action.upper()}_{actuator_id}")
    await asyncio.sleep(0.2)

# ─── Controller ────────────────────────────────────────────────
async def controller(bus):
    abs_y = abs(latest["y"])

    for actuator_id in ACTUATOR_IDS:
        i = trigger_indices[str(actuator_id)]
        if i >= len(TRIGGER_Y):
            continue
        trigger_y = TRIGGER_Y[i]
        if abs_y >= trigger_y:
            signal = SIGNALS[actuator_id][i]
            action = "open" if signal == 1 else "close"
            await send_actuator_command(bus, actuator_id, action)
            trigger_indices[str(actuator_id)] += 1

# ─── GPS Loop ──────────────────────────────────────────────────
async def gps_stream(gps_cfg, bus):
    global initial_x, initial_y
    cfg = proto_from_json_file(gps_cfg, EventServiceConfig())
    async for _, msg in EventClient(cfg).subscribe(cfg.subscriptions[0]):
        if isinstance(msg, gps_pb2.RelativePositionFrame):
            x, y = msg.relative_pose_north, msg.relative_pose_east
            if initial_x is None:
                initial_x, initial_y = x, y
            rel_x, rel_y = x - initial_x, y - initial_y
            update_latest(x=rel_x, y=rel_y)
        elif isinstance(msg, gps_pb2.GpsFrame):
            vx, vy = msg.vel_north, msg.vel_east
            update_latest(vx=vx, vy=vy)

        await controller(bus)

# ─── Main ──────────────────────────────────────────────────────
async def main(gps_cfg):
    bus = setup_can_bus()
    load_signal_data()
    await send_initial(bus)
    await gps_stream(gps_cfg, bus)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--gps-service-config", type=Path, required=True)
    args = p.parse_args()
    asyncio.run(main(args.gps_service_config))
