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
CAN_CHANNEL  = 'can0'
ACTUATOR_IDS = [22, 24, 26]

L = 30.48    # field length (m)
d = 0.9      # look‑back distance (rad)
T = 5        # time horizon (s)

# These get populated in load_signal_data()
heading_array = None
c             = None
s1 = s2 = s3  = None

# Indices into heading_array for each actuator
i1 = i2 = i3 = 0

# GPS state
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
    "w1","w2","w3",
    "i1","i2","i3",
    "lookahead","x","y","vx","vy",
]
f_csv = open(csv_path, "w", newline="")
writer = csv.DictWriter(f_csv, fieldnames=csv_headers)
writer.writeheader()
f_csv.flush()
start_time = time.time()

def log_row(**kwargs):
    row = {h: "" for h in csv_headers}
    row["time_sec"] = f"{time.time() - start_time:.3f}"
    for k,v in kwargs.items():
        if k not in row: continue
        if isinstance(v,(list,np.ndarray)):
            row[k] = repr(list(v))
        elif isinstance(v,float):
            row[k] = f"{v:.3f}"
        else:
            row[k] = str(v)
    writer.writerow(row)
    f_csv.flush()

def update_latest(**kwargs):
    for k,v in kwargs.items():
        if k in latest: latest[k] = v

# ───────────────────────────────────────────────────────
# Load the 3×5 signal matrix (plus the 1×5 heading row)
# ───────────────────────────────────────────────────────
def load_signal_data():
    global heading_array, c, s1, s2, s3
    raw = np.loadtxt(Path(__file__).parent/"signals3.txt")
    heading_array = raw[0,:]            # [0, 3.048, 6.096, …]
    c             = heading_array.copy()
    s1, s2, s3    = raw[1:,:].astype(int)
    print("signals3.txt loaded:", heading_array, (s1.shape, s2.shape, s3.shape))

# ───────────────────────────────────────────────────────
# CAN setup & send helpers
# ───────────────────────────────────────────────────────
def setup_can_bus():
    bus = can.interface.Bus(channel=CAN_CHANNEL,interface='socketcan')
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
# Full initialization: NMT, SDO mode & enable, then homing
# ───────────────────────────────────────────────────────
async def send_initial(bus):
    # 1) NMT start all
    await send_message(bus,0x000,[0x01,0x00],"NMT_START_ALL")
    await asyncio.sleep(0.1)

    # 2) Per-node: set Profile Position mode + Operation Enabled
    for node in ACTUATOR_IDS:
        # set mode = 0x01
        await send_message(bus,0x600+node,[0x2F,0x60,0x60,0x00,0x01,0,0,0],f"SDO_MODE_{node}")
        await asyncio.sleep(0.05)
        # enable = control‑word 0x0F
        await send_message(bus,0x600+node,[0x2B,0x40,0x60,0x00,0x0F,0,0,0],f"SDO_EN_{node}")
        await asyncio.sleep(0.05)

    # 3) Homing / your original init messages
    seq = []
    for n in ACTUATOR_IDS:
        seq.append((0x600+n,[0x23,0x16,0x10,0x01,0xC8,0,0x01,0]))
    for n in ACTUATOR_IDS:
        seq.append((0x000,[0x01,n]))
    for n in ACTUATOR_IDS:
        seq.append((0x200+n,[0x00,0xFB,0xFB,0xFB,0xFB,0xFB,0,0]))
    print("Running homing…")
    for arb,d in seq:
        await send_message(bus,arb,d,"INIT")
        await asyncio.sleep(0.1)
    print("Init complete")

# ───────────────────────────────────────────────────────
# Simple open/close helper
# ───────────────────────────────────────────────────────
async def send_act(bus,aid,action):
    arb_map={22:0x222,24:0x224,26:0x226}
    arb=arb_map[aid]
    data=[0xE8,3,0xFB,0xFB,0xFB,0xFB,0,0] if action=="open" else [2,0xFB,0xFB,0xFB,0xFB,0xFB,0,0]
    await send_message(bus,arb,data,f"{action.upper()}_{aid}")

# ───────────────────────────────────────────────────────
# Controller: single lookahead threshold → one command
# ───────────────────────────────────────────────────────
async def controller(bus,b,a,vx,_):
    global i1,i2,i3

    # first pass: load signals & do initial bits
    if heading_array is None:
        load_signal_data()
        # send the very first bit for each actuator
        if s1[0]==1: await send_act(bus,22,"open")
        if s2[0]==1: await send_act(bus,24,"open")
        if s3[0]==1: await send_act(bus,26,"open")

    # compute common lookahead
    lookahead = -b - d + (T * 0.9)

    # get next target for each actuator
    w1 = c[i1] if i1 < len(c) else None
    w2 = c[i2] if i2 < len(c) else None
    w3 = c[i3] if i3 < len(c) else None

    # log state
    log_row(
      a=-a, b=-b,
      s1=s1, s2=s2, s3=s3,
      w1=w1, w2=w2, w3=w3,
      i1=i1, i2=i2, i3=i3,
      lookahead=lookahead,
      x=latest["x"], y=latest["y"],
      vx=latest["vx"], vy=latest["vy"]
    )

    # actuator 1: when lookahead > next heading, send its bit
    if i1 < len(c) and lookahead > w1:
        cmd = "open" if s1[i1]==1 else "close"
        await send_act(bus,22,cmd)
        i1 += 1

    # actuator 2
    if i2 < len(c) and lookahead > w2:
        cmd = "open" if s2[i2]==1 else "close"
        await send_act(bus,24,cmd)
        i2 += 1

    # actuator 3
    if i3 < len(c) and lookahead > w3:
        cmd = "open" if s3[i3]==1 else "close"
        await send_act(bus,26,cmd)
        i3 += 1

    # final “close all” if at end
    if b > -c[-1]:
        for aid in ACTUATOR_IDS:
            await send_act(bus,aid,"close")

    # throttle ~50 Hz
    await asyncio.sleep(0.02)

# ───────────────────────────────────────────────────────
# GPS stream + controller
# ───────────────────────────────────────────────────────
async def gps_stream(cfg_path,bus):
    cfg = proto_from_json_file(cfg_path,EventServiceConfig())
    sub = EventClient(cfg).subscribe(cfg.subscriptions[0])
    global initial_x,initial_y

    async for _,msg in sub:
        if isinstance(msg,gps_pb2.RelativePositionFrame):
            x,y = msg.relative_pose_north, msg.relative_pose_east
            if initial_x is None:
                initial_x,initial_y = x,y
                rel_x=rel_y=0.0
            else:
                rel_x,rel_y = x-initial_x, y-initial_y
            update_latest(x=rel_x,y=rel_y)
            continue

        if isinstance(msg,gps_pb2.GpsFrame):
            update_latest(vx=msg.vel_north, vy=msg.vel_east)

        b  = latest["y"]
        a  = math.atan2(latest["vx"], latest["vy"])
        await controller(bus,b,a,latest["vx"],None)

# ───────────────────────────────────────────────────────
# Main entrypoint
# ───────────────────────────────────────────────────────
async def main(gps_cfg_path):
    bus = setup_can_bus()
    await send_initial(bus)
    await asyncio.gather(gps_stream(gps_cfg_path, bus))

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--gps-service-config",type=Path,required=True)
    args = p.parse_args()
    asyncio.run(main(args.gps_service_config))
