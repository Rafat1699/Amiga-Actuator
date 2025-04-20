import can
import csv
import time
import asyncio
from pathlib import Path
from farm_ng.core.event_client import EventClient
from farm_ng.core.event_service_pb2 import EventServiceConfig
from farm_ng.core.events_file_reader import proto_from_json_file
from farm_ng.gps import gps_pb2

# === CSV Setup ===
csv_file = Path("gps_can_log.csv")
csv_headers = [
    "time_sec", "event_type",
    "x", "y", "vx", "vy",
    "sampling_time_sec",
    "actuator_id", "command",
    "pdo_position_mm", "pdo_speed_mms"
]

# Shared cache of the last‑seen values (as strings for CSV)
latest = {
    "x": "", "y": "",
    "vx": "", "vy": "",
    "actuator_id": "", "command": "",
    "pdo_position_mm": "", "pdo_speed_mms": ""
}

start_time = time.time()

def log_event(event_type, **kwargs):
    """Append a new row to the CSV using the combined latest values."""
    now = time.time()
    row = {
        "time_sec": f"{now - start_time:.3f}",
        "event_type": event_type,
        "sampling_time_sec": f"{now:.6f}",
    }
    # first fill in whatever was in latest, then overwrite with any new kwargs
    row.update(latest)
    row.update(kwargs)

    # update the shared cache
    for k in latest:
        if k in kwargs:
            latest[k] = kwargs[k]

    # write out
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
        log_event("CAN_SEND", actuator_id=hex(arb_id), command=action)
    except can.CanError as e:
        print(f"❌ CAN error: {e}")


async def send_initial_can_commands(bus):
    print("🔧 Sending startup SDO, NMT, and Clear Error...")
    sdo = [(0x622, [0x23, 0x16, 0x10, 0x01, 0xC8, 0x00, 0x01, 0x00])]
    nmt = [(0x000, [0x01, 0x22])]
    clear = [(0x222, [0x00, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00])]

    for arb_id, data in sdo + nmt + clear:
        await send_message_async(bus, arb_id, data, "SDO/NMT/CLR")
        await asyncio.sleep(0.2)

    print("✅ CAN startup complete.")


async def send_actuator_command_with_feedback(bus, actuator_id, action):
    arb_id_map = {22: 0x222, 24: 0x224, 26: 0x226}
    arb_id = arb_id_map.get(actuator_id)
    if arb_id is None:
        print("Invalid actuator ID.")
        return

    if action == 'open':
        cmd = [0xE8, 0x03, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]
    elif action == 'close':
        cmd = [0x02, 0xFB, 0xFB, 0xFB, 0xFB, 0xFB, 0x00, 0x00]
    else:
        print("Invalid action.")
        return

    await send_message_async(bus, arb_id, cmd, action)
    await asyncio.sleep(0.5)


# === GPS Processing ===
async def gps_streaming_task(service_config_path):
    config: EventServiceConfig = proto_from_json_file(
        service_config_path, EventServiceConfig()
    )
    async for _, msg in EventClient(config).subscribe(config.subscriptions[0]):
        if isinstance(msg, gps_pb2.GpsFrame):
            vx = msg.vel_north
            vy = -msg.vel_east
            latest["vx"] = f"{vx:.3f}"
            latest["vy"] = f"{vy:.3f}"
            log_event("GPS_FRAME")
        elif isinstance(msg, gps_pb2.RelativePositionFrame):
            latest["x"] = f"{msg.relative_pose_north:.3f}"
            latest["y"] = f"{msg.relative_pose_east:.3f}"
            log_event("REL_POS_FRAME")


# === PDO Listener ===
async def listen_actuator_pdo(bus):
    loop = asyncio.get_event_loop()
    while True:
        msg = await loop.run_in_executor(None, bus.recv)
        if msg and msg.arbitration_id == 0x1A2:
            data = msg.data
            pos_raw = int.from_bytes(data[0:2], byteorder='little')
            speed_raw = int.from_bytes(data[5:7], byteorder='little')
            pos_mm = pos_raw * 0.1
            speed_mm_s = speed_raw * 0.1

            print(f"📡 PDO Feedback → Position: {pos_mm:.1f} mm | Speed: {speed_mm_s:.1f} mm/s")

            latest["pdo_position_mm"] = f"{pos_mm:.1f}"
            latest["pdo_speed_mms"]   = f"{speed_mm_s:.1f}"
            log_event("PDO_FEEDBACK")


# === Actuator Control Demo ===
async def actuator_task(bus):
    """Continuously read the latest 'y' and trigger open/close."""
    while True:
        y_str = latest.get("y", "")
        if y_str:
            try:
                y_val = float(y_str)
                # if |y| > 5 → close first; elif |y| > 2 → open
                if abs(y_val) > 5:
                    await send_actuator_command_with_feedback(bus, 22, 'close')
                    await send_actuator_command_with_feedback(bus, 26, 'close')
                elif abs(y_val) > 2:
                    await send_actuator_command_with_feedback(bus, 22, 'open')
            except ValueError:
                # still initializing or bad data
                pass
        await asyncio.sleep(0.1)


# === MAIN ===
async def main(gps_config_path):
    # write header if file doesn't exist
    if not csv_file.exists():
        with open(csv_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=csv_headers)
            writer.writeheader()

    bus = setup_can_bus()
    await send_initial_can_commands(bus)

    tasks = [
        asyncio.create_task(gps_streaming_task(gps_config_path)),
        asyncio.create_task(listen_actuator_pdo(bus)),
        asyncio.create_task(actuator_task(bus)),
    ]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--gps_service_config", type=Path, required=True)
    args = parser.parse_args()

    asyncio.run(main(args.gps_service_config))
