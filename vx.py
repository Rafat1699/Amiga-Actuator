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

CAN_CHANNEL = 'can0'
csv_file = Path(__file__).parent / "gps_can_log.csv"
csv_headers = ["time_sec", "event_type", "x", "y", "vx", "vy", "sampling_time_sec", "actuator_id", "command"]

with open(csv_file, mode='w', newline='') as f:
    csv.writer(f).writerow(csv_headers)

start_time = time.time()

def log_event(event_type, x=None, y=None, vx=None, vy=None, sampling_time=None, actuator_id=None, command=None):
    elapsed = time.time() - start_time
    with open(csv_file, mode='a', newline='') as f:
        csv.writer(f).writerow([
            f"{elapsed:.3f}",
            event_type,
            f"{x:.3f}" if x is not None else "",
            f"{y:.3f}" if y is not None else "",
            f"{vx:.3f}" if vx is not None else "",
            f"{vy:.3f}" if vy is not None else "",
            f"{sampling_time:.3f}" if sampling_time is not None else "",
            actuator_id if actuator_id else "",
            command if command else ""
        ])

def setup_can_bus():
    return can.interface.Bus(channel=CAN_CHANNEL, interface='socketcan')

async def send_message_async(bus, arb_id, data, command=""):
    msg = can.Message(arbitration_id=arb_id, data=data, is_extended_id=False)
    try:
        await asyncio.get_event_loop().run_in_executor(None, bus.send, msg)
        print(f"✅ Sent: CAN ID {hex(arb_id)} Data {[hex(b) for b in data]}")
        log_event("CAN_SEND", actuator_id=arb_id, command=command)
    except can.CanError as e:
        print(f"❌ CAN Error: {e}")

async def send_initial_can_commands(bus):
    sdo = [
        (0x622, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
        (0x624, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
        (0x626, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00])
    ]
    nmt = [(0x000, [0x01, i]) for i in (0x22, 0x24, 0x26)]
    clr = [
        (0x222, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x224, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x226, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00])
    ]
    print("📤 Sending SDO, NMT, and Clear Error commands...")
    for cmd in sdo + nmt + clr:
        await send_message_async(bus, *cmd, "SDO" if cmd in sdo else "NMT" if cmd in nmt else "CLEAR_ERROR")
        await asyncio.sleep(0.2)
    print("✅ CAN startup complete.")

async def send_actuator_command_async(bus, actuator_id, action):
    arb_id = {22: 0x222, 24: 0x224, 26: 0x226}.get(actuator_id)
    if arb_id is None:
        print("❌ Invalid actuator ID")
        return
    data = [0xE8, 0x03, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00] if action == 'open' else [0x02, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]
    await send_message_async(bus, arb_id, data, action)
    print(f"🛠️ Actuator {actuator_id} {action.upper()} sent")
    log_event("ACTUATOR", actuator_id=actuator_id, command=action)

def print_gps_frame(msg):
    # Log vel_north (vx), vel_east (vy)
    vx = msg.vel_north
    vy = msg.vel_east
    print(f"GPS FRAME: velNorth (vx): {vx:.3f}, velEast (vy): {vy:.3f}")
    log_event("GPS_FRAME", vx=vx, vy=vy, sampling_time=time.time())

def print_relative_position_frame(msg):
    global initial_x, initial_y
    x = msg.relative_pose_north
    y = msg.relative_pose_east
    if initial_x is None or initial_y is None:
        initial_x, initial_y = x, y
        print(f"📍 Initial pos X={x:.3f}, Y={y:.3f}")
        log_event("INITIAL_POSITION", x=x, y=y)
        return
    print(f"REL FRAME: X={x:.3f} Y={y:.3f}")
    log_event("RELATIVE_FRAME", x=x, y=y, sampling_time=time.time())

async def gps_streaming_task(gps_config_path):
    config = proto_from_json_file(gps_config_path, EventServiceConfig())
    async for event, msg in EventClient(config).subscribe(config.subscriptions[0]):
        if isinstance(msg, gps_pb2.RelativePositionFrame):
            print_relative_position_frame(msg)
        elif isinstance(msg, gps_pb2.GpsFrame):  # <== Use GpsFrame for vel_north, vel_east
            print_gps_frame(msg)

async def actuator_task(bus):
    await asyncio.sleep(20)
    await send_actuator_command_async(bus, 22, 'open')
    await asyncio.sleep(10)
    await send_actuator_command_async(bus, 22, 'close')

async def main(gps_config_path):
    bus = setup_can_bus()
    await send_initial_can_commands(bus)
    gps_task = asyncio.create_task(gps_streaming_task(gps_config_path))
    act_task = asyncio.create_task(actuator_task(bus))
    await asyncio.gather(gps_task, act_task)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gps-service-config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.gps_service_config))
