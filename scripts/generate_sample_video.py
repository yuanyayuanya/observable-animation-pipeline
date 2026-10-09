from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np


def generate_video(path: Path, *, seconds: int = 6, fps: int = 12) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 640, 360
    writer = cv2.VideoWriter(
        str(path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        raise RuntimeError("OpenCV could not create the sample MP4")

    total_frames = seconds * fps
    try:
        for index in range(total_frames):
            frame = np.zeros((height, width, 3), dtype=np.uint8)
            frame[:] = (28, 34, 48)

            x = 40 + int((width - 140) * index / max(total_frames - 1, 1))
            y = 80 + int(45 * np.sin(index / 8))
            cv2.rectangle(frame, (x, y), (x + 96, y + 96), (68, 177, 125), -1)
            cv2.circle(frame, (width - x, 250), 42, (235, 166, 52), -1)
            cv2.putText(
                frame,
                f"Frame {index + 1:03d}",
                (24, 330),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.75,
                (238, 240, 244),
                2,
                cv2.LINE_AA,
            )
            writer.write(frame)
    finally:
        writer.release()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a small demo video")
    parser.add_argument("output", type=Path)
    parser.add_argument("--seconds", type=int, default=6)
    parser.add_argument("--fps", type=int, default=12)
    args = parser.parse_args()
    generate_video(args.output, seconds=args.seconds, fps=args.fps)
    print(args.output.resolve())


if __name__ == "__main__":
    main()

