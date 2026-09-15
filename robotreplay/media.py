"""Bounded child-process video processing; no external model or network access."""

import argparse
import json
import math
import os
import subprocess
from pathlib import Path

import cv2
import imageio_ffmpeg
import numpy as np


def transcode(source: Path, target: Path):
    args = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        "scale='min(1280,iw)':-2",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "26",
        "-threads",
        "1",
        "-movflags",
        "+faststart",
        str(target),
    ]
    result = subprocess.run(args, capture_output=True, timeout=60, check=False)
    if result.returncode:
        raise ValueError("transcode_failed")


def inspect(source: Path, max_duration=180.0):
    with source.open("rb") as stream:
        header = stream.read(32)
    supported = (
        (len(header) >= 12 and header[4:8] == b"ftyp")
        or (header[:4] == b"RIFF" and header[8:12] == b"AVI ")
        or header[:4] == b"\x1aE\xdf\xa3"
    )
    if not supported:
        raise ValueError("invalid_video")
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "protocol_whitelist;file,pipe|enable_drefs;0"
    cap = cv2.VideoCapture(str(source), cv2.CAP_FFMPEG)
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        frames = float(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width, height = (
            int(cap.get(k)) for k in (cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT)
        )
        if not cap.isOpened() or not all(
            math.isfinite(v) and v > 0 for v in (fps, frames, width, height)
        ):
            raise ValueError("invalid_video")
        duration = frames / fps
        if duration > max_duration or width * height > 3840 * 2160 or fps > 120:
            raise ValueError("video_limits")
        return {"fps": fps, "width": width, "height": height, "duration": duration}
    finally:
        cap.release()


def analyze(source: Path, directory: Path, max_duration=180.0):
    cv2.setNumThreads(1)
    inspect(source, max_duration)
    target = directory / "playback.mp4"
    transcode(source, target)
    metadata = inspect(target, max_duration)
    cap = cv2.VideoCapture(str(target))
    samples, evidence = [], []
    previous_gray = None
    step = max(1, round(metadata["fps"] / 5))
    frame_index = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if frame_index % step != 0:
                frame_index += 1
                continue
            t = round(frame_index / metadata["fps"], 3)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            motion = (
                0.0
                if previous_gray is None
                else float(np.mean(cv2.absdiff(gray, previous_gray)) / 255)
            )
            previous_gray = gray
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, np.array([35, 100, 70]), np.array([85, 255, 255]))
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            valid = [c for c in contours if cv2.contourArea(c) >= 40]
            x = y = None
            # Multiple green objects are ambiguous, not proof of robot identity.
            visible = len(valid) == 1
            if visible:
                moments = cv2.moments(valid[0])
                x = moments["m10"] / moments["m00"] / metadata["width"]
                y = moments["m01"] / moments["m00"] / metadata["height"]
            samples.append({"t": t, "x": x, "y": y, "visible": visible, "motion": motion})
            frame_index += 1
    finally:
        cap.release()
    if not samples:
        raise ValueError("empty_video")
    gap_start, last_heading, stopped, last_turn = None, None, False, -10.0
    for i, sample in enumerate(samples):
        t = sample["t"]
        if not sample["visible"]:
            gap_start = t if gap_start is None else gap_start
            last_heading, stopped = None, False
            continue
        if gap_start is not None:
            evidence.append(
                {
                    "t": gap_start,
                    "end_t": t,
                    "kind": "visibility_gap",
                    "text": "No unique green marker is visible in this interval.",
                }
            )
            gap_start = None
        if i == 0 or not samples[i - 1]["visible"]:
            continue
        previous = samples[i - 1]
        dx, dy = sample["x"] - previous["x"], sample["y"] - previous["y"]
        speed = math.hypot(dx, dy) / max(0.01, t - previous["t"])
        if speed < 0.015:
            if not stopped:
                evidence.append(
                    {
                        "t": t,
                        "end_t": t,
                        "kind": "stop_candidate",
                        "text": "The visible green marker has little image-plane motion.",
                    }
                )
            stopped = True
            continue
        stopped = False
        heading = math.atan2(dy, dx)
        if last_heading is not None:
            angle = abs(
                math.atan2(math.sin(heading - last_heading), math.cos(heading - last_heading))
            )
            if angle > math.radians(40) and t - last_turn >= 1.0:
                evidence.append(
                    {
                        "t": t,
                        "end_t": t,
                        "kind": "turn_candidate",
                        "text": "The visible green marker changes direction in the image.",
                    }
                )
                last_turn = t
        last_heading = heading
    if gap_start is not None:
        evidence.append(
            {
                "t": gap_start,
                "end_t": metadata["duration"],
                "kind": "visibility_gap",
                "text": "No unique green marker is visible in this interval.",
            }
        )
    if any(s["visible"] for s in samples):
        first = next(s["t"] for s in samples if s["visible"])
        evidence.insert(
            0,
            {
                "t": first,
                "end_t": first,
                "kind": "marker_visible",
                "text": "One green marker is visible; confirm it belongs to the robot.",
            },
        )
    metadata["coverage"] = sum(s["visible"] for s in samples) / len(samples)
    return {
        "metadata": metadata,
        "samples": samples,
        "evidence": evidence,
        "algorithm": "green-marker-v1",
    }


def make_demo(target: Path, obscured=False):
    """Original generated footage, no children, outside footage, or VEX field claims."""
    target.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(target), cv2.VideoWriter_fourcc(*"mp4v"), 15, (640, 360))
    if not writer.isOpened():
        raise RuntimeError("demo_encoder_unavailable")
    try:
        for i in range(180):
            t = i / 15
            frame = np.full((360, 640, 3), (38, 43, 46), np.uint8)
            for x in range(40, 640, 40):
                cv2.line(frame, (x, 50), (x, 330), (55, 61, 63), 1)
            for y in range(50, 340, 40):
                cv2.line(frame, (40, y), (600, y), (55, 61, 63), 1)
            if t < 4:
                x, y = 80 + t * 65, 280
            elif t < 8:
                x, y = 340, 280 - (t - 4) * 45
            elif t < 10:
                x, y = 340 + (t - 8) * 90, 100
            else:
                x, y = 520, 100
            if not (obscured and 5 <= t <= 7):
                cv2.rectangle(
                    frame,
                    (int(x) - 18, int(y) - 13),
                    (int(x) + 18, int(y) + 13),
                    (105, 110, 116),
                    -1,
                )
                cv2.circle(frame, (int(x), int(y)), 8, (50, 225, 70), -1)
            cv2.putText(
                frame,
                "RobotReplay / generated practice drill",
                (24, 28),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (220, 225, 225),
                1,
            )
            cv2.putText(
                frame, f"{t:04.1f}s", (550, 340), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 225, 225), 1
            )
            writer.write(frame)
    finally:
        writer.release()


def child_main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--max-duration", type=float, default=180)
    args = parser.parse_args()
    if os.name == "posix":
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (80, 85))
    try:
        result = analyze(args.source, args.directory, args.max_duration)
        (args.directory / "analysis.json").write_text(json.dumps(result, allow_nan=False))
    except Exception as exc:
        allowed = {"invalid_video", "video_limits", "transcode_failed", "empty_video"}
        code = str(exc) if str(exc) in allowed else "analysis_failed"
        (args.directory / "error.json").write_text(json.dumps({"error_code": code}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    child_main()
