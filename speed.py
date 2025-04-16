import can
import time
import math
import asyncio
import csv
from pathlib import Path
import argparse
from farm_ng.core.event_client import EventClient
from farm_ng.core.events_file_reader import proto_from_json_file
from farm_ng.core.event_service_pb2 import EventServiceConfig
from farm_ng.gps import gps_pb2

# Globals
initial_x = None
initial_y = None
start_time = time.time()
CAN_CHANNEL = 'can0'

# CSV File
csv_file = Path(__file__).parent / "gps_can_log.csv"
csv_headers = [
    "time_sec", "x", "y", "vx", "vy", "sampling_time_sec",
    "actuator_id", "command", "actuator_position_mm", "actuator_speed_mms", "event_type"
]
with open(csv_file, mode='w', newline='') as f:
    csv.writer(f).writerow(csv_headers)

latest_data = {
    "x": "", "y": "", "vx": "", "vy": "",
    "sampling_time_sec": "", "actuator_id": "",
    "command": "", "actuator_position_mm": "", "actuator_speed_mms": "", "event_type": ""
}

def log_event(event_type, x=None, y=None, vx=None, vy=None, sampling_time=None,
              actuator_id=None, command=None, position=None, speed=None):
    elapsed = time.time() - start_time

    if x is not None: latest_data["x"] = f"{x:.3f}"
    if y is not None: latest_data["y"] = f"{y:.3f}"
    if vx is not None: latest_data["vx"] = f"{vx:.3f}"
    if vy is not None: latest_data["vy"] = f"{vy:.3f}"
    if sampling_time is not None: latest_data["sampling_time_sec"] = f"{sampling_time:.3f}"
    if actuator_id is not None: latest_data["actuator_id"] = actuator_id
    if command is not None: latest_data["command"] = command
    if position is not None: latest_data["actuator_position_mm"] = f"{position:.1f}"
    if speed is not None: latest_data["actuator_speed_mms"] = f"{speed:.1f}"
    latest_data["event_type"] = event_type

    with open(csv_file, mode='a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            f"{elapsed:.3f}",
            latest_data["x"], latest_data["y"],
            latest_data["vx"], latest_data["vy"],
            latest_data["sampling_time_sec"],
            latest_data["actuator_id"], latest_data["command"],
            latest_data["actuator_position_mm"], latest_data["actuator_speed_mms"],
            latest_data["event_type"]
        ])

def setup_can_bus():
    return can.interface.Bus(channel=CAN_CHANNEL, interface='socketcan')

async def send_message_async(bus, arb_id, data, command=""):
    msg = can.Message(arbitration_id=arb_id, data=data, is_extended_id=False)
    try:
        await asyncio.get_event_loop().run_in_executor(None, bus.send, msg)
        print(f"\u2705 Sent: CAN ID {hex(arb_id)} Data {[hex(b) for b in data]}")
        log_event("CAN_SEND", actuator_id=arb_id, command=command)
    except can.CanError as e:
        print(f"\u274C CAN Error: {e}")

async def read_sdo_feedback(bus, node_id):
    read_position = [0x40, 0x01, 0x20, 0x00, 0, 0, 0, 0]
    read_speed =    [0x40, 0x01, 0x20, 0x04, 0, 0, 0, 0]
    request_id = 0x600 + node_id
    response_id = 0x580 + node_id

    def parse_response(data):
        return int.from_bytes(data[4:6], byteorder='little')

    async def request_and_wait(data):
        await send_message_async(bus, request_id, data, "SDO_READ")
        while True:
            msg = await asyncio.get_event_loop().run_in_executor(None, bus.recv)
            if msg.arbitration_id == response_id and msg.data[0] & 0x40 == 0x40:
                return parse_response(msg.data)

    pos = await request_and_wait(read_position)
    spd = await request_and_wait(read_speed)

    pos_mm = pos * 0.1
    spd_mms = spd * 0.1
    print(f"\ud83d\udce1 Actuator {node_id} Feedback → Position: {pos_mm:.1f} mm | Speed: {spd_mms:.1f} mm/s")
    return pos_mm, spd_mms

