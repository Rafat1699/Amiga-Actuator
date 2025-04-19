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

# Controller state (populated on first controller call)
index = None
ds1 = ds2 = ds3 = None
xx = None
i1 = 0
i2 = 0
i3 = 0

heading_array = None
S = None
c = None

# GPS state
initial_x = None
initial_y = None

# ───────────────────────────────────────────────────────
# CSV Logging Setup
# ───────────────────────────────────────────────────────
csv_file = Path(__file__).parent / "gps_can_log.csv"
csv_headers = [
    "time_sec", "x", "y", "vx", "vy", "command",
    "pdo_position_mm_22", "pdo_speed_mms_22",
    "pdo_position_mm_24", "pdo_speed_mms_24",
    "pdo_position_mm_26", "pdo_speed_mms_26",
]
latest = {h: "" for h in csv_headers if h != "time_sec"}
start_time = time.time()

def log_event(**kwargs):
    now = time.time()
    for k, v in kwargs.items():
        latest[k] = f"{v:.3f}" if isinstance(v, (int, float)) else str(v)
    row = {"time_sec": f"{now - start_time:.3f}"}
    row.update(latest)
    with open(csv_file, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_headers)
        writer.writerow(row)

# ───────────────────────────────────────────────────────
# CAN Helpers
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
# Signal Data Loader (signals2.txt)
# ───────────────────────────────────────────────────────
def load_signal_data():
    global heading_array, S, c
    path = Path(__file__).parent / "signals2.txt"
    data = np.loadtxt(path, delimiter=',')
    heading_array = data[0, :]
    S = data[1:, :]
    z = S.shape[0] // len(ACTUATOR_IDS)
    c = np.linspace(0, L, z)

# ───────────────────────────────────────────────────────
# Controller (delta‑signal logic)
# ───────────────────────────────────────────────────────
async def controller(bus, a, b, vx, _):
    global index, ds1, ds2, ds3, xx, i

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

    if i1 >= len(xx):
        return
    if i2 >= len(xx):
        return
    if i3 >= len(xx):
        return

    w1 = xx[i1]
    w2 = xx[i2]
    w3 = xx[i3]
    lookahead = a - d + T*vx

    if lookahead > w1 and ds1[i1]==1:
        await send_actuator_command(bus, 22, "open");  i1+=1
    elif lookahead < w1 and ds1[i1]==-1:
        await send_actuator_command(bus, 22, "close"); i1+=1

    if lookahead > w2 and ds2[i2]==1:
        await send_actuator_command(bus, 24, "open");  i2+=1
    elif lookahead < w2 and ds2[i2]==-1:
        await send_actuator_command(bus, 24, "close"); i2+=1

    if lookahead > w3 and ds3[i3]==1:
        await send_actuator_command(bus, 26, "open");  i3+=1
    elif lookahead < w3 and ds3[i3]==-1:
        await send_actuator_command(bus, 26, "close"); i3+=1

    if a > xx[-1]:
        for aid in ACTUATOR_IDS:
            await send_actuator_command(bus, aid, "close")

# ───────────────────────────────────────────────────────
# GPS Streaming Task (with terminal prints)
# ───────────────────────────────────────────────────────
async def gps_streaming_task(gps_config_path, bus):
    global initial_x, initial_y
    config: EventServiceConfig = proto_from_json_file(gps_config_path, EventServiceConfig())
    async for _, msg in EventClient(config).subscribe(config.subscriptions[0]):

        if isinstance(msg, gps_pb2.RelativePositionFrame):
            # update and log x,y
            x = msg.relative_pose_north
            y = msg.relative_pose_east
            if initial_x is None:
                initial_x, initial_y = x, y
                log_event(x=0.0, y=0.0)
                x, y = 0.0, 0.0
            else:
                log_event(x=x-initial_x, y=y-initial_y)
                x, y = x-initial_x, y-initial_y

            # print current x,y
            print(f"[REL_POS] x = {x:.3f}, y = {y:.3f}")
            continue

        if isinstance(msg, gps_pb2.GpsFrame):
            # update and log vx,vy
            vx = msg.vel_north
            vy = msg.vel_east
            log_event(vx=vx, vy=vy)

            # print current vx,vy
            print(f"[GPS   ] vx = {vx:.3f}, vy = {vy:.3f}")

        # safe float conversion for controller
        b      = float(latest["y"]  or 0.0)
        vx_now = float(latest["vx"] or 0.0)
        vy_now = float(latest["vy"] or 0.0)
        a = math.atan2(vx_now, vy_now)

        await controller(bus, a, b, vx_now, None)

# ───────────────────────────────────────────────────────
# PDO Listener Task (unchanged)
# ───────────────────────────────────────────────────────
ID_TO_AID = {0x1A2:22, 0x1A4:24, 0x1A6:26}

async def listen_actuator_pdo(bus):
    while True:
        msg = await asyncio.get_event_loop().run_in_executor(None, bus.recv)
        aid = ID_TO_AID.get(msg.arbitration_id)
        if aid:
            pos_mm = int.from_bytes(msg.data[0:2], "little")*0.1
            spd_mms= int.from_bytes(msg.data[5:7], "little")*0.1
            log_event(**{
                f"pdo_position_mm_{aid}": pos_mm,
                f"pdo_speed_mms_{aid}": spd_mms
            })

# ───────────────────────────────────────────────────────
# Main Entrypoint
# ───────────────────────────────────────────────────────
async def main(gps_config_path):
    if not csv_file.exists():
        with open(csv_file,"w",newline="") as f:
            writer = csv.DictWriter(f, fieldnames=csv_headers)
            writer.writeheader()

    bus = setup_can_bus()
    await send_initial_can_commands(bus)
    await asyncio.gather(
        gps_streaming_task(gps_config_path, bus),
        listen_actuator_pdo(bus),
    )

if __name__=="__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gps-service-config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.gps_service_config))
