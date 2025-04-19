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
# Configuration & Globals
# ───────────────────────────────────────────────────────
CAN_CHANNEL = 'can0'
ACTUATOR_IDS = [22, 24, 26]

L = 30.48   # field length (m)
d = 0.9     # look‑back distance (rad)
T = 5       # time horizon (s)

# Signals file globals
heading_array = None
S = None
c = None

# Controller state (unchanged)
index = None
ds1 = ds2 = ds3 = None
xx = None
i1 = i2 = i3 = 0

# GPS state
initial_x = initial_y = None

# Store the most recent GPS values for control logic
latest = {"x": 0.0, "y": 0.0, "vx": 0.0, "vy": 0.0}

# ───────────────────────────────────────────────────────
# CSV LOGGING SETUP
# ───────────────────────────────────────────────────────
csv_path = Path(__file__).parent / "gps_can_log.csv"
csv_headers = [
    "time_sec", "command",
    "x", "y", "vx", "vy",
    "raw_can_id", "raw_can_data",
    "pdo_position_mm_22", "pdo_speed_mms_22",
    "pdo_position_mm_24", "pdo_speed_mms_24",
    "pdo_position_mm_26", "pdo_speed_mms_26",
    "lookahead1", "lookahead2",
]

# Open once and write header
f_csv = open(csv_path, "w", newline="")
writer = csv.DictWriter(f_csv, fieldnames=csv_headers)
writer.writeheader()
f_csv.flush()
start_time = time.time()

def log_row(**kwargs):
    """Write a row; missing fields remain blank."""
    row = {h: "" for h in csv_headers}
    row["time_sec"] = f"{time.time() - start_time:.3f}"
    for k, v in kwargs.items():
        if k not in row:
            continue
        row[k] = f"{v:.3f}" if isinstance(v, float) else str(v)
    writer.writerow(row)
    f_csv.flush()

def update_latest(**kwargs):
    """Update latest GPS values and log them."""
    for k, v in kwargs.items():
        if k in latest:
            latest[k] = v
    log_row(**kwargs)

# ───────────────────────────────────────────────────────
# Load your signals3.txt
# ───────────────────────────────────────────────────────
def load_signal_data():
    global heading_array, S, c
    path = Path(__file__).parent / "signals3.txt"
    raw = np.loadtxt(path)
    data = raw[:, 1:]  # drop index column
    heading_array = data[0, :]
    S = data[1:, :]
    z = S.shape[0] // len(ACTUATOR_IDS)
    c = np.linspace(0, L, z)
    print("‣ signals3.txt loaded:")
    print("  heading_array =", heading_array)
    print("  S shape =", S.shape)
    print("  c =", c)

# ───────────────────────────────────────────────────────
# CAN BUS SETUP & HELPERS
# ───────────────────────────────────────────────────────
def setup_can_bus():
    bus = can.interface.Bus(channel=CAN_CHANNEL, interface='socketcan')
    print(f"✅ CAN bus {CAN_CHANNEL} is up")
    return bus

async def send_message_async(bus, arb_id, data, command=None):
    msg = can.Message(arbitration_id=arb_id, data=data, is_extended_id=False)
    try:
        await asyncio.get_event_loop().run_in_executor(None, bus.send, msg)
        print(f"→ Sent 0x{arb_id:X}: {data} ({command or ''})")
        if command:
            log_row(command=command)
    except Exception as e:
        print("❌ CAN send error:", e)

async def send_initial_can_commands(bus):
    cmds = []
    for node in ACTUATOR_IDS:
        cmds.append((0x600 + node, [0x23,0x16,0x10,0x01,0xC8,0x00,0x01,0x00]))
    for node in ACTUATOR_IDS:
        cmds.append((0x000, [0x01, node]))
    for node in ACTUATOR_IDS:
        cmds.append((0x200 + node, [0x00,0xFB,0xFB,0xFB,0xFB,0xFB,0x00,0x00]))

    print("📤 Initializing actuators...")
    for arb_id, data in cmds:
        await send_message_async(bus, arb_id, data, command="INIT")
        await asyncio.sleep(0.1)
    print("✅ Initialization complete.")

async def send_actuator_command(bus, actuator_id, action):
    arb_id_map = {22:0x222, 24:0x224, 26:0x226}
    arb_id = arb_id_map.get(actuator_id)
    if arb_id is None:
        return
    if action == "open":
        data = [0xE8,0x03,0xFB,0xFB,0xFB,0xFB,0x00,0x00]
    elif action == "close":
        data = [0x02,0xFB,0xFB,0xFB,0xFB,0xFB,0x00,0x00]
    else:
        return
    await send_message_async(bus, arb_id, data, command=action)

