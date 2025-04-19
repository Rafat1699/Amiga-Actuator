#!/usr/bin/env python3
# Copyright (c) farm-ng, inc.
#
# Licensed under the Amiga Development Kit License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/farm-ng/amiga-dev-kit/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import argparse
import asyncio
from math import radians
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from farm_ng.core.event_client import EventClient
from farm_ng.core.event_service_pb2 import EventServiceConfig
from farm_ng.core.events_file_reader import proto_from_json_file
from farm_ng.filter.filter_pb2 import FilterState
from farm_ng.track.track_pb2 import Track
from farm_ng_core_pybind import Isometry3F64, Pose3F64
from google.protobuf.empty_pb2 import Empty
from track_planner import TrackBuilder

# Use the TkAgg backend (or Agg for headless)  
matplotlib.use("TkAgg")


def plot_track(waypoints: list[list[float]]) -> None:
    """
    Plot a 2D track with heading arrows, rotated +1° about the origin.
    """
    x = np.array(waypoints[0])
    y = np.array(waypoints[1])
    headings = np.array(waypoints[2])

    # Rotation angle: +1° in radians
    theta = np.radians(1.0)
    cos_t, sin_t = np.cos(theta), np.sin(theta)

    # Rotate all points
    x_rot =  cos_t * x - sin_t * y
    y_rot =  sin_t * x + cos_t * y

    # Rotate headings by same amount
    headings_rot = headings + theta

    # Components for arrows
    U = np.cos(headings_rot)
    V = np.sin(headings_rot)

    arrow_interval = 20
    turn_threshold = np.radians(10)

    plt.figure(figsize=(8, 8))
    plt.plot(x_rot, y_rot, color='orange', linewidth=1.0)

    for i in range(0, len(x_rot), arrow_interval):
        if i > 0:
            d_heading = abs(headings_rot[i] - headings_rot[i - 1])
        else:
            d_heading = 0
        if d_heading < turn_threshold:
            plt.quiver(
                x_rot[i], y_rot[i],
                U[i], V[i],
                angles='xy', scale_units='xy', scale=3.5, color='blue'
            )

    # Mark the start point
    plt.plot(x_rot[0], y_rot[0], marker='o', markersize=5, color='red')
    plt.axis("equal")

    # Legend
    legend_elements = [
        plt.Line2D([0], [0], color='orange', lw=2, label='Track'),
        plt.Line2D([0], [0], color='blue', lw=2, label='Heading'),
        plt.Line2D([0], [0], marker='o', color='w',
                   markerfacecolor='red', markersize=7, label='Start'),
    ]
    plt.legend(handles=legend_elements)
    plt.show()


async def create_start_pose(
    client: EventClient | None = None,
    timeout: float = 0.5
) -> Pose3F64:
    """Fetch the current filter pose or return a default."""
    print("Creating start pose...")
    zero_tangent = np.zeros((6, 1), dtype=np.float64)
    start = Pose3F64(
        a_from_b=Isometry3F64(),
        frame_a="world",
        frame_b="robot",
        tangent_of_b_in_a=zero_tangent
    )

    if client is not None:
        try:
            state: FilterState = await asyncio.wait_for(
                client.request_reply("/get_state", Empty(), decode=True),
                timeout=timeout
            )
            start = Pose3F64.from_proto(state.pose)
        except asyncio.TimeoutError:
            print("Timeout fetching filter state; using default pose.")
        except Exception as e:
            print(f"Error fetching filter state ({e}); using default pose.")

    return start


async def build_track(
    reverse: bool,
    client: EventClient | None = None,
    save_track: Path | None = None
) -> Track:
    """Construct and (optionally) save & plot a custom field-navigation track."""
    print("Building track...")
    # row spacing & length in meters
    row_spacing = 48 * 0.0254
    row_length = 32 * 12 * 0.0254

    start = await create_start_pose(client)
    tb = TrackBuilder(start=start)

    # Define segments & arcs per field layout:
    tb.create_straight_segment("goal1", distance=row_length, spacing=0.1)
    tb.create_arc_segment     ("goal2", radius=row_spacing, angle=radians(180), spacing=0.1)
    tb.create_straight_segment("goal3", distance=row_length, spacing=0.1)
    tb.create_arc_segment     ("goal4", radius=1.5 * row_spacing, angle=radians(180), spacing=0.1)
    

    if reverse:
        tb.reverse_track()

    print(f"Track created with {len(tb.track_waypoints)} waypoints")

    if save_track is not None:
        tb.save_track(save_track)
        print(f"Saved track proto to {save_track}")

    # Unpack & plot (rotated by +1° inside plot_track)
    waypoints = tb.unpack_track()
    plot_track(waypoints)

    return tb.track


async def run(args) -> None:
    """Entry point: parse flags, spin up the build_track task via asyncio."""
    save_path = args.save_track
    reverse   = args.reverse

    client: EventClient | None = None
    if args.service_config is not None:
        client = EventClient(
            proto_from_json_file(args.service_config, EventServiceConfig())
        )
        if client.config.name != "filter":
            raise RuntimeError(
                f"Expected filter service, got '{client.config.name}'"
            )

    task = asyncio.create_task(build_track(reverse, client, save_path))
    await asyncio.gather(task)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Amiga path planning + rotated plot example."
    )
    parser.add_argument(
        "--save-track",
        type=Path,
        help="Optional path to save the Track protobuf."
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="Whether to reverse the generated track."
    )
    parser.add_argument(
        "--service-config",
        type=Path,
        help="Path to your filter service config (JSON/proto)."
    )
    args = parser.parse_args()

    asyncio.run(run(args))
