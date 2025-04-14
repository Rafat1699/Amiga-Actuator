import can
import time
import math
import asyncio
import argparse
import csv
import numpy as np
from pathlib import Path
from datetime import datetime
from farm_ng.core.event_client import EventClient
from farm_ng.core.event_service_pb2 import EventServiceConfig
from farm_ng.core.events_file_reader import proto_from_json_file
from farm_ng.gps import gps_pb2

# =============================================================================
# 1. Weed Map Generation for Three Rows
# =============================================================================
# Field parameters: 100 ft field with grid cells of 10 ft.
x_ft = np.arange(0, 101, 10)            # [0, 10, 20, ..., 100] feet
x_m = x_ft * 0.3048                     # convert feet to meters (~3.048 m per cell)
cell_length = 3.048                     # each cell is 3.048 m long
num_cells = len(x_m)

num_rows = 3  # There are three rows corresponding to three actuators
# Generate weed presence map for 3 rows (0: no weed, 1: weed) using a 43% probability.
weed_map = (np.random.rand(num_rows, num_cells) < 0.43).astype(int)

print("Weed map (each row corresponds to an actuator):")
print("Columns represent grid cells (starting at these distances in meters):", x_m)
for row in range(num_rows):
    print(f"Row {row+1} (Actuator {row+1}):", weed_map[row, :])

# Initialize trigger status storage.
# For each row (0-indexed) and for each cell (column index), we’ll record whether a command was sent.
triggered_states = {row: {} for row in range(num_rows)}

# =============================================================================
# 2. CSV Logging Setup (with automatic timestamp-based filename)
# =============================================================================
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
csv_file = Path(__file__).parent / f"gps_can_log_{timestamp}.csv"
csv_headers = ["timestamp", "event_type", "north", "east", "down", "distance_traveled", "can_action"]

with open(csv_file, mode='w', newline='') as file:
    writer = csv.writer(file)
    writer.writerow(csv_headers)

def log_event(event_type, north=None, east=None, down=None, distance=None, can_action=None):
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

# =============================================================================
# 3. CAN Bus Functions and Actuator Command Functions
# =============================================================================
# Define CAN channel (adjust as needed)
CAN_CHANNEL = 'can0'

def setup_can_bus():
    """Initialize the CAN bus."""
    return can.interface.Bus(channel=CAN_CHANNEL, interface='socketcan')

def send_message(bus, arbitration_id, data, can_action=""):
    """Send a CAN message and log the action."""
    msg = can.Message(arbitration_id=arbitration_id, data=data, is_extended_id=False)
    try:
        bus.send(msg)
        print(f"✅ Sent: CAN ID {hex(arbitration_id)} Data {[hex(b) for b in data]}")
        log_event("CAN_SEND", can_action=can_action)
    except can.CanError as e:
        print(f"❌ CAN Error: {e}")

def send_initial_can_commands(bus):
    """Send initial SDO, NMT Start, and Clear Error commands."""
    print("📤 Sending SDO commands")
    sdo_commands = [
        (0x622, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
        (0x624, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
        (0x626, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00])
    ]
    for arb_id, data in sdo_commands:
        send_message(bus, arb_id, data, "SDO_COMMAND")
        time.sleep(0.2)
    
    print("📤 Sending NMT Start commands")
    nmt_commands = [
        (0x000, [0x01, 0x22]),
        (0x000, [0x01, 0x24]),
        (0x000, [0x01, 0x26])
    ]
    for arb_id, data in nmt_commands:
        send_message(bus, arb_id, data, "NMT_START")
        time.sleep(0.2)
        
    print("📤 Sending Clear Error commands")
    clear_error_commands = [
        (0x222, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x224, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]),
        (0x226, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00])
    ]
    for arb_id, data in clear_error_commands:
        send_message(bus, arb_id, data, "CLEAR_ERROR")
        time.sleep(0.2)
    
    print("✅ Initial CAN setup completed.")

# New functions: separate actuator commands for each row.
def send_run_out_command_for_row(row, bus):
    """Send RUN_OUT command for the given actuator (row)."""
    # Map row to CAN ID: row 0 -> 0x22, row 1 -> 0x24, row 2 -> 0x26.
    can_id = {0: 0x22, 1: 0x24, 2: 0x26}[row]
    data = [0xE8, 0x03, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]
    send_message(bus, can_id, data, f"RUN_OUT_row{row+1}")

