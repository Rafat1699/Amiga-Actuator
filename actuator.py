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
initial_north = None
initial_east = None
triggered_distances = set()

# Define trigger distances (meters)
distance_trigger_thresholds = [3.048, 6.096]  # 10 ft, 20 ft

# CAN channel configuration
CAN_CHANNEL = 'can0'

# Prepare automatic timestamped CSV filename
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
csv_file = Path(__file__).parent / f"gps_can_log_{timestamp}.csv"
csv_headers = ["timestamp", "event_type", "north", "east", "down", "distance_traveled", "can_action"]

# Initialize CSV file with headers
with open(csv_file, mode='w', newline='') as file:
    writer = csv.writer(file)
    writer.writerow(csv_headers)

def log_event(event_type, north=None, east=None, down=None, distance=None, can_action=None):
    """Log an event to the CSV file."""
    with open(csv_file, mode='a', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([
            datetime.now().isoformat(),
            event_type,
            f"{north:.3f}" if north is not None else "",
            f"{east:.3f}" if east is not None else "",
            f"{down:.3f}" if down is not None else "",
            f"{distance:.3f}" if distance is not None else "",
            can_action if can_action else ""
        ])

def setup_can_bus():
    """Initialize CAN bus."""
    return can.interface.Bus(channel=CAN_CHANNEL, interface='socketcan')

def send_message(bus, arbitration_id, data, can_action=""):
    """Send CAN message and log it."""
    msg = can.Message(arbitration_id=arbitration_id, data=data, is_extended_id=False)
    try:
        bus.send(msg)
        print(f"✅ Sent: CAN ID {hex(arbitration_id)} Data {[hex(b) for b in data]}")
        log_event("CAN_SEND", can_action=can_action)
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
        send_message(bus, arb_id, data, "SDO_COMMAND")
        time.sleep(0.2)

    print("📤 Sending NMT Start commands")
    nmt_commands = [
        (0x000, [0x01, 0x22]),
        (0x000, [0x01, 0x24]),
        (0x000, [0x01, 0x26]),
    ]
    for arb_id, data in nmt_commands:
        send_message(bus, arb_id, data, "NMT_START")
        time.sleep(0.2)

    print("📤 Sending Clear Error commands")
    clear_error_commands = [
        (0x222, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x224, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x226, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
    ]
    for arb_id, data in clear_error_commands:
        send_message(bus, arb_id, data, "CLEAR_ERROR")
        time.sleep(0.2)

    print("✅ Initial CAN setup completed.")

def send_run_out_command(bus):
    """Send Run Out actuator command."""
    print("📤 Sending Run Out command at 10 ft")
    run_out_commands = [
        (0x222, [0xE8, 0x03, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x224, [0xE8, 0x03, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x226, [0xE8, 0x03, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
    ]
    for arb_id, data in run_out_commands:
        send_message(bus, arb_id, data, "RUN_OUT")
        time.sleep(0.2)

def send_run_in_command(bus):
    """Send Run In actuator command."""
    print("📤 Sending Run In command at 20 ft")
    run_in_commands = [
        (0x222, [0x02, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x224, [0x02, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x226, [0x02, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
    ]
    for arb_id, data in run_in_commands:
        send_message(bus, arb_id, data, "RUN_IN")
        time.sleep(0.2)

def calculate_distance(north, east):
    """Calculate the distance from the initial position."""
    delta_north = north - initial_north
    delta_east = east - initial_east
    return math.sqrt(delta_north ** 2 + delta_east ** 2)

def print_relative_position_frame(msg, bus):
    global initial_north, initial_east

    print("RELATIVE POSITION FRAME \n")
    print(f"North: {msg.relative_pose_north}")
    print(f"East: {msg.relative_pose_east}")
    print(f"Down: {msg.relative_pose_down}")

    if initial_north is None and initial_east is None:
        # Set initial reference point
        initial_north = msg.relative_pose_north
        initial_east = msg.relative_pose_east
        print(f"📍 Initial position set. North: {initial_north}, East: {initial_east}")
        log_event("INITIAL_POSITION", initial_north, initial_east, msg.relative_pose_down)
        return

    # Calculate distance traveled
    distance = calculate_distance(msg.relative_pose_north, msg.relative_pose_east)
    print(f"🚗 Distance traveled: {distance:.3f} meters")

    # Log GPS frame
    log_event("GPS_FRAME", msg.relative_pose_north, msg.relative_pose_east, msg.relative_pose_down, distance)

    # Check each trigger threshold
    if distance >= 3.048 and 3.048 not in triggered_distances:
        send_run_out_command(bus)
        triggered_distances.add(3.048)

    if distance >= 6.096 and 6.096 not in triggered_distances:
        send_run_in_command(bus)
        triggered_distances.add(6.096)

    print("-" * 50)

def print_gps_frame(msg):
    print("PVT FRAME \n")
    print(f"Latitude: {msg.latitude}")
    print(f"Longitude: {msg.longitude}")
    print(f"Altitude: {msg.altitude}")
    print(f"Ground speed: {msg.ground_speed}")
    print("-" * 50)

def print_ecef_frame(msg):
    print("ECEF FRAME \n")
    print(f"x: {msg.x}")
    print(f"y: {msg.y}")
    print(f"z: {msg.z}")
    print("-" * 50)

async def main(service_config_path: Path) -> None:
    """Run the GPS service client."""
    config: EventServiceConfig = proto_from_json_file(service_config_path, EventServiceConfig())

    # Setup CAN interface once
    bus = setup_can_bus()

    # Initial CAN setup (SDO, NMT, Clear Error)
    send_initial_can_commands(bus)

    async for event, msg in EventClient(config).subscribe(config.subscriptions[0]):
        if isinstance(msg, gps_pb2.RelativePositionFrame):
            print_relative_position_frame(msg, bus)
        elif isinstance(msg, gps_pb2.GpsFrame):
            print_gps_frame(msg)
        elif isinstance(msg, gps_pb2.EcefCoordinates):
            print_ecef_frame(msg)

    # Close CAN bus
    bus.shutdown()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python main.py", description="Amiga GPS stream + actuator trigger + CSV logging.")
    parser.add_argument("--service-config", type=Path, required=True, help="The GPS config.")
    args = parser.parse_args()

    asyncio.run(main(args.service_config))
