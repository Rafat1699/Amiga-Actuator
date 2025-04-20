#!/usr/bin/env python3
import can
import asyncio
import math
import time
import csv
from pathlib import Path
import argparse
from farm_ng.core.event_client import EventClient
from farm_ng.core.event_service_pb2 import EventServiceConfig
from farm_ng.core.events_file_reader import proto_from_json_file
from farm_ng.gps import gps_pb2

# ─── Globals ────────────────────────────────────────────────────
CAN_CHANNEL = 'can0'
ACTUATOR_IDS = [22, 24, 26]
initial_x = initial_y = None
latest = {"x": 0.0, "y": 0.0, "vx": 0.0, "vy": 0.0}
opened = {22: False, 24: False, 26: False}

# ─── CSV Logging ────────────────────────────────────────────────
csv_path = Path(__file__).parent / "gps_can_log.csv"
csv_headers = ["time_sec", "command", "x", "y", "vx", "vy"]
f_csv = open(csv_path, "w", newline="")
writer = csv.DictWriter(f_csv, fieldnames=csv_headers)
writer.writeheader()
f_csv.flush()
start_time = time.time()

def log_row(command="", **kwargs):
    row = {h: "" for h in csv_headers}
    row["time_sec"] = f"{time.time() - start_time:.3f}"
    row["command"] = command
    for k in ["x", "y", "vx", "vy"]:
        if k in kwargs:
            row[k] = f"{kwargs[k]:.3f}"
    writer.writerow(row)
    f_csv.flush()

def update_latest(**kwargs):
    for k, v in kwargs.items():
        if k in latest:
            latest[k] = v

# ─── CAN Bus Setup ──────────────────────────────────────────────
def setup_can_bus():
    bus = can.interface.Bus(channel=CAN_CHANNEL, interface='socketcan')
    print(f"CAN on {CAN_CHANNEL}")
    return bus

async def send_message(bus, arb_id, data, command=None):
    msg = can.Message(arbitration_id=arb_id, data=bytes(data), is_extended_id=False)
    try:
        await asyncio.get_event_loop().run_in_executor(None, bus.send, msg)
        print(f"→ Sent 0x{arb_id:X}: {data} ({command})")
        if command:
            log_row(command, **latest)
    except Exception as e:
        print("CAN send error:", e)

async def send_initial(bus):
    sdo_commands = [
        (0x622, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
        (0x624, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
        (0x626, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
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
        (0x226, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
    ]
    for arb_id, data in clear_errors:
        await send_message(bus, arb_id, data, "CLEAR_ERROR")
        await asyncio.sleep(0.2)

    print("✅ Init complete")

async def send_actuator_command(bus, actuator_id):
    arb_map = {22: 0x222, 24: 0x224, 26: 0x226}
    cmd = [0xE8, 0x03, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]
    await send_message(bus, arb_map[actuator_id], cmd, f"OPEN_{actuator_id}")
    await asyncio.sleep(0.5)

# ─── Controller ────────────────────────────────────────────────
async def controller(bus):
    y = latest["y"]
    abs_y = abs(y)

    if abs_y > 2.0 and not opened[22]:
        await send_actuator_command(bus, 22)
        opened[22] = True
    if abs_y > 3.0 and not opened[24]:
        await send_actuator_command(bus, 24)
        opened[24] = True
    if abs_y > 5.0 and not opened[26]:
        await send_actuator_command(bus, 26)
        opened[26] = True

# ─── GPS Listener ──────────────────────────────────────────────
async def gps_stream(gps_cfg, bus):
    global initial_x, initial_y
    cfg = proto_from_json_file(gps_cfg, EventServiceConfig())
    async for _, msg in EventClient(cfg).subscribe(cfg.subscriptions[0]):
        if isinstance(msg, gps_pb2.RelativePositionFrame):
            x, y = msg.relative_pose_north, msg.relative_pose_east
            if initial_x is None:
                initial_x, initial_y = x, y
            rel_x, rel_y = x - initial_x, y - initial_y
            print(f"[REL] {rel_x:.3f}, {rel_y:.3f}")
            update_latest(x=rel_x, y=rel_y)
        elif isinstance(msg, gps_pb2.GpsFrame):
            vx, vy = msg.vel_north, msg.vel_east
            print(f"[VEL] {vx:.3f}, {vy:.3f}")
            update_latest(vx=vx, vy=vy)

        await controller(bus)

# ─── Main ──────────────────────────────────────────────────────
async def main(gps_cfg):
    bus = setup_can_bus()
    await send_initial(bus)
    await gps_stream(gps_cfg, bus)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gps-service-config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.gps_service_config))
