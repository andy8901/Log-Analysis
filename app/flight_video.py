"""Renders a flight's 3D track (see flight3d.py) to a downloadable MP4 --
path flown so far + a simple attitude-indicating drone glyph (a rotated
'+' cross, oriented by real roll/pitch/yaw at each frame), server-side,
headless. Uses matplotlib's 3D plotting (no browser/WebGL needed) and
imageio-ffmpeg's bundled static ffmpeg binary (no system FFmpeg install
required -- important for a Windows office PC with no admin-installed
tooling)."""

import math

import imageio
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from mpl_toolkits.mplot3d.art3d import Line3DCollection

from app.flight3d import build_track, decimate_track


def _rotate(vec, axis, angle_deg):
    """Rodrigues' rotation formula: vec rotated by angle_deg around axis."""
    axis = axis / np.linalg.norm(axis)
    theta = math.radians(angle_deg)
    return (
        vec * math.cos(theta)
        + np.cross(axis, vec) * math.sin(theta)
        + axis * np.dot(axis, vec) * (1 - math.cos(theta))
    )


def _drone_glyph(center, roll, pitch, yaw, arm_length):
    """Four line segments (center -> each arm tip) for a simple '+'-shaped
    drone glyph at `center`, oriented by roll/pitch/yaw. Axis convention
    matches flight3d.build_track: x=east, y=up, z=north: yaw=0 faces
    north (+z), measured clockwise (compass convention, matching the
    ATT.Yaw values this is built from)."""
    up = np.array([0.0, 1.0, 0.0])
    yaw_r = math.radians(yaw)
    forward = np.array([math.sin(yaw_r), 0.0, math.cos(yaw_r)])
    right = np.array([math.cos(yaw_r), 0.0, -math.sin(yaw_r)])

    # Pitch: tilt forward (nose up/down) around the right axis.
    forward = _rotate(forward, right, pitch)
    up = _rotate(up, right, pitch)
    # Roll: tilt right (bank) around the (pitched) forward axis.
    right = _rotate(right, forward, roll)

    tips = [forward, -forward, right, -right]
    return [[center, center + arm_length * t] for t in tips]


def render_flight_video(report, output_path, fps=20, max_frames=240):
    """Writes an MP4 to output_path. Returns True on success, False if the
    log has no GPS track to render (nothing to show)."""
    track, meta = build_track(report)
    if not track:
        return False

    track = decimate_track(track, max_frames=max_frames)

    xs_full = [p["x"] for p in track]
    ys_full = [p["y"] for p in track]
    zs_full = [p["z"] for p in track]

    x_range = meta["x_range"]
    z_range = meta["z_range"]
    span = max(x_range[1] - x_range[0], z_range[1] - z_range[0], 10.0)
    arm_length = span * 0.025

    fig = plt.figure(figsize=(9, 6), dpi=120)
    ax = fig.add_subplot(111, projection="3d")

    def set_limits():
        cx = (x_range[0] + x_range[1]) / 2
        cz = (z_range[0] + z_range[1]) / 2
        half = span / 2 * 1.15
        ax.set_xlim(cx - half, cx + half)
        ax.set_zlim(z_range[0] - 2, z_range[1] + 2)  # mpl "z" axis = our y (altitude)
        ax.set_ylim(cz - half, cz + half)

    writer = imageio.get_writer(output_path, fps=fps, codec="libx264", quality=7)
    try:
        for i, point in enumerate(track):
            ax.clear()
            set_limits()
            ax.set_xlabel("East (m)")
            ax.set_ylabel("North (m)")
            ax.set_zlabel("Altitude (m)")
            ax.set_title("Flight Path (3D)")

            # mplot3d's 3rd axis is drawn vertically -- pass (x, z=north, y=altitude)
            ax.plot(xs_full[: i + 1], zs_full[: i + 1], ys_full[: i + 1],
                    color="#2a78d6", linewidth=1.8)
            ax.plot(xs_full, zs_full, ys_full, color="#c3c2b7", linewidth=0.6, alpha=0.4)

            segments = _drone_glyph(
                np.array([point["x"], point["y"], point["z"]]),
                point["roll"], point["pitch"], point["yaw"], arm_length,
            )
            # Reorder each segment's (x,y,z) -> (x, z, y) to match the plot axes above.
            plot_segments = [[[a[0], a[2], a[1]], [b[0], b[2], b[1]]] for a, b in segments]
            ax.add_collection3d(Line3DCollection(plot_segments, colors="#dc2626", linewidths=2.5))

            ax.view_init(elev=28, azim=-60 + i * 0.15)

            fig.canvas.draw()
            frame = np.asarray(fig.canvas.buffer_rgba())[:, :, :3]
            writer.append_data(frame)
    finally:
        writer.close()
        plt.close(fig)

    return True
