import asyncio
import can
import csv
import os
import subprocess
import time
from datetime import datetime
import threading

CHANNEL = 'can0'
CSV_FILE = os.path.join(os.path.dirname(__file__), 'actuator_20_log.csv')
HEARTBEAT_SCRIPT = os.path.join(os.path.dirname(__file__), 'send_heartbeat.sh')

latest = {}

def log_row(command=None):
    with open(CSV_FILE, 'a', newline='') as csvfile:
        writer = csv.writer(csvfile)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        row = [now, command or "", latest.get("pdo_position_mm_22", ""), latest.get("pdo_speed_mms_22", "")]
        writer.writerow(row)

def call_heartbeat_script():
    try:
        subprocess.run([HEARTBEAT_SCRIPT], check=True)
        print("✅ Heartbeat script executed successfully.")
    except Exception as e:
        print(f"❌ Heartbeat script error: {e}")

def send_message(bus, arbitration_id, data, label=None):
    msg = can.Message(arbitration_id=arbitration_id, data=data, is_extended_id=False)
    try:
        bus.send(msg)
        print(f"📤 Sent {label}: {hex(arbitration_id)} {[hex(b) for b in data]}")
        log_row(command=label)
    except can.CanError as e:
        print(f"❌ CAN send error: {e}")

async def listen_actuator_pdo(bus):
    while True:
        msg = await asyncio.get_event_loop().run_in_executor(None, bus.recv)
        if msg.arbitration_id == 0x1A0:  # Actuator 20
            data = msg.data
            pos = int.from_bytes(data[0:2], 'little') * 0.1
            speed = int.from_bytes(data[5:7], 'little') * 0.1
            print(f"📡 PDO Feedback → Actuator 20 | Position: {pos:.1f} mm | Speed: {speed:.1f} mm/s")
            latest["pdo_position_mm_22"] = f"{pos:.1f}"
            latest["pdo_speed_mms_22"] = f"{speed:.1f}"
            log_row(command="PDO_FEEDBACK_20")

async def send_can_sequence():
    bus = can.interface.Bus(channel=CHANNEL, interface='socketcan')
    try:
        print("⚙️ Starting actuator sequence for ID 0x220...")

        send_message(bus, 0x620, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00], "SDO")
        time.sleep(0.2)

        send_message(bus, 0x000, [0x01, 0x20], "NMT Start")
        time.sleep(0.2)

        send_message(bus, 0x220, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00, 0x00], "Clear Error")
        time.sleep(0.2)

        send_message(bus, 0x220, [0x01, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00], "Run Out")
        time.sleep(5)

        print("⏳ Waiting 3 seconds before Run In...")
        time.sleep(3)

        send_message(bus, 0x220, [0x02, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00], "Run In")
        time.sleep(5)

        print("✅ Actuator 20 command sequence completed.")
    finally:
        bus.shutdown()

async def main():
    # Start heartbeat in a separate thread
    threading.Thread(target=call_heartbeat_script, daemon=True).start()

    # Delay to allow heartbeat to stabilize
    await asyncio.sleep(1)

    # Setup CAN bus
    bus = can.interface.Bus(channel=CHANNEL, interface='socketcan')

    # Start listening and sending concurrently
    await asyncio.gather(
        listen_actuator_pdo(bus),
        send_can_sequence()
    )

if __name__ == '__main__':
    # Prepare CSV with headers if not exists
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["Timestamp", "Command", "Position_mm", "Speed_mm/s"])
    asyncio.run(main())
