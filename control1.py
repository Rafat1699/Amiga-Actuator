import can
import time
import math
import asyncio
import argparse
import csv
from pathlib import Path
from datetime import datetime
from farm_ng.core.event_client import EventClient
from farm_ng.core.event_service_pb2 import EventServiceConfig
from farm_ng.core.events_file_reader import proto_from_json_file
from farm_ng.gps import gps_pb2

# Global variables
initial_x = None
initial_y = None
previous_time = None
previous_position = None
triggered_actuators = set()

# CAN channel configuration
CAN_CHANNEL = 'can0'

# Prepare automatic timestamped CSV filename
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
csv_file = Path(__file__).parent / f"gps_can_log_{timestamp}.csv"
csv_headers = ["timestamp", "event_type", "x", "y", "vx", "vy", "sampling_time_sec", "actuator_id", "command"]

# Initialize CSV file with headers
with open(csv_file, mode='w', newline='') as file:
    writer = csv.writer(file)
    writer.writerow(csv_headers)

def log_event(event_type, x=None, y=None, vx=None, vy=None, sampling_time=None, actuator_id=None, command=None):
    """Log an event to the CSV file."""
    with open(csv_file, mode='a', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([datetime.now().isoformat(),
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

async def send_message_async(bus, arbitration_id, data, command=""):
    """Asynchronously send CAN message and log it."""
    msg = can.Message(arbitration_id=arbitration_id, data=data, is_extended_id=False)
    try:
        await asyncio.to_thread(bus.send, msg)  # Non-blocking send using asyncio
        print(f"✅ Sent: CAN ID {hex(arbitration_id)} Data {[hex(b) for b in data]}")
        log_event("CAN_SEND", actuator_id=arbitration_id, command=command)
    except can.CanError as e:
        print(f"❌ CAN Error: {e}")

def send_initial_can_commands(bus):
    """Send SDO, NMT Start, and Clear Error commands."""
    print("📤 Sending SDO commands")
    sdo_commands = [
        (0x622, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
        (0x624, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
        (0x626, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
    ]
    for arb_id, data in sdo_commands:
        asyncio.create_task(send_message_async(bus, arb_id, data, "SDO_COMMAND"))
        time.sleep(0.2)

    print("📤 Sending NMT Start commands")
    nmt_commands = [
        (0x000, [0x01, 0x22]),
        (0x000, [0x01, 0x24]),
        (0x000, [0x01, 0x26]),
    ]
    for arb_id, data in nmt_commands:
        asyncio.create_task(send_message_async(bus, arb_id, data, "NMT_START"))
        time.sleep(0.2)

    print("📤 Sending Clear Error commands")
    clear_error_commands = [
        (0x222, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x224, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x226, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
    ]
    for arb_id, data in clear_error_commands:
        asyncio.create_task(send_message_async(bus, arb_id, data, "CLEAR_ERROR"))
        time.sleep(0.2)

    print("✅ Initial CAN setup completed.")

async def send_actuator_command_async(bus, command_str):
    """Send actuator command asynchronously (open/close)."""
    actuator_id = int(command_str.split('-')[0])  # Get actuator ID (22, 24, or 26)
    action = command_str.split('-')[1]  # Get action (open or close)

    # Map actuator ID to CAN arbitration IDs
    if actuator_id == 22:
        arb_id = 0x222  # Corresponding CAN arbitration ID for actuator 22
    elif actuator_id == 24:
        arb_id = 0x224  # Corresponding CAN arbitration ID for actuator 24
    elif actuator_id == 26:
        arb_id = 0x226  # Corresponding CAN arbitration ID for actuator 26
    else:
        print("Invalid actuator ID.")
        return

    # Define the CAN data for open (Run Out) and close (Run In) commands
    if action == "open":
        data = [0xE8, 0x03, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]  # Run Out command
    elif action == "close":
        data = [0x02, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]  # Run In command
    else:
        print(f"Invalid action for actuator {actuator_id}. Please use 'open' or 'close'.")
        return

    # Send the message asynchronously
    await send_message_async(bus, arb_id, data, action)

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

def print_relative_position_frame(msg, bus, previous_position=None, previous_time=None):
    global initial_x, initial_y

    # Print current GPS data
    print("RELATIVE POSITION FRAME \n")
    print(f"X (North): {msg.relative_pose_north}")
    print(f"Y (West): {msg.relative_pose_east}")

    # Set initial position if it's the first frame
    if initial_x is None and initial_y is None:
        initial_x = msg.relative_pose_north
        initial_y = msg.relative_pose_east
        print(f"📍 Initial position set. X: {initial_x}, Y: {initial_y}")
        log_event("INITIAL_POSITION", initial_x, initial_y, 0, 0)
        return  # No further processing until first frame is received

    # Get current position and velocity
    x, y, vx, vy, previous_x, previous_y, previous_time = extract_position_and_velocity(
        msg, previous_position, previous_time
    )

    # Log GPS data and velocity to CSV
    log_event("GPS_FRAME", x, y, vx, vy, sampling_time=previous_time)

    # Print for terminal
    print(f"X (North): {x}, Y (West): {y}")
    print(f"VX: {vx}, VY: {vy}")
    print("-" * 50)

async def main(service_config_path: Path) -> None:
    """Run the GPS service client."""
    config: EventServiceConfig = proto_from_json_file(service_config_path, EventServiceConfig())

    # Setup CAN interface once
    bus = setup_can_bus()

    # Initial CAN setup (SDO, NMT, Clear Error)
    send_initial_can_commands(bus)

    # Listen for user input to engage actuators
    while True:
        command_str = input("Enter actuator command (e.g., 22-open, 24-close) or 'exit' to quit: ")
        if command_str == 'exit':
            break
        # Run actuator commands asynchronously in the background
        asyncio.create_task(send_actuator_command_async(bus, command_str))

    async for event, msg in EventClient(config).subscribe(config.subscriptions[0]):
        if isinstance(msg, gps_pb2.RelativePositionFrame):
            print_relative_position_frame(msg, bus)

    # Close CAN bus
    bus.shutdown()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python main.py", description="Amiga GPS stream + actuator trigger + CSV logging.")
    parser.add_argument("--service-config", type=Path, required=True, help="The GPS config.")
    args = parser.parse_args()

    asyncio.run(main(args.service_config))
