from __future__ import annotations

import hashlib
import json
import math
import time
import zipfile
from pathlib import Path
from typing import Any

import cv2
from PIL import Image, ImageDraw, ImageFont

from app.domain.contracts import (
    ArtifactInput,
    ProducedArtifact,
    StepContext,
    StepResult,
)
from app.domain.errors import (
    ArtifactValidationError,
    PathBoundaryError,
    PermanentInputError,
    PipelineError,
    RetryableStepError,
    StepTimeoutError,
)
from app.pipeline.artifacts import ArtifactStore
from app.pipeline.faults import FaultPlan, normalize_fault_plan


VIDEO_KINDS = {"source_video", "video", "upload"}
FRAME_KIND = "frame"
ANALYSIS_KIND = "analysis"


class InjectedPermanentStepError(PipelineError):
    """A non-retryable test failure requested by the public API fault plan."""


def _report(context: StepContext, progress: int, message: str) -> None:
    context.report_progress(max(0, min(100, int(progress))), message)


def _consume_fault_once(
    context: StepContext,
    store: ArtifactStore,
    fault: FaultPlan,
) -> bool:
    token = hashlib.sha256(
        f"{context.task_id}:{context.step_key}:{fault.identity}".encode("utf-8")
    ).hexdigest()
    marker = store.require_runtime_path(
        store.temp_root / "faults" / f"{token}.used"
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open("x", encoding="ascii") as handle:
            handle.write(context.attempt_id)
    except FileExistsError:
        return False
    return True


def _store(context: StepContext) -> ArtifactStore:
    store = ArtifactStore(context.runtime_root)
    staging = store.require_runtime_path(context.staging_dir)
    try:
        staging.relative_to(store.staging_root)
    except ValueError as error:
        raise PathBoundaryError("step staging directory is outside staging root") from error
    staging.mkdir(parents=True, exist_ok=True)
    return store


def _input_path(store: ArtifactStore, artifact: ArtifactInput) -> Path:
    path = store.require_runtime_path(artifact.path)
    if not path.is_file():
        raise ArtifactValidationError(f"input artifact is missing: {path}")
    return path


def _first_input(
    context: StepContext,
    store: ArtifactStore,
    kinds: set[str],
) -> tuple[ArtifactInput, Path]:
    for artifact in context.input_artifacts:
        if artifact.kind in kinds:
            return artifact, _input_path(store, artifact)
    raise ArtifactValidationError(f"missing input artifact of kind: {sorted(kinds)}")


def _inputs(
    context: StepContext,
    store: ArtifactStore,
    kind: str,
) -> list[tuple[ArtifactInput, Path]]:
    values = [
        (artifact, _input_path(store, artifact))
        for artifact in context.input_artifacts
        if artifact.kind == kind
    ]
    return sorted(values, key=lambda value: value[1].as_posix())


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class ValidateInputStep:
    key = "validate_input"
    version = "1"
    max_attempts = 1

    def execute(self, context: StepContext) -> StepResult:
        store = _store(context)
        source_artifact, source = _first_input(context, store, VIDEO_KINDS)
        _report(context, 5, "checking input path and extension")
        if source.suffix.lower() not in {".mp4", ".mov"}:
            raise PermanentInputError("only .mp4 and .mov videos are supported")

        size_bytes = source.stat().st_size
        max_bytes = int(context.params.get("max_upload_bytes", 50 * 1024 * 1024))
        if size_bytes <= 0 or size_bytes > max_bytes:
            raise PermanentInputError(f"video size must be between 1 and {max_bytes} bytes")

        _report(context, 25, "opening video")
        capture = cv2.VideoCapture(str(source))
        try:
            if not capture.isOpened():
                raise PermanentInputError("OpenCV could not open the video")
            fps = float(capture.get(cv2.CAP_PROP_FPS))
            frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            readable, _ = capture.read()
        finally:
            capture.release()
        if not readable or frame_count <= 0:
            raise PermanentInputError("video does not contain a readable frame")
        if not math.isfinite(fps) or fps <= 0:
            raise PermanentInputError("video has invalid FPS metadata")
        duration_seconds = frame_count / fps
        max_seconds = float(context.params.get("max_video_seconds", 20))
        if duration_seconds > max_seconds:
            raise PermanentInputError(f"video duration exceeds {max_seconds:g} seconds")

        _report(context, 70, "computing source checksum")
        actual_sha256 = store.sha256_file(source)
        metadata = {
            "duration_seconds": round(duration_seconds, 3),
            "extension": source.suffix.lower(),
            "fps": round(fps, 3),
            "frame_count": frame_count,
            "height": height,
            "sha256": actual_sha256,
            "size_bytes": size_bytes,
            "width": width,
        }
        output = context.staging_dir / "input_metadata.json"
        _write_json(output, metadata)
        _report(context, 100, "input validated")
        return StepResult(
            artifacts=(
                ProducedArtifact(
                    kind="input_metadata",
                    relative_path="input_metadata.json",
                    media_type="application/json",
                    metadata={"source_sha256_matches": actual_sha256 == source_artifact.sha256},
                ),
            ),
            metrics={
                "duration_seconds": round(duration_seconds, 3),
                "frame_count": frame_count,
                "size_bytes": size_bytes,
            },
        )


class ExtractFramesStep:
    key = "extract_frames"
    version = "1"
    max_attempts = 2

    def execute(self, context: StepContext) -> StepResult:
        store = _store(context)
        _, source = _first_input(context, store, VIDEO_KINDS)
        requested = int(context.params.get("frame_count", 8))
        if not 4 <= requested <= 12:
            raise PermanentInputError("frame_count must be between 4 and 12")

        capture = cv2.VideoCapture(str(source))
        try:
            if not capture.isOpened():
                raise RetryableStepError("OpenCV could not reopen the video")
            total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            if total <= 0:
                raise RetryableStepError("video frame count is unavailable")
            positions = [round(index * (total - 1) / max(requested - 1, 1)) for index in range(requested)]
            outputs: list[ProducedArtifact] = []
            frame_manifest: list[dict[str, int | str]] = []
            frames_dir = context.staging_dir / "frames"
            frames_dir.mkdir(parents=True, exist_ok=True)
            for output_index, source_index in enumerate(positions, start=1):
                capture.set(cv2.CAP_PROP_POS_FRAMES, source_index)
                readable, frame = capture.read()
                if not readable:
                    raise RetryableStepError(f"could not decode source frame {source_index}")
                relative = f"frames/frame-{output_index:03d}.jpg"
                destination = context.staging_dir / relative
                if not cv2.imwrite(str(destination), frame, [cv2.IMWRITE_JPEG_QUALITY, 88]):
                    raise RetryableStepError(f"could not write {relative}")
                outputs.append(
                    ProducedArtifact(
                        kind=FRAME_KIND,
                        relative_path=relative,
                        media_type="image/jpeg",
                        metadata={"sequence": output_index, "source_frame": source_index},
                    )
                )
                frame_manifest.append(
                    {"path": relative, "sequence": output_index, "source_frame": source_index}
                )
                _report(context, output_index * 90 // requested, f"extracted frame {output_index}/{requested}")
        finally:
            capture.release()

        _write_json(context.staging_dir / "frames_manifest.json", {"frames": frame_manifest})
        outputs.append(
            ProducedArtifact(
                kind="frames_manifest",
                relative_path="frames_manifest.json",
                media_type="application/json",
            )
        )
        _report(context, 100, "frame extraction complete")
        return StepResult(artifacts=tuple(outputs), metrics={"frames_extracted": requested})


class AnalyzeFramesStep:
    key = "analyze_frames"
    version = "1"
    max_attempts = 3
    _labels = ("character", "motion", "bright", "dark", "warm", "cool")

    def execute(self, context: StepContext) -> StepResult:
        store = _store(context)
        frames = _inputs(context, store, FRAME_KIND)
        if not frames:
            raise ArtifactValidationError("analyze_frames requires extracted frame artifacts")
        profile = str(context.params.get("analysis_profile", "balanced"))
        delay = max(0.0, float(context.params.get("mock_delay_seconds_per_frame", 0.0)))
        fault = normalize_fault_plan(context.params.get("fault_plan"), self.key)
        threshold = fault.at_progress if fault else 101
        analyses: list[dict[str, Any]] = []

        for index, (artifact, path) in enumerate(frames, start=1):
            if delay:
                time.sleep(delay)
            digest = hashlib.sha256(path.read_bytes() + profile.encode("utf-8")).digest()
            labels = [
                self._labels[digest[0] % len(self._labels)],
                self._labels[digest[1] % len(self._labels)],
            ]
            labels = list(dict.fromkeys(labels))
            analyses.append(
                {
                    "frame": path.name,
                    "labels": labels,
                    "profile": profile,
                    "score": round(0.5 + digest[2] / 510, 4),
                    "source_sha256": artifact.sha256,
                }
            )
            progress = index * 95 // len(frames)
            _report(context, progress, f"analyzed frame {index}/{len(frames)}")
            if fault and progress >= threshold and _consume_fault_once(
                context, store, fault
            ):
                mode = fault.mode.lower()
                if mode in {"temporary_failure_once", "retryable_once"}:
                    raise RetryableStepError("injected one-time temporary analysis failure")
                if mode in {"timeout_once", "timeout_signal_once"}:
                    raise StepTimeoutError("injected one-time analysis timeout signal")

        output = context.staging_dir / "analysis.json"
        _write_json(output, {"frames": analyses, "profile": profile})
        _report(context, 100, "mock analysis complete")
        return StepResult(
            artifacts=(
                ProducedArtifact(
                    kind=ANALYSIS_KIND,
                    relative_path="analysis.json",
                    media_type="application/json",
                    metadata={"profile": profile},
                ),
            ),
            metrics={"frames_analyzed": len(analyses), "profile": profile},
        )

class BuildContactSheetStep:
    key = "build_contact_sheet"
    version = "1"
    max_attempts = 2

    def execute(self, context: StepContext) -> StepResult:
        store = _store(context)
        fault = normalize_fault_plan(context.params.get("fault_plan"), self.key)
        if (
            fault
            and fault.mode.lower() == "permanent_fail"
            and _consume_fault_once(context, store, fault)
        ):
            _report(context, fault.at_progress, "injecting permanent contact sheet failure")
            raise InjectedPermanentStepError("injected permanent contact sheet failure")
        frames = _inputs(context, store, FRAME_KIND)
        _, analysis_path = _first_input(context, store, {ANALYSIS_KIND})
        if not frames:
            raise ArtifactValidationError("build_contact_sheet requires frame artifacts")
        try:
            analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ArtifactValidationError("analysis artifact is not valid JSON") from error
        by_name = {item["frame"]: item for item in analysis.get("frames", [])}

        cell_width, image_height, caption_height = 320, 180, 54
        columns = min(4, len(frames))
        rows = math.ceil(len(frames) / columns)
        sheet = Image.new("RGB", (columns * cell_width, rows * (image_height + caption_height)), "white")
        draw = ImageDraw.Draw(sheet)
        font = ImageFont.load_default()
        for index, (_, frame_path) in enumerate(frames):
            row, column = divmod(index, columns)
            x, y = column * cell_width, row * (image_height + caption_height)
            try:
                with Image.open(frame_path) as image:
                    tile = image.convert("RGB")
                    tile.thumbnail((cell_width, image_height))
                    offset = (x + (cell_width - tile.width) // 2, y + (image_height - tile.height) // 2)
                    sheet.paste(tile, offset)
            except OSError as error:
                raise ArtifactValidationError(f"cannot read frame image: {frame_path.name}") from error
            item = by_name.get(frame_path.name, {})
            labels = ", ".join(item.get("labels", [])) or "no labels"
            score = item.get("score", "n/a")
            draw.text((x + 8, y + image_height + 8), frame_path.name, fill="black", font=font)
            draw.text((x + 8, y + image_height + 26), f"{labels} | {score}", fill="#333333", font=font)
            _report(context, (index + 1) * 90 // len(frames), f"placed frame {index + 1}/{len(frames)}")

        output = context.staging_dir / "contact-sheet.jpg"
        sheet.save(output, format="JPEG", quality=88, optimize=True)
        _report(context, 100, "contact sheet complete")
        return StepResult(
            artifacts=(
                ProducedArtifact(
                    kind="contact_sheet",
                    relative_path="contact-sheet.jpg",
                    media_type="image/jpeg",
                    metadata={"columns": columns, "rows": rows},
                ),
            ),
            metrics={"frames_rendered": len(frames), "width": sheet.width, "height": sheet.height},
        )


class PackageResultStep:
    key = "package_result"
    version = "1"
    max_attempts = 2

    def execute(self, context: StepContext) -> StepResult:
        store = _store(context)
        source_files: list[tuple[ArtifactInput, Path]] = []
        for artifact in context.input_artifacts:
            source_files.append((artifact, _input_path(store, artifact)))
        if not source_files:
            raise ArtifactValidationError("package_result requires input artifacts")
        source_files.sort(key=lambda value: (value[0].kind, value[1].as_posix()))

        manifest_entries = [
            {
                "kind": artifact.kind,
                "name": path.name,
                "sha256": artifact.sha256,
                "size_bytes": path.stat().st_size,
            }
            for artifact, path in source_files
        ]
        manifest = {
            "artifacts": manifest_entries,
            "pipeline": "observable-animation-pipeline",
            "task_id": context.task_id,
        }
        manifest_path = context.staging_dir / "manifest.json"
        _write_json(manifest_path, manifest)

        archive_path = context.staging_dir / "result.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            self._write_deterministic(archive, "manifest.json", manifest_path.read_bytes())
            for index, (artifact, path) in enumerate(source_files, start=1):
                archive_name = self._archive_name(artifact, path, index)
                self._write_deterministic(archive, archive_name, path.read_bytes())
                _report(context, index * 90 // len(source_files), f"packaged artifact {index}/{len(source_files)}")
        _report(context, 100, "result package complete")
        return StepResult(
            artifacts=(
                ProducedArtifact(
                    kind="manifest",
                    relative_path="manifest.json",
                    media_type="application/json",
                ),
                ProducedArtifact(
                    kind="result_package",
                    relative_path="result.zip",
                    media_type="application/zip",
                ),
            ),
            metrics={"files_packaged": len(source_files) + 1},
        )

    @staticmethod
    def _archive_name(artifact: ArtifactInput, path: Path, index: int) -> str:
        safe_kind = "".join(char if char.isalnum() or char in "-_" else "_" for char in artifact.kind)
        return f"artifacts/{index:03d}-{safe_kind}/{path.name}"

    @staticmethod
    def _write_deterministic(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, content)
