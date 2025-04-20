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
CAN_CHANNEL   = 'can0'
ACTUATOR_IDS  = [22, 24, 26]

L = 30.48   # field length (m)
d = 0.9     # look‑back distance (rad)
T = 5       # time horizon (s)

# Signals & controller state
heading_array = None
c             = None
s1 = s2 = s3  = None
ds1 = ds2 = ds3 = None
xx            = None

index = None
i1 = i2 = i3 = 0

# GPS reference
initial_x = initial_y = None
latest = {"x": 0.0, "y": 0.0, "vx": 0.0, "vy": 0.0}

# ───────────────────────────────────────────────────────
# CSV LOGGING SETUP
# ───────────────────────────────────────────────────────
csv_path = Path(__file__).parent / "gps_can_log.csv"
csv_headers = [
    "time_sec","command",
    "a","b",
    "s1","s2","s3",
    "ds1","ds2","ds3",
    "w1","w2","w3",
    "i1","i2","i3",
    "lookahead1","lookahead2",
    "x","y","vx","vy",
]
f_csv = open(csv_path, "w", newline="")
writer = csv.DictWriter(f_csv, fieldnames=csv_headers)
writer.writeheader()
f_csv.flush()
start_time = time.time()

def log_row(**kwargs):
    row = {h: "" for h in csv_headers}
    row["time_sec"] = f"{time.time() - start_time:.3f}"
    for k, v in kwargs.items():
        if k not in row: continue
        if isinstance(v, (list, np.ndarray)):
            row[k] = repr(list(v))
        elif isinstance(v, float):
            row[k] = f"{v:.3f}"
        else:
            row[k] = str(v)
    writer.writerow(row)
    f_csv.flush()

def update_latest(**kwargs):
    for k, v in kwargs.items():
        if k in latest:
            latest[k] = v

# ───────────────────────────────────────────────────────
# Load signals3.txt (including first column)
# ───────────────────────────────────────────────────────
def load_signal_data():
    global heading_array, c, s1, s2, s3, ds1, ds2, ds3
    path = Path(__file__).parent / "signals3.txt"
    raw  = np.loadtxt(path)
    heading_array = raw[0, :]
    c             = heading_array.copy()
    signals       = raw[1:, :].astype(int)
    s1, s2, s3    = signals
    ds1 = np.insert(np.diff(s1), 0, s1[0])
    ds2 = np.insert(np.diff(s2), 0, s2[0])
    ds3 = np.insert(np.diff(s3), 0, s3[0])
    print("signals3.txt loaded:", heading_array, signals.shape)

# ───────────────────────────────────────────────────────
# CAN setup & send helpers
# ───────────────────────────────────────────────────────
def setup_can_bus():
    bus = can.interface.Bus(channel=CAN_CHANNEL, interface='socketcan')
    print(f"CAN bus {CAN_CHANNEL} up")
    return bus

async def send_message(bus, arb_id, data, command=None):
    msg = can.Message(arbitration_id=arb_id, data=bytes(data), is_extended_id=False)
    try:
        await asyncio.get_event_loop().run_in_executor(None, bus.send, msg)
        print(f"→ Sent 0x{arb_id:X}: {data} ({command})")
        if command:
            log_row(command=command)
    except Exception as e:
        print("CAN send error:", e)

# ───────────────────────────────────────────────────────
# Initialization: NMT, SDO setup, then original init
# ───────────────────────────────────────────────────────
async def send_initial(bus):
    # 1) NMT: bring all nodes to OPERATIONAL
    await send_message(bus, 0x000, [0x01, 0x00], "NMT_START_ALL")
    await asyncio.sleep(0.1)

    # 2) For each actuator node: set mode & enable operation
    for node in ACTUATOR_IDS:
        # a) set Profile Position mode (0x01)
        mode = 0x01
        sdo_mode = [0x2F, 0x60, 0x60, 0x00, mode, 0x00, 0x00, 0x00]
        await send_message(bus, 0x600 + node, sdo_mode, f"SDO_SET_MODE_{node}")
        await asyncio.sleep(0.05)
        # b) enable drive (Control Word = 0x0F)
        cw = 0x0F
        sdo_cw = [0x2B, 0x40, 0x60, 0x00, cw, 0x00, 0x00, 0x00]
        await send_message(bus, 0x600 + node, sdo_cw, f"SDO_ENABLE_OP_{node}")
        await asyncio.sleep(0.05)

    # 3) Your original init sequence
    cmds = []
    for n in ACTUATOR_IDS:
        cmds.append((0x600 + n, [0x23,0x16,0x10,0x01,0xC8,0x00,0x01,0x00]))
    for n in ACTUATOR_IDS:
        cmds.append((0x000, [0x01,n]))
    for n in ACTUATOR_IDS:
        cmds.append((0x200 + n, [0x00,0xFB,0xFB,0xFB,0xFB,0xFB,0x00,0x00]))

    print("Running original init sequence...")
    for arb, d in cmds:
        await send_message(bus, arb, d, "INIT")
        await asyncio.sleep(0.1)
    print("Initialization complete")

# ───────────────────────────────────────────────────────
# Standard open/close helper
# ───────────────────────────────────────────────────────
async def send_act(bus, aid, action):
    arb_map = {22: 0x222, 24: 0x224, 26: 0x226}
    arb = arb_map.get(aid)
    if not arb:
        return
    data = [0xE8,0x03,0xFB,0xFB,0xFB,0xFB,0x00,0x00] if action=="open" else \
           [0x02,0xFB,0xFB,0xFB,0xFB,0xFB,0x00,0x00]
    await send_message(bus, arb, data, f"{action.upper()}_{aid}")