def send_run_in_command_for_row(row, bus):
    """Send RUN_IN command for the given actuator (row)."""
    can_id = {0: 0x22, 1: 0x24, 2: 0x26}[row]
    data = [0x02, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]
    send_message(bus, can_id, data, f"RUN_IN_row{row+1}")

# =============================================================================
# 4. GPS Data Processing and Actuator Triggering
# =============================================================================
# Global variables for GPS reference
initial_north = None
initial_east = None

def calculate_distance(north, east):
    """Calculate Euclidean distance in meters from the initial point."""
    delta_north = north - initial_north
    delta_east = east - initial_east
    return math.sqrt(delta_north**2 + delta_east**2)

def process_relative_position_frame(msg, bus):
    """Process the RelativePositionFrame, compute distance, and trigger actuators based on the weed map."""
    global initial_north, initial_east

    print("RELATIVE POSITION FRAME")
    print(f"North: {msg.relative_pose_north}")
    print(f"East: {msg.relative_pose_east}")
    print(f"Down: {msg.relative_pose_down}")

    if initial_north is None and initial_east is None:
        initial_north = msg.relative_pose_north
        initial_east = msg.relative_pose_east
        print(f"📍 Initial position set. North: {initial_north}, East: {initial_east}")
        log_event("INITIAL_POSITION", initial_north, initial_east, msg.relative_pose_down)
        return

    # Compute distance traveled (in meters)
    distance = calculate_distance(msg.relative_pose_north, msg.relative_pose_east)
    print(f"🚗 Distance traveled: {distance:.3f} m")
    log_event("GPS_FRAME", msg.relative_pose_north, msg.relative_pose_east, msg.relative_pose_down, distance)

    # Determine the current cell index based on distance.
    cell_index = int(distance // cell_length)
    if cell_index >= num_cells:
        cell_index = num_cells - 1  # cap at the last cell

    # For each actuator (row 0, 1, 2), check the weed map for the current cell.
    for row in range(num_rows):
        if cell_index not in triggered_states[row]:
            if weed_map[row, cell_index] == 1:
                # Weed is present → send RUN_OUT command.
                send_run_out_command_for_row(row, bus)
                triggered_states[row][cell_index] = "run_out"
            else:
                # Weed is absent → send RUN_IN command.
                send_run_in_command_for_row(row, bus)
                triggered_states[row][cell_index] = "run_in"

    print("-" * 50)

def process_gps_frame(msg):
    """Process a PVT (GPS) Frame and print details."""
    print("PVT FRAME")
    print(f"Latitude: {msg.latitude}")
    print(f"Longitude: {msg.longitude}")
    print(f"Altitude: {msg.altitude}")
    print(f"Ground speed: {msg.ground_speed}")
    print("-" * 50)

def process_ecef_frame(msg):
    """Process an ECEF frame and print details."""
    print("ECEF FRAME")
    print(f"x: {msg.x}")
    print(f"y: {msg.y}")
    print(f"z: {msg.z}")
    print("-" * 50)

# =============================================================================
# 5. Main Async Routine
# =============================================================================
async def main(service_config_path: Path) -> None:
    """Main routine: sets up CAN, sends initial commands, and processes GPS events."""
    config: EventServiceConfig = proto_from_json_file(service_config_path, EventServiceConfig())
    bus = setup_can_bus()

    # Send initial CAN configuration commands (SDO, NMT Start, Clear Error)
    send_initial_can_commands(bus)

    async for event, msg in EventClient(config).subscribe(config.subscriptions[0]):
        if isinstance(msg, gps_pb2.RelativePositionFrame):
            process_relative_position_frame(msg, bus)
        elif isinstance(msg, gps_pb2.GpsFrame):
            process_gps_frame(msg)
        elif isinstance(msg, gps_pb2.EcefCoordinates):
            process_ecef_frame(msg)

    bus.shutdown()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Amiga GPS stream + weed map triggered actuator commands (separate for each row) + CSV logging."
    )
    parser.add_argument("--service-config", type=Path, required=True, help="Path to GPS service config JSON.")
    args = parser.parse_args()
    asyncio.run(main(args.service_config))
