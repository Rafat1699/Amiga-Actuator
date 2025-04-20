import argparse
import asyncio
from pathlib import Path
import cv2
import numpy as np
from farm_ng.canbus.canbus_pb2 import Twist2d
from farm_ng.core.event_client import EventClient
from farm_ng.core.event_service_pb2 import EventServiceConfig
from farm_ng.core.events_file_reader import proto_from_json_file
from numpy import clip

# YOLO model files
YOLO_CONFIG = "yolov4-tiny.cfg"
YOLO_WEIGHTS = "yolov4-tiny.weights"
YOLO_CLASSES = "coco.names"

# Velocity parameters
MAX_LINEAR_VELOCITY_MPS = 0.5
MAX_ANGULAR_VELOCITY_RPS = 0.5
VELOCITY_INCREMENT = 0.05

# Load YOLO
net = cv2.dnn.readNet(YOLO_WEIGHTS, YOLO_CONFIG)

with open(YOLO_CLASSES, 'r') as f:
    CLASSES = [line.strip() for line in f.readlines()]


def detect_person(frame):
    height, width = frame.shape[:2]
    blob = cv2.dnn.blobFromImage(frame, 1/255.0, (416, 416), swapRB=True, crop=False)
    net.setInput(blob)
    outputs = net.forward(net.getUnconnectedOutLayersNames())

    for output in outputs:
        for detection in output:
            scores = detection[5:]
            class_id = np.argmax(scores)
            confidence = scores[class_id]
            if confidence > 0.5 and CLASSES[class_id] == "person":
                return True
    return False


def update_twist_with_key_press(twist: Twist2d, key: int):
    """Update twist based on keyboard input (WASD)."""
    if key == ord(" "):
        twist.linear_velocity_x = 0.0
        twist.angular_velocity = 0.0
    elif key == ord("w"):
        twist.linear_velocity_x += VELOCITY_INCREMENT
    elif key == ord("s"):
        twist.linear_velocity_x -= VELOCITY_INCREMENT
    elif key == ord("a"):
        twist.angular_velocity += VELOCITY_INCREMENT
    elif key == ord("d"):
        twist.angular_velocity -= VELOCITY_INCREMENT

    twist.linear_velocity_x = clip(twist.linear_velocity_x, -MAX_LINEAR_VELOCITY_MPS, MAX_LINEAR_VELOCITY_MPS)
    twist.angular_velocity = clip(twist.angular_velocity, -MAX_ANGULAR_VELOCITY_RPS, MAX_ANGULAR_VELOCITY_RPS)

    return twist


async def main(canbus_config_path: Path, camera_config_path: Path):
    # Load configs
    canbus_config = proto_from_json_file(canbus_config_path, EventServiceConfig())
    camera_config = proto_from_json_file(camera_config_path, EventServiceConfig())

    canbus_client = EventClient(canbus_config)
    camera_client = EventClient(camera_config)

    # Open camera feed window
    cv2.namedWindow("Camera Feed", cv2.WINDOW_NORMAL)

    # Initialize twists
    twist = Twist2d()
    previous_twist = Twist2d()

    # Start with robot stationary
    twist.linear_velocity_x = 0.0
    twist.angular_velocity = 0.0

    person_detected = False

    async for event, message in camera_client.subscribe(camera_config.subscriptions[0], decode=True):
        frame = cv2.imdecode(np.frombuffer(message.image_data, dtype="uint8"), cv2.IMREAD_UNCHANGED)

        # Show the camera feed
        cv2.imshow("Camera Feed", frame)

        if detect_person(frame):
            if not person_detected:
                print("⚠️ Person detected! Stopping the robot.")
                previous_twist = twist  # Save last movement before stopping
                twist.linear_velocity_x = 0.0
                twist.angular_velocity = 0.0
                await canbus_client.request_reply("/twist", twist)
                person_detected = True
        else:
            if person_detected:
                print("✅ Person left! Resuming previous velocity...")
                twist = previous_twist  # Resume the remembered speed
                await canbus_client.request_reply("/twist", twist)
                person_detected = False

            # Listen for manual key press even when person not detected
            key = cv2.waitKey(1)

            if key == ord("q"):
                print("Exiting control loop.")
                break

            if key in [ord("w"), ord("a"), ord("s"), ord("d"), ord(" ")]:
                twist = update_twist_with_key_press(twist, key)
                previous_twist = twist  # Update "last known twist" after every manual command

            await canbus_client.request_reply("/twist", twist)

        await asyncio.sleep(0.05)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Amiga person detection + auto-resume control.")
    parser.add_argument("--canbus-config", type=Path, required=True)
    parser.add_argument("--camera-config", type=Path, required=True)

    args = parser.parse_args()

    asyncio.run(main(args.canbus_config, args.camera_config))
