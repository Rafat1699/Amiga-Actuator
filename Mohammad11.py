import can
import csv
import time
import asyncio
from datetime import datetime
from pathlib import Path
from farm_ng.core.event_client import EventClient
from farm_ng.core.event_service_pb2 import EventServiceConfig
from farm_ng.core.events_file_reader import proto_from_json_file
from farm_ng.gps import gps_pb2

# === CSV Setup ===
csv_file = Path("gps_can_log.csv")
csv_headers = ["time_sec", "event_type", "x", "y", "vx", "vy", "sampling_time_sec", "actuator_id", "command", "pdo_position_mm", "pdo_speed_mms"]

# Track latest row values for consistent logging
latest = {
    "x": "", "y": "", "vx": "", "vy": "", "actuator_id": "", "command": "", "pdo_position_mm": "", "pdo_speed_mms": ""
}

start_time = time.time()
log_lock = asyncio.Lock()


def log_event(event_type, **kwargs):
    """Append row to CSV with combined latest values."""
    now = time.time()
    row = {
        "time_sec": f"{now - start_time:.3f}",
        "event_type": event_type,
        "sampling_time_sec": f"{now:.6f}",
    }
    row.update(latest)
    row.update(kwargs)

    # Update cache
    for k in latest:
        if k in kwargs:
            latest[k] = kwargs[k]

    # Write to CSV
    with open(csv_file, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_headers)
        writer.writerow(row)


# === CAN Setup ===
def setup_can_bus():
    return can.interface.Bus(channel='can0', interface='socketcan')


async def send_message_async(bus, arb_id, data, action=None):
    msg = can.Message(arbitration_id=arb_id, data=data, is_extended_id=False)
    try:
        bus.send(msg)
        print(f"✅ Sent: CAN ID {hex(arb_id)} Data {[hex(b) for b in data]}")
        log_event("CAN_SEND", actuator_id=arb_id, command=action)
    except can.CanError as e:
        print(f"❌ CAN error: {e}")


async def send_initial_can_commands(bus):
    print("🔧 Sending startup SDO, NMT, and Clear Error...")
    sdo = [
        (0x622, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00]),
    ]
    nmt = [(0x000, [0x01, 0x22])]
    clear = [(0x222, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00])]

    for arb_id, data in sdo + nmt + clear:
        await send_message_async(bus, arb_id, data, "SDO/NMT/CLR")
        await asyncio.sleep(0.2)

    print("✅ CAN startup complete.")


async def send_actuator_command_with_feedback(bus, actuator_id, action):
    arb_id = {22: 0x222, 24: 0x224, 26: 0x226}.get(actuator_id)
    if not arb_id:
        print("Invalid actuator ID.")
        return

    if action == 'open':
        run_cmd = [0xE8, 0x03, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]
    elif action == 'close':
        run_cmd = [0x02, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]
    else:
        print("Invalid action.")
        return

    await send_message_async(bus, arb_id, run_cmd, action)
    await asyncio.sleep(0.5)


# === GPS Processing ===
async def gps_streaming_task(service_config_path):
    config: EventServiceConfig = proto_from_json_file(service_config_path, EventServiceConfig())
    async for event, msg in EventClient(config).subscribe(config.subscriptions[0]):
        if isinstance(msg, gps_pb2.GpsFrame):
            x = msg.vel_north
            y = -msg.vel_east
            latest["vx"] = f"{x:.3f}"
            latest["vy"] = f"{y:.3f}"
            log_event("GPS_FRAME")
        elif isinstance(msg, gps_pb2.RelativePositionFrame):
            latest["x"] = f"{msg.relative_pose_north:.3f}"
            latest["y"] = f"{msg.relative_pose_east:.3f}"
            log_event("REL_POS_FRAME")


# === PDO Listener ===
async def listen_actuator_pdo(bus):
    while True:
        msg = await asyncio.get_event_loop().run_in_executor(None, bus.recv)
        if msg.arbitration_id == 0x1A2:
            data = msg.data

            # Position: bytes 0 (LSB), 1 (MSB)
            pos_raw = int.from_bytes(data[0:2], byteorder='little')
            pos_mm = pos_raw * 0.1

            # Speed: bytes 5 (LSB), 6 (MSB)
            speed_raw = int.from_bytes(data[5:7], byteorder='little')
            speed_mm_s = speed_raw * 0.1

            print(f"📡 PDO Feedback → Position: {pos_mm:.1f} mm | Speed: {speed_mm_s:.1f} mm/s")

            latest["pdo_position_mm"] = f"{pos_mm:.1f}"
            latest["pdo_speed_mms"] = f"{speed_mm_s:.1f}"
            log_event("PDO_FEEDBACK")


# === Actuator Control Demo ===
async def actuator_task(bus):
    await asyncio.sleep(5)  # Wait before sending
    await send_actuator_command_with_feedback(bus, 22, 'open')
    await asyncio.sleep(10)
    await send_actuator_command_with_feedback(bus, 22, 'close')


# === MAIN ===
async def main(gps_config_path):
    # Setup CSV
    if not csv_file.exists():
        with open(csv_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=csv_headers)
            writer.writeheader()

    bus = setup_can_bus()
    await send_initial_can_commands(bus)

    gps_task = asyncio.create_task(gps_streaming_task(gps_config_path))
    pdo_task = asyncio.create_task(listen_actuator_pdo(bus))
    act_task = asyncio.create_task(actuator_task(bus))

    await asyncio.gather(gps_task, pdo_task, act_task)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--gps_service_config", type=Path, required=True)
    args = parser.parse_args()

    asyncio.run(main(args.gps_service_config))
