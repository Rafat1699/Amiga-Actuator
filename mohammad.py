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

# Global variables
initial_x = None
initial_y = None
previous_time = None
previous_position = None

# CAN channel configuration
CAN_CHANNEL = 'can0'

# Fixed CSV file name (ensure both scripts write to the same file)
csv_file = Path(__file__).parent / "gps_can_log.csv"
csv_headers = ["time_sec", "event_type", "x", "y", "vx", "vy", "sampling_time_sec", "actuator_id", "command"]

# Initialize CSV file with headers
with open(csv_file, mode='w', newline='') as file:
    writer = csv.writer(file)
    writer.writerow(csv_headers)

# Track the start time of the program
start_time = time.time()  # Record the start time (in seconds)

def log_event(event_type, x=None, y=None, vx=None, vy=None, sampling_time=None, actuator_id=None, command=None):
    """Log an event to the CSV file."""
    # Calculate the elapsed time in seconds since the start of the program
    elapsed_time = time.time() - start_time
    with open(csv_file, mode='a', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([f"{elapsed_time:.3f}",  # Elapsed time in seconds
                         event_type,
                         f"{x:.3f}" if x is not None else "",
                         f"{y:.3f}" if y is not None else "",
                         f"{vx:.3f}" if vx is not None else "",
                         f"{vy:.3f}" if vy is not None else "",
                         f"{sampling_time:.3f}" if sampling_time is not None else "",
                         actuator_id if actuator_id else "",
                         command if command else ""])

def setup_can_bus():
    """Initialize CAN bus."""
    return can.interface.Bus(channel=CAN_CHANNEL, interface='socketcan')

def extract_position_and_velocity(msg, previous_position=None, previous_time=None):
    """Extract position (X, Y) and velocity (VX, VY) from the GPS frame."""
    # Extract position data
    x = msg.relative_pose_north  # X-axis (North)
    y = msg.relative_pose_east   # Y-axis (West)

    # Initialize previous values if first GPS reading
    if previous_position is None or previous_time is None:
        return x, y, 0.0, 0.0, x, y, time.time()

    # Calculate velocity (change in position / change in time)
    time_diff = time.time() - previous_time
    distance = calculate_distance(x, y)  # distance in meters

    # Compute velocities (VX, VY) based on change in position over time
    vx = (x - previous_position[0]) / time_diff if time_diff != 0 else 0.0  # Velocity in North (X)
    vy = (y - previous_position[1]) / time_diff if time_diff != 0 else 0.0  # Velocity in West (Y)

    return x, y, vx, vy, x, y, time.time()

def calculate_distance(x, y):
    """Calculate the distance from the initial position."""
    delta_x = x - initial_x
    delta_y = y - initial_y
    return math.sqrt(delta_x ** 2 + delta_y ** 2)

async def send_message_async(bus, arbitration_id, data, command=""):
    """Asynchronously send CAN message and log it."""
    msg = can.Message(arbitration_id=arbitration_id, data=data, is_extended_id=False)
    try:
        # Use run_in_executor to send the message in a separate thread
        await asyncio.get_event_loop().run_in_executor(None, bus.send, msg)  # Non-blocking send using run_in_executor
        print(f"✅ Sent: CAN ID {hex(arbitration_id)} Data {[hex(b) for b in data]}")
        log_event("CAN_SEND", actuator_id=arbitration_id, command=command)
    except can.CanError as e:
        print(f"❌ CAN Error: {e}")

async def send_initial_can_commands(bus):
    """Send SDO, NMT Start, and Clear Error commands asynchronously."""
    print("📤 Sending SDO commands")
    sdo_commands = [
        (0x622, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
        (0x624, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
        (0x626, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
    ]
    for arb_id, data in sdo_commands:
        await send_message_async(bus, arb_id, data, "SDO_COMMAND")
        await asyncio.sleep(0.2)

    print("📤 Sending NMT Start commands")
    nmt_commands = [
        (0x000, [0x01, 0x22]),
        (0x000, [0x01, 0x24]),
        (0x000, [0x01, 0x26]),
    ]
    for arb_id, data in nmt_commands:
        await send_message_async(bus, arb_id, data, "NMT_START")
        await asyncio.sleep(0.2)

    print("📤 Sending Clear Error commands")
    clear_error_commands = [
        (0x222, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x224, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x226, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
    ]
    for arb_id, data in clear_error_commands:
        await send_message_async(bus, arb_id, data, "CLEAR_ERROR")
        await asyncio.sleep(0.2)

    print("✅ Initial CAN setup completed.")

async def send_actuator_command_async(bus, actuator_id):
    """Send actuator command asynchronously (open/close)."""
    action = 'open' if actuator_id == 22 else 'close'
    arb_id = 0x222 if actuator_id == 22 else 0x224

    # Define the CAN data for open (Run Out) and close (Run In) commands
    data = [0xE8, 0x03, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]  # Run Out command for open
    if action == "close":
        data = [0x02, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]  # Run In command for close

    # Send the message asynchronously
    await send_message_async(bus, arb_id, data, action)
    await asyncio.sleep(20)  # Wait for 20 seconds
    print(f"Actuator {arb_id} {action} completed.")

async def main(gps_config_path, actuator_config_path):
    """Run the GPS service client and actuator control loop."""
    # Setup CAN bus for actuator control
    bus = setup_can_bus()

    # Send the initial setup commands (SDO, NMT, Clear Error)
    await send_initial_can_commands(bus)

    # Start actuator control: Open actuator 22, wait for 20 seconds, then close it
    await send_actuator_command_async(bus, 22)  # Open actuator 22
    await send_actuator_command_async(bus, 22)  # Close actuator 22 after 20 seconds

    # Start GPS data streaming and logging
    gps_config = proto_from_json_file(gps_config_path, EventServiceConfig())  # Load service_config.json for GPS
    async for event, msg in EventClient(gps_config).subscribe(gps_config.subscriptions[0]):
        if isinstance(msg, gps_pb2.RelativePositionFrame):
            print_relative_position_frame(msg)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python gps_actuator_control.py", description="GPS + Actuator control + CSV logging.")
    parser.add_argument("--gps-service-config", type=Path, required=True, help="The GPS service config (service_config.json).")
    parser.add_argument("--actuator-service-config", type=Path, required=True, help="The actuator service config (actuator_config.json).")
    args = parser.parse_args()

    asyncio.run(main(args.gps_service_config, args.actuator_service_config))
