import can
import asyncio
import csv
from pathlib import Path
import time
from datetime import datetime

# Global variables
csv_file = Path(__file__).parent / "gps_can_log.csv"  # Same CSV file used by gps_stream.py
csv_headers = ["time_sec", "event_type", "x", "y", "vx", "vy", "sampling_time_sec", "actuator_id", "command"]

# Track the start time of the program
start_time = time.time()  # Record the start time (in seconds)

def log_event(event_type, actuator_id=None, command=None):
    """Log an event to the CSV file."""
    # Calculate the elapsed time in seconds since the start of the program
    elapsed_time = time.time() - start_time
    with open(csv_file, mode='a', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([f"{elapsed_time:.3f}",  # Elapsed time in seconds
                         event_type,
                         "", "", "", "", "", actuator_id if actuator_id else "", command if command else ""])

def setup_can_bus():
    """Initialize CAN bus."""
    return can.interface.Bus(channel='can0', interface='socketcan')

async def send_message_async(bus, arbitration_id, data, command=""):
    """Asynchronously send CAN message and log it."""
    msg = can.Message(arbitration_id=arbitration_id, data=data, is_extended_id=False)
    try:
        # Use run_in_executor for non-blocking send in Python 3.8
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

async def main():
    """Run the actuator control loop."""
    bus = setup_can_bus()

    # Send the initial setup commands (SDO, NMT, Clear Error)
    await send_initial_can_commands(bus)

    # Listen for actuator commands and send them
    while True:
        command_str = input("Enter actuator command (e.g., 22-open, 24-close) or 'exit' to quit: ")
        if command_str == 'exit':
            break
        # Run actuator commands asynchronously
        await send_actuator_command_async(bus, command_str)

    # Close CAN bus
    bus.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
