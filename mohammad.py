import can
import time
import math
import asyncio
import csv
from pathlib import Path
from datetime import datetime
import argparse
from farm_ng.core.event_client import EventClient
from farm_ng.core.events_file_reader import proto_from_json_file
from farm_ng.core.event_service_pb2 import EventServiceConfig
from farm_ng.gps import gps_pb2

# Global variables for GPS processing
initial_x = None
initial_y = None
previous_time = None
previous_position = None

# CAN configuration
CAN_CHANNEL = 'can0'

# CSV file (both GPS and actuator events are logged here)
csv_file = Path(__file__).parent / "gps_can_log.csv"
csv_headers = ["time_sec", "event_type", "x", "y", "vx", "vy", "sampling_time_sec", "actuator_id", "command"]

# Create the CSV file and write headers
with open(csv_file, mode='w', newline='') as file:
    writer = csv.writer(file)
    writer.writerow(csv_headers)

# Record the program start time (for elapsed seconds)
start_time = time.time()


def log_event(event_type, x=None, y=None, vx=None, vy=None, sampling_time=None, actuator_id=None, command=None):
    """
    Log an event to the CSV file.
    The first column is the elapsed time in seconds since the program started.
    """
    elapsed_time = time.time() - start_time
    with open(csv_file, mode='a', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([
            f"{elapsed_time:.3f}",
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
    """Initialize the CAN bus interface."""
    return can.interface.Bus(channel=CAN_CHANNEL, interface='socketcan')


def extract_position_and_velocity(msg, previous_position=None, previous_time=None):
    """
    Extract GPS position and compute velocity from a RelativePositionFrame message.
    X is taken from msg.relative_pose_north and Y from msg.relative_pose_east.
    """
    x = msg.relative_pose_north
    y = msg.relative_pose_east

    # If this is the first reading, return zero velocities
    if previous_position is None or previous_time is None:
        return x, y, 0.0, 0.0, x, y, time.time()

    time_diff = time.time() - previous_time
    vx = (x - previous_position[0]) / time_diff if time_diff else 0.0
    vy = (y - previous_position[1]) / time_diff if time_diff else 0.0

    return x, y, vx, vy, x, y, time.time()


def calculate_distance(x, y):
    """Calculate distance from the initial position."""
    delta_x = x - initial_x
    delta_y = y - initial_y
    return math.sqrt(delta_x**2 + delta_y**2)


def print_relative_position_frame(msg, previous_position=None, previous_time=None):
    """Print and log the GPS data when a RelativePositionFrame is received."""
    global initial_x, initial_y

    print("RELATIVE POSITION FRAME")
    print(f"X (North): {msg.relative_pose_north}")
    print(f"Y (West): {msg.relative_pose_east}")

    # Set the initial position on the first message
    if initial_x is None and initial_y is None:
        initial_x = msg.relative_pose_north
        initial_y = msg.relative_pose_east
        print(f"📍 Initial position set: X: {initial_x}, Y: {initial_y}")
        log_event("INITIAL_POSITION", initial_x, initial_y, 0, 0)
        return

    x, y, vx, vy, previous_x, previous_y, pt = extract_position_and_velocity(msg, previous_position, previous_time)
    log_event("GPS_FRAME", x, y, vx, vy, sampling_time=pt)
    print(f"X (North): {x}, Y (West): {y}")
    print(f"VX: {vx}, VY: {vy}")
    print("-" * 50)


async def send_message_async(bus, arbitration_id, data, command=""):
    """
    Send a CAN message asynchronously using run_in_executor to avoid blocking.
    Log the event after sending.
    """
    msg = can.Message(arbitration_id=arbitration_id, data=data, is_extended_id=False)
    try:
        await asyncio.get_event_loop().run_in_executor(None, bus.send, msg)
        print(f"✅ Sent: CAN ID {hex(arbitration_id)} Data {[hex(b) for b in data]}")
        log_event("CAN_SEND", actuator_id=arbitration_id, command=command)
    except can.CanError as e:
        print(f"❌ CAN Error: {e}")


async def send_initial_can_commands(bus):
    """Send the startup CAN commands (SDO, NMT, and Clear Error) asynchronously."""
    print("📤 Sending SDO commands")
    sdo_commands = [
        (0x622, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
        (0x624, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
        (0x626, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00])
    ]
    for arb_id, data in sdo_commands:
        await send_message_async(bus, arb_id, data, "SDO_COMMAND")
        await asyncio.sleep(0.2)

    print("📤 Sending NMT Start commands")
    nmt_commands = [
        (0x000, [0x01, 0x22]),
        (0x000, [0x01, 0x24]),
        (0x000, [0x01, 0x26])
    ]
    for arb_id, data in nmt_commands:
        await send_message_async(bus, arb_id, data, "NMT_START")
        await asyncio.sleep(0.2)

    print("📤 Sending Clear Error commands")
    clear_error_commands = [
        (0x222, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x224, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x226, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00])
    ]
    for arb_id, data in clear_error_commands:
        await send_message_async(bus, arb_id, data, "CLEAR_ERROR")
        await asyncio.sleep(0.2)

    print("✅ Initial CAN setup completed.")


async def send_actuator_command_async(bus, actuator_id, command_type):
    """
    Send an actuator command (open or close) for a given actuator.
    actuator_id is numeric (e.g., 22, 24, 26) and command_type is 'open' or 'close'.
    """
    # Map actuator to its CAN arbitration id
    if actuator_id == 22:
        arb_id = 0x222
    elif actuator_id == 24:
        arb_id = 0x224
    elif actuator_id == 26:
        arb_id = 0x226
    else:
        print("Invalid actuator id")
        return

    action = command_type  # either 'open' or 'close'
    # Define command data based on action:
    if action == 'open':
        data = [0xE8, 0x03, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]
    else:
        data = [0x02, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]

    # Send the actuator command asynchronously.
    await send_message_async(bus, arb_id, data, action)
    print(f"Actuator {arb_id} {action} command sent.")
    log_event("ACTUATOR", actuator_id, command=action)


async def gps_streaming_task(gps_config_path):
    """Continuously stream and print GPS data."""
    gps_config = proto_from_json_file(gps_config_path, EventServiceConfig())
    async for event, msg in EventClient(gps_config).subscribe(gps_config.subscriptions[0]):
        if isinstance(msg, gps_pb2.RelativePositionFrame):
            print_relative_position_frame(msg)


async def actuator_task(bus):
    """
    Wait until 20 seconds have elapsed, then send the actuator open command,
    and then wait another 10 seconds and send the close command.
    """
    # Wait for 20 seconds from program start
    await asyncio.sleep(20)
    await send_actuator_command_async(bus, 22, 'open')
    # Wait 10 seconds after open command
    await asyncio.sleep(10)
    await send_actuator_command_async(bus, 22, 'close')


async def main(gps_config_path):
    # Setup CAN bus
    bus = setup_can_bus()

    # Send initial CAN commands (SDO, NMT, Clear Error)
    await send_initial_can_commands(bus)

    # Create two tasks:
    # 1. GPS streaming task.
    # 2. Actuator task that sends actuator open at 20s and close at 30s.
    gps_task = asyncio.create_task(gps_streaming_task(gps_config_path))
    act_task = asyncio.create_task(actuator_task(bus))

    # Run both tasks concurrently
    await asyncio.gather(gps_task, act_task)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python gps_actuator_control.py", 
                                     description="GPS streaming and Actuator control with CSV logging.")
    parser.add_argument("--gps-service-config", type=Path, required=True,
                        help="The GPS service config file (service_config.json)")
    args = parser.parse_args()

    asyncio.run(main(args.gps_service_config))