async def send_actuator_command_with_feedback(bus, actuator_id, action):
    arb_id = {22: 0x222, 24: 0x224, 26: 0x226}.get(actuator_id)
    if not arb_id:
        print("Invalid actuator ID.")
        return

    run_cmd = [0xE8, 0x03, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00] if action == 'open' else \
              [0x01, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]  # correct RUN IN value
    await send_message_async(bus, arb_id, run_cmd, action)
    await asyncio.sleep(0.5)

    pos, spd = await read_sdo_feedback(bus, actuator_id)
    log_event("ACTUATOR_FEEDBACK", actuator_id=actuator_id, command=action, position=pos, speed=spd)

async def send_initial_can_commands(bus):
    sdo = [
        (0x622, [0x23, 0x16, 0x10, 0x01, 0xC8, 0, 0x01, 0]),
        (0x624, [0x23, 0x16, 0x10, 0x01, 0xC8, 0, 0x01, 0]),
        (0x626, [0x23, 0x16, 0x10, 0x01, 0xC8, 0, 0x01, 0])
    ]
    nmt = [(0x000, [0x01, i]) for i in (0x22, 0x24, 0x26)]
    clr = [
        (0x222, [0, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0, 0]),
        (0x224, [0, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0, 0]),
        (0x226, [0, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0, 0])
    ]
    for cmd in sdo + nmt + clr:
        await send_message_async(bus, *cmd)
        await asyncio.sleep(0.2)

async def listen_actuator_pdo(bus):
    while True:
        msg = await asyncio.get_event_loop().run_in_executor(None, bus.recv)
        if msg.arbitration_id == 0x1A2:
            data = msg.data
            pos_mm = int.from_bytes(data[1:3], byteorder='little') * 0.1
            print(f"\ud83d\udce2 PDO Feedback: Position ≈ {pos_mm:.1f} mm | Raw: {[hex(b) for b in data]}")
            log_event("PDO_FEEDBACK", actuator_id="22", position=pos_mm)

def print_gps_frame(msg):
    vx = msg.vel_north
    vy = msg.vel_east
    print(f"GPS FRAME: vx: {vx:.3f}, vy: {vy:.3f}")
    log_event("GPS_FRAME", vx=vx, vy=vy, sampling_time=time.time())

def print_relative_position_frame(msg):
    global initial_x, initial_y
    x = msg.relative_pose_north
    y = msg.relative_pose_east
    if initial_x is None or initial_y is None:
        initial_x, initial_y = x, y
        log_event("INITIAL_POSITION", x=x, y=y)
        return
    log_event("RELATIVE_FRAME", x=x, y=y, sampling_time=time.time())

async def gps_streaming_task(gps_config_path):
    config = proto_from_json_file(gps_config_path, EventServiceConfig())
    async for event, msg in EventClient(config).subscribe(config.subscriptions[0]):
        if isinstance(msg, gps_pb2.RelativePositionFrame):
            print_relative_position_frame(msg)
        elif isinstance(msg, gps_pb2.GpsFrame):
            print_gps_frame(msg)

async def actuator_task(bus):
    await asyncio.sleep(20)
    await send_actuator_command_with_feedback(bus, 22, 'open')
    await asyncio.sleep(10)
    await send_actuator_command_with_feedback(bus, 22, 'close')

async def main(gps_config_path):
    bus = setup_can_bus()
    await send_initial_can_commands(bus)
    gps_task = asyncio.create_task(gps_streaming_task(gps_config_path))
    act_task = asyncio.create_task(actuator_task(bus))
    pdo_task = asyncio.create_task(listen_actuator_pdo(bus))
    await asyncio.gather(gps_task, act_task, pdo_task)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gps-service-config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.gps_service_config))