# ───────────────────────────────────────────────────────
# —––– Y O U R   O R I G I N A L   C O N T R O L L E R ––––
# ───────────────────────────────────────────────────────
async def controller(bus, a, b, vx, _):
    global index, ds1, ds2, ds3, xx, i1, i2, i3

    if index is None:
        load_signal_data()
        index = int(np.argmin(np.abs(c - b)))
        s1 = S[index*3    , :]
        s2 = S[index*3 + 1, :]
        s3 = S[index*3 + 2, :]
        ds1 = np.insert(np.diff(s1), 0, s1[0])
        ds2 = np.insert(np.diff(s2), 0, s2[0])
        ds3 = np.insert(np.diff(s3), 0, s3[0])
        xx = heading_array.copy() if (index+1)%2==1 else (L - heading_array)
        for aid in ACTUATOR_IDS:
            await send_actuator_command(bus, aid, "close")
        if s1[0]==1: await send_actuator_command(bus, 22, "open")
        if s2[0]==1: await send_actuator_command(bus, 24, "open")
        if s3[0]==1: await send_actuator_command(bus, 26, "open")

    w1 = xx[i1]; w2 = xx[i2]; w3 = xx[i3]
    lookahead1 = a - d + T*0.7
    lookahead2 = a - d

    # ── NEW: capture these values too ──
    log_row(lookahead1=lookahead1, lookahead2=lookahead2)

    if lookahead1 > w1 and ds1[i1]==1:
        await send_actuator_command(bus, 22, "open");  i1 += 1
    elif lookahead2 > w1 and ds1[i1]==-1:
        await send_actuator_command(bus, 22, "close"); i1 += 1

    if lookahead1 > w2 and ds2[i2]==1:
        await send_actuator_command(bus, 24, "open");  i2 += 1
    elif lookahead2 > w2 and ds2[i2]==-1:
        await send_actuator_command(bus, 24, "close"); i2 += 1

    if lookahead1 > w3 and ds3[i3]==1:
        await send_actuator_command(bus, 26, "open");  i3 += 1
    elif lookahead2 > w3 and ds3[i3]==-1:
        await send_actuator_command(bus, 26, "close"); i3 += 1

    if a > xx[-1]:
        for aid in ACTUATOR_IDS:
            await send_actuator_command(bus, aid, "close")

    if i1 >= len(xx) or i2 >= len(xx) or i3 >= len(xx):
        return

# ───────────────────────────────────────────────────────
# PDO Listener (logs each actuator feedback)
# ───────────────────────────────────────────────────────
ID_TO_AID = {0x1A2:22, 0x1A4:24, 0x1A6:26}
async def listen_actuator_pdo(bus):
    while True:
        msg = await asyncio.get_event_loop().run_in_executor(None, bus.recv)
        aid = ID_TO_AID.get(msg.arbitration_id)
        if aid:
            pos_mm  = int.from_bytes(msg.data[0:2], "little") * 0.1
            spd_mms = int.from_bytes(msg.data[5:7], "little") * 0.1
            print(f"[PDO{aid}] pos={pos_mm:.1f}mm spd={spd_mms:.1f}mm/s")
            log_row(**{
                f"pdo_position_mm_{aid}": pos_mm,
                f"pdo_speed_mms_{aid}": spd_mms
            })

# ───────────────────────────────────────────────────────
# Raw‐CAN Logger (every frame on the bus)
# ───────────────────────────────────────────────────────
async def log_all_can(bus):
    while True:
        msg = await asyncio.get_event_loop().run_in_executor(None, bus.recv)
        raw_id = msg.arbitration_id
        raw_data = list(msg.data)
        print(f"[CAN] 0x{raw_id:X}: {raw_data}")
        log_row(raw_can_id=hex(raw_id), raw_can_data=raw_data)

# ───────────────────────────────────────────────────────
# GPS Stream & Controller Caller
# ───────────────────────────────────────────────────────
async def gps_streaming_task(gps_config_path, bus):
    global initial_x, initial_y
    config: EventServiceConfig = proto_from_json_file(gps_config_path, EventServiceConfig())
    async for _, msg in EventClient(config).subscribe(config.subscriptions[0]):

        if isinstance(msg, gps_pb2.RelativePositionFrame):
            x = msg.relative_pose_north
            y = msg.relative_pose_east
            if initial_x is None:
                initial_x, initial_y = x, y
                x_rel, y_rel = 0.0, 0.0
            else:
                x_rel = x - initial_x
                y_rel = y - initial_y
            print(f"[REL_POS] x={x_rel:.3f}, y={y_rel:.3f}")
            update_latest(x=x_rel, y=y_rel)
            continue

        if isinstance(msg, gps_pb2.GpsFrame):
            vx = msg.vel_north
            vy = msg.vel_east
            print(f"[GPS   ] vx={vx:.3f}, vy={vy:.3f}")
            update_latest(vx=vx, vy=vy)

        # feed your controller exactly as before
        b      = latest["y"]
        vx_now = latest["vx"]
        vy_now = latest["vy"]
        a = math.atan2(vx_now, vy_now)

        await controller(bus, a, b, vx_now, None)

# ───────────────────────────────────────────────────────
# Main Entrypoint
# ───────────────────────────────────────────────────────
async def main(gps_cfg):
    bus = setup_can_bus()
    await send_initial_can_commands(bus)
    await asyncio.gather(
        log_all_can(bus),
        listen_actuator_pdo(bus),
        gps_streaming_task(gps_cfg, bus),
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gps-service-config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.gps_service_config))
