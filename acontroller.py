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

# ───────────────────────────────────────────────────────
# Configuration
# ───────────────────────────────────────────────────────
CAN_CHANNEL = 'can0'
ACTUATOR_IDS = [22, 24, 26]
# for controller geometry/time thresholds
L = 30.48
d = 0.9
T = 5

# signals.txt: first row heading array; next rows S (n × m)
data = np.loadtxt("signals.txt")
heading_array = data[0, :]
S = data[1:, :]
n, m = S.shape
assert n % len(ACTUATOR_IDS) == 0, "signals.txt row‐count must be a multiple of #actuators"
z = n // len(ACTUATOR_IDS)
c = [i * L / z for i in range(z)]

# CSV setup
csv_file = Path(__file__).parent / "gps_can_log.csv"
csv_headers = [
    "time_sec", "x", "y", "vx", "vy", "command",
    "pdo_position_mm_22", "pdo_speed_mms_22",
    "pdo_position_mm_24", "pdo_speed_mms_24",
    "pdo_position_mm_26", "pdo_speed_mms_26",
]
# cache latest values for filling out rows
latest = {h: "" for h in csv_headers if h != "time_sec"}
start_time = time.time()

def log_event(**kwargs):
    """Update latest with any kwargs, then write a full row to CSV."""
    now = time.time()
    row = {"time_sec": f"{now - start_time:.3f}"}
    # update cache
    for k, v in kwargs.items():
        # format floats to 3 decimals
        latest[k] = f"{v:.3f}" if isinstance(v, (int, float)) else str(v)
    # merge and write
    row.update(latest)
    with open(csv_file, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_headers)
        writer.writerow(row)

# ───────────────────────────────────────────────────────
# CAN helpers
# ───────────────────────────────────────────────────────
def setup_can_bus():
    return can.interface.Bus(channel=CAN_CHANNEL, interface='socketcan')

async def send_message_async(bus, arb_id, data, command=None):
    msg = can.Message(arbitration_id=arb_id, data=data, is_extended_id=False)
    try:
        await asyncio.get_event_loop().run_in_executor(None, bus.send, msg)
        print(f"✅ Sent CAN 0x{arb_id:X}: {[hex(b) for b in data]}")
        if command:
            log_event(command=command)
    except can.CanError as e:
        print(f"❌ CAN error: {e}")

async def send_initial_can_commands(bus):
    """SDO, NMT, and clear‐error startup for each actuator."""
    cmds = []
    # SDO config (same for all)
    for node in ACTUATOR_IDS:
        cmds.append((0x600 + node, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]))
    # NMT start nodes
    for node in ACTUATOR_IDS:
        cmds.append((0x000, [0x01, node]))
    # clear errors (use each actuator's PDO ID)
    for node in ACTUATOR_IDS:
        cmds.append((0x200 + node, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]))

    print("📤 Initializing actuators...")
    for arb_id, data in cmds:
        await send_message_async(bus, arb_id, data, command="INIT")
        await asyncio.sleep(0.1)
    print("✅ Initialization complete.")

async def send_actuator_command(bus, actuator_id, action):
    """Open/close via the correct COB‐ID for that node."""
    arb_id = {22: 0x222, 24: 0x224, 26: 0x226}.get(actuator_id)
    if arb_id is None:
        return
    if action == "open":
        data = [0xE8, 0x03, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]
    else:
        data = [0x02, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]
    await send_message_async(bus, arb_id, data, command=action)

# ───────────────────────────────────────────────────────
# Controller logic (from Controllerm.py)
# ───────────────────────────────────────────────────────
_index = None
_sigs = None  # tuple of (s1,s2,s3)
_xx = None
_step = 0

async def controller(bus, heading, lateral_y, vx, lateral_x):
    global _index, _sigs, _xx, _step

    # on first call, pick which segment to use
    if _index is None:
        # find nearest c[j] to lateral_y
        _index = min(range(len(c)), key=lambda j: abs(c[j] - lateral_y))
        s1 = S[_index * 3 + 0]
        s2 = S[_index * 3 + 1]
        s3 = S[_index * 3 + 2]
        _sigs = (s1, s2, s3)
        _xx = heading_array if (_index % 2) else (L - heading_array)
        # send initial positions
        for aid, sig in zip(ACTUATOR_IDS, _sigs):
            await send_actuator_command(bus, aid, "open" if sig[0] == 1 else "close")

    if _step >= len(_xx):
        return

    w = _xx[_step]
    if heading - d + T * vx > w:
        for aid, sig in zip(ACTUATOR_IDS, _sigs):
            act = "open" if sig[_step] == 1 else "close"
            await send_actuator_command(bus, aid, act)
        _step += 1

    # once we pass the last threshold, close all
    if heading > _xx[-1]:
        for aid in ACTUATOR_IDS:
            await send_actuator_command(bus, aid, "close")

# ───────────────────────────────────────────────────────
# GPS handling
# ───────────────────────────────────────────────────────
initial_x = initial_y = None

async def gps_streaming_task(gps_config_path, bus):
    global initial_x, initial_y
    config: EventServiceConfig = proto_from_json_file(gps_config_path, EventServiceConfig())
    async for _, msg in EventClient(config).subscribe(config.subscriptions[0]):
        if isinstance(msg, gps_pb2.RelativePositionFrame):
            x = msg.relative_pose_north
            y = msg.relative_pose_east
            if initial_x is None:
                initial_x, initial_y = x, y
                # treat first frame as (0,0)
                log_event(x=0.0, y=0.0)
            else:
                log_event(x=x - initial_x, y=y - initial_y)
            continue

        if isinstance(msg, gps_pb2.GpsFrame):
            vx = msg.vel_north
            vy = msg.vel_east
            log_event(vx=vx, vy=vy)

        # then drive controller
        # note: lateral_y = current y, heading = atan2(vy, vx)
        cy = float(latest["y"])
        hv = float(latest["vx"])
        hh = math.atan2(hv, float(latest["vy"]))
        await controller(bus, hh, cy, hv, float(latest["x"]))

# ───────────────────────────────────────────────────────
# PDO listener (three IDs)
# ───────────────────────────────────────────────────────
ID_TO_AID = {0x1A2: 22, 0x1A4: 24, 0x1A6: 26}

async def listen_actuator_pdo(bus):
    while True:
        msg = await asyncio.get_event_loop().run_in_executor(None, bus.recv)
        aid = ID_TO_AID.get(msg.arbitration_id)
        if aid:
            # position in bytes 0–1, speed in bytes 5–6
            pos_raw = int.from_bytes(msg.data[0:2], "little")
            spd_raw = int.from_bytes(msg.data[5:7], "little")
            pos_mm = pos_raw * 0.1
            spd_mms = spd_raw * 0.1
            log_event(**{
                f"pdo_position_mm_{aid}": pos_mm,
                f"pdo_speed_mms_{aid}": spd_mms
            })

# ───────────────────────────────────────────────────────
# Entry point
# ───────────────────────────────────────────────────────
async def main(gps_config_path):
    # prepare CSV
    if not csv_file.exists():
        with open(csv_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=csv_headers)
            writer.writeheader()

    bus = setup_can_bus()
    await send_initial_can_commands(bus)

    # run GPS + PDO listener in parallel
    await asyncio.gather(
        gps_streaming_task(gps_config_path, bus),
        listen_actuator_pdo(bus),
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--gps-service-config",
        dest="gps_config_path",
        type=Path,
        required=True,
        help="Path to your GPS EventServiceConfig JSON"
    )
    args = parser.parse_args()
    asyncio.run(main(args.gps_config_path))
