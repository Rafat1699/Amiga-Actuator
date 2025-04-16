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
    "time_sec", "event_type", "x", "y", "vx", "vy", "sampling_time_sec",
    "actuator_id", "command", "actuator_position_mm", "actuator_speed_mms"
]
with open(csv_file, mode='w', newline='') as f:
    csv.writer(f).writerow(csv_headers)

def log_event(event_type, x=None, y=None, vx=None, vy=None, sampling_time=None,
              actuator_id=None, command=None, position=None, speed=None):
    elapsed = time.time() - start_time
    with open(csv_file, mode='a', newline='') as f:
        csv.writer(f).writerow([
            f"{elapsed:.3f}",
            event_type,
            f"{x:.3f}" if x else "",
            f"{y:.3f}" if y else "",
            f"{vx:.3f}" if vx else "",
            f"{vy:.3f}" if vy else "",
            f"{sampling_time:.3f}" if sampling_time else "",
            actuator_id if actuator_id else "",
            command if command else "",
            f"{position:.1f}" if position else "",
            f"{speed:.1f}" if speed else ""
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
            if msg.arbitration_id == response_id and msg.data[0] == 0x43:
                return parse_response(msg.data)

    pos = await request_and_wait(read_position)
    spd = await request_and_wait(read_speed)

    pos_mm = pos * 0.1
    spd_mms = spd * 0.1
    print(f"📥 Actuator {node_id} Feedback → Position: {pos_mm:.1f} mm | Speed: {spd_mms:.1f} mm/s")
    return pos_mm, spd_mms

async def send_actuator_command_with_feedback(bus, actuator_id, action):
    arb_id = {22: 0x222, 24: 0x224, 26: 0x226}.get(actuator_id)
    if arb_id is None:
        print("Invalid actuator")
        return
    data = [0xE8, 0x03, 0xFB, 0xFB, 0xFB, 0xFB, 0, 0] if action == 'open' else [0x02, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0, 0]
    await send_message_async(bus, arb_id, data, action)
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
    await asyncio.gather(gps_task, act_task)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gps-service-config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.gps_service_config))
