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
latest = {
    "x": 0.0, "y": 0.0, "vx": 0.0, "vy": 0.0,
    "pdo_position_mm": "", "pdo_speed_mms": "",
    "pdo_position_mm_22": "", "pdo_speed_mms_22": "",
    "pdo_position_mm_24": "", "pdo_speed_mms_24": "",
    "pdo_position_mm_26": "", "pdo_speed_mms_26": ""
}

# ─── CSV Setup ─────────────────────────────────────────────────
csv_path = Path(__file__).parent / "gps_can_log.csv"
csv_headers = [
    "time_sec", "command", "y", "vx", "vy", 
    "pdo_position_mm_22", "pdo_speed_mms_22", 
    "pdo_position_mm_24", "pdo_speed_mms_24",
    "pdo_position_mm_26", "pdo_speed_mms_26"
]
f_csv = open(csv_path, "w", newline="")
writer = csv.DictWriter(f_csv, fieldnames=csv_headers)
writer.writeheader()
f_csv.flush()
start_time = time.time()

def log_row(**kwargs):
    row = {h: latest.get(h, "") for h in csv_headers}
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

# ─── Reusable actuator trigger function ─────────────────────────
async def check_and_actuate(y, trigger_y_list, signal_list, actuator_id, index, bus):
    while index < len(trigger_y_list):
        signal = signal_list[index]
        trigger_y = trigger_y_list[index]

        if signal == 1 and abs(y) + 2.6 >= trigger_y:
            action = "open"
        elif signal == 0 and abs(y) - 0.9 >= trigger_y:
            action = "close"
        else:
            break

        print(f"[DEBUG] At Y={y:.2f}, Actuator {actuator_id} → {action.upper()} (signal={signal})")
        await send_actuator_command(bus, actuator_id, action)
        index += 1

    return index

# ─── Controller ────────────────────────────────────────────────
async def controller(bus):
    abs_y = abs(latest["y"])
    trigger_indices["22"] = await check_and_actuate(abs_y, TRIGGER_Y, SIGNALS[22], 22, trigger_indices["22"], bus)
    trigger_indices["24"] = await check_and_actuate(abs_y, TRIGGER_Y, SIGNALS[24], 24, trigger_indices["24"], bus)
    trigger_indices["26"] = await check_and_actuate(abs_y, TRIGGER_Y, SIGNALS[26], 26, trigger_indices["26"], bus)

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

# ─── Actuator PDO Listener ─────────────────────────────────────
async def listen_actuator_pdo(bus):
    while True:
        msg = await asyncio.get_event_loop().run_in_executor(None, bus.recv)
        
        # Listen for PDO messages of actuators with IDs 0x1A2, 0x1A4, 0x1A6
        if msg.arbitration_id in [0x1A2, 0x1A4, 0x1A6]:
            data = msg.data
            
            # Position: bytes 0 (LSB), 1 (MSB)
            pos_raw = int.from_bytes(data[0:2], byteorder='little')
            pos_mm = pos_raw * 0.1
            
            # Speed: bytes 5 (LSB), 6 (MSB)
            speed_raw = int.from_bytes(data[5:7], byteorder='little')
            speed_mm_s = speed_raw * 0.1

            # Determine actuator ID from the arbitration ID
            actuator_id = None
            if msg.arbitration_id == 0x1A2:
                actuator_id = 22
            elif msg.arbitration_id == 0x1A4:
                actuator_id = 24
            elif msg.arbitration_id == 0x1A6:
                actuator_id = 26

            print(f"📡 PDO Feedback → Actuator {actuator_id} | Position: {pos_mm:.1f} mm | Speed: {speed_mm_s:.1f} mm/s")

            # Update the latest data with position and speed
            latest[f"pdo_position_mm_{actuator_id}"] = f"{pos_mm:.1f}"
            latest[f"pdo_speed_mms_{actuator_id}"] = f"{speed_mm_s:.1f}"

            # Log the event with the actuator ID
            log_row(command=f"PDO_FEEDBACK_{actuator_id}")

# ─── Main ──────────────────────────────────────────────────────
async def main(gps_cfg):
    bus = setup_can_bus()
    load_signal_data()
    await send_initial(bus)
    await asyncio.gather(
        gps_stream(gps_cfg, bus),
        listen_actuator_pdo(bus)  # Now listening to 0x1A2, 0x1A4, 0x1A6
    )

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--gps-service-config", type=Path, required=True)
    args = p.parse_args()
    asyncio.run(main(args.gps_service_config))