# ───────────────────────────────────────────────────────
# Run‐Out & Run‐In commands
# ───────────────────────────────────────────────────────
async def send_run_out_command(bus):
    """Send Run Out actuator command."""
    print("📤 Sending Run Out command at 10 ft")
    run_out = [
        (0x222, [0xE8,0x03,0xFB,0xFB,0xFB,0xFB,0x00,0x00]),
        (0x224, [0xE8,0x03,0xFB,0xFB,0xFB,0xFB,0x00,0x00]),
        (0x226, [0xE8,0x03,0xFB,0xFB,0xFB,0xFB,0x00,0x00]),
    ]
    for arb, data in run_out:
        await send_message(bus, arb, data, "RUN_OUT")
        await asyncio.sleep(0.2)

async def send_run_in_command(bus):
    """Send Run In actuator command."""
    print("📤 Sending Run In command at 20 ft")
    run_in = [
        (0x222, [0x02,0xFB,0xFB,0xFB,0xFB,0xFB,0x00,0x00]),
        (0x224, [0x02,0xFB,0xFB,0xFB,0xFB,0xFB,0x00,0x00]),
        (0x226, [0x02,0xFB,0xFB,0xFB,0xFB,0xFB,0x00,0x00]),
    ]
    for arb, data in run_in:
        await send_message(bus, arb, data, "RUN_IN")
        await asyncio.sleep(0.2)

# ───────────────────────────────────────────────────────
# Controller with continuous commands
# ───────────────────────────────────────────────────────
async def controller(bus, b, a, vx, _):
    global index, xx, i1, i2, i3

    if index is None:
        load_signal_data()
        index = int(np.argmin(np.abs(c - b)))
        xx    = heading_array.copy() if (index+1)%2 else (L - heading_array)
        # set initial bits
        for aid in ACTUATOR_IDS:
            await send_act(bus, aid, "close")
        if s1[0]==1: await send_act(bus,22,"open")
        if s2[0]==1: await send_act(bus,24,"open")
        if s3[0]==1: await send_act(bus,26,"open")

    w1 = xx[i1] if i1 < len(xx) else None
    w2 = xx[i2] if i2 < len(xx) else None
    w3 = xx[i3] if i3 < len(xx) else None
    lookahead1 = -b - d + T*0.9
    lookahead2 = -b - d

    log_row(
        a=-a, b=-b,
        s1=s1, s2=s2, s3=s3,
        ds1=ds1, ds2=ds2, ds3=ds3,
        w1=w1, w2=w2, w3=w3,
        i1=i1, i2=i2, i3=i3,
        lookahead1=lookahead1, lookahead2=lookahead2,
        x=latest["x"], y=latest["y"],
        vx=latest["vx"], vy=latest["vy"]
    )

    # Actuator 1
    if i1 < len(xx):
        if s1[i1] == 1:
            await send_act(bus,22,"open")
            if lookahead1 > w1:
                i1 += 1
        else:
            await send_act(bus,22,"close")
            if lookahead2 > w1:
                i1 += 1

    # Actuator 2
    if i2 < len(xx):
        if s2[i2] == 1:
            await send_act(bus,24,"open")
            if lookahead1 > w2:
                i2 += 1
        else:
            await send_act(bus,24,"close")
            if lookahead2 > w2:
                i2 += 1

    # Actuator 3
    if i3 < len(xx):
        if s3[i3] == 1:
            await send_act(bus,26,"open")
            if lookahead1 > w3:
                i3 += 1
        else:
            await send_act(bus,26,"close")
            if lookahead2 > w3:
                i3 += 1

    if b > -xx[-1]:
        for aid in ACTUATOR_IDS:
            await send_act(bus, aid, "close")

    await asyncio.sleep(0.02)
    if i1 >= len(xx) and i2 >= len(xx) and i3 >= len(xx):
        return

# ───────────────────────────────────────────────────────
# GPS streaming + controller
# ───────────────────────────────────────────────────────
async def gps_stream(gps_cfg, bus):
    global initial_x, initial_y
    cfg = proto_from_json_file(gps_cfg, EventServiceConfig())
    sub = EventClient(cfg).subscribe(cfg.subscriptions[0])

    async for _, msg in sub:
        if isinstance(msg, gps_pb2.RelativePositionFrame):
            x, y = msg.relative_pose_north, msg.relative_pose_east
            if initial_x is None:
                initial_x, initial_y = x, y
                rel_x = rel_y = 0.0
            else:
                rel_x, rel_y = x - initial_x, y - initial_y
            print(f"[REL] x={rel_x:.3f} y={rel_y:.3f}")
            update_latest(x=rel_x, y=rel_y)
            continue

        if isinstance(msg, gps_pb2.GpsFrame):
            vx, vy = msg.vel_north, msg.vel_east
            print(f"[VEL] vx={vx:.3f} vy={vy:.3f}")
            update_latest(vx=vx, vy=vy)

        b   = latest["y"]
        vx0, vy0 = latest["vx"], latest["vy"]
        a = math.atan2(vx0, vy0)
        await controller(bus, b, a, vx0, None)

# ───────────────────────────────────────────────────────
# Main entry
# ───────────────────────────────────────────────────────
async def main(gps_cfg):
    bus = setup_can_bus()
    await send_initial(bus)
    # Example one‑off commands:
    # await send_run_out_command(bus)
    # await send_run_in_command(bus)
    await asyncio.gather(gps_stream(gps_cfg, bus),)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gps-service-config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.gps_service_config))
