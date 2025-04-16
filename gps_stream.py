import can
import time
import math
import asyncio
import argparse
import csv
from pathlib import Path
from datetime import datetime
from farm_ng.core.event_client import EventClient
from farm_ng.core.event_service_pb2 import EventServiceConfig  # Import EventServiceConfig
from farm_ng.core.events_file_reader import proto_from_json_file
from farm_ng.gps import gps_pb2

# Global variables
initial_x = None
initial_y = None
previous_time = None
previous_position = None

# CAN channel configuration
CAN_CHANNEL = 'can0'

# Prepare automatic timestamped CSV filename
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
csv_file = Path(__file__).parent / f"gps_can_log_{timestamp}.csv"
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

def print_relative_position_frame(msg, previous_position=None, previous_time=None):
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
    config = proto_from_json_file(service_config_path, EventServiceConfig())

    async for event, msg in EventClient(config).subscribe(config.subscriptions[0]):
        if isinstance(msg, gps_pb2.RelativePositionFrame):
            print_relative_position_frame(msg)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python gps_stream.py", description="Amiga GPS stream + CSV logging.")
    parser.add_argument("--service-config", type=Path, required=True, help="The GPS config.")
    args = parser.parse_args()

    asyncio.run(main(args.service_config))
