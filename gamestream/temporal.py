"""Backward optical-flow guides for a single, chronological capture stream."""
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True, slots=True)
class TemporalFrame:
    motion: np.ndarray
    reset: bool
    residual: float


class TemporalGuide:
    """Estimate current-to-previous displacement in input pixels, stored as FP16.

    A cut is detected from motion-compensated error, so an ordinary camera pan
    does not repeatedly erase history. Capture cannot supply engine depth or
    motion; optical flow is an approximation, not reconstructed engine data.
    """
    def __init__(self, width: int, height: int, flow_width: int = 480):
        self.width, self.height = width, height
        scale = min(1.0, flow_width / width)
        self.size = (max(16, round(width * scale)), max(16, round(height * scale)))
        self.zero = np.zeros((height, width, 2), dtype=np.float16)
        self.previous = None
        # Bound CPU use; the stream already has capture/encode/render threads.
        cv2.setNumThreads(1)
        self.flow = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_FAST)
        self.flow.setUseSpatialPropagation(True)
        y, x = np.mgrid[:self.size[1], :self.size[0]]
        self.grid = np.stack((x, y), axis=-1).astype(np.float32)

    def reset(self):
        self.previous = None

    def process(self, rgba: np.ndarray) -> TemporalFrame:
        gray = cv2.cvtColor(rgba, cv2.COLOR_RGBA2GRAY)
        current = cv2.resize(gray, self.size, interpolation=cv2.INTER_AREA)
        previous, self.previous = self.previous, current
        if previous is None:
            return TemporalFrame(self.zero, True, 1.0)
        if np.array_equal(current, previous):
            return TemporalFrame(self.zero, False, 0.0)
        backward = self.flow.calc(current, previous, None)
        if not np.isfinite(backward).all():
            return TemporalFrame(self.zero, True, 1.0)
        coordinates = self.grid + backward
        warped = cv2.remap(previous, coordinates, None, cv2.INTER_LINEAR,
                           borderMode=cv2.BORDER_REPLICATE)
        valid = ((coordinates[..., 0] >= 0) & (coordinates[..., 0] < self.size[0] - 1)
                 & (coordinates[..., 1] >= 0) & (coordinates[..., 1] < self.size[1] - 1))
        error = cv2.absdiff(current, warped)
        residual = float(error[valid].mean()) / 255 if valid.any() else 1.0
        # Similar average brightness does not imply the same scene. Detect
        # unrelated texture after compensation as well as large luminance cuts.
        correlation = 1.0
        if residual > 0.04 and valid.any():
            a = current[valid].astype(np.float32)
            b = warped[valid].astype(np.float32)
            a -= a.mean()
            b -= b.mean()
            energy = float(np.sqrt(np.dot(a, a) * np.dot(b, b)))
            correlation = float(np.dot(a, b)) / energy if energy > 1 else 0.0
        if residual > 0.18 or (residual > 0.04 and correlation < 0.3) or float(valid.mean()) < 0.5:
            return TemporalFrame(self.zero, True, residual)
        backward[..., 0] *= self.width / self.size[0]
        backward[..., 1] *= self.height / self.size[1]
        motion = cv2.resize(backward, (self.width, self.height), interpolation=cv2.INTER_LINEAR)
        # OpenCV uses hardware FP16 conversion where available; NumPy's generic
        # cast adds substantial CPU latency at 1440p/4K. convertFp16 returns the
        # half-float bit pattern in int16 storage, so reinterpret (do not cast).
        half = cv2.convertFp16(motion).view(np.float16)
        return TemporalFrame(half, False, residual)


class StaticRegionStabilizer:
    """Bounded output smoothing only where source RGB is exactly unchanged.

    Never warps an image or supplies estimated motion/history to NGX. Changed
    pixels and an eight-input-pixel border use the current independent render.
    Two unchanged comparisons are required before history contributes again.
    """
    def __init__(self):
        self.reset()
        self.kernel = np.ones((17, 17), np.uint8)

    def reset(self):
        self.previous_source = None
        self.previous_output = None
        self.previous_static = None

    def process(self, source: np.ndarray, output: np.ndarray) -> np.ndarray:
        if (self.previous_source is None or self.previous_source.shape != source.shape
                or self.previous_output.shape != output.shape):
            self.previous_source = source.copy()
            self.previous_output = output.copy()
            self.previous_static = None
            return output
        diff = cv2.absdiff(source, self.previous_source)
        unchanged = cv2.inRange(diff, (0, 0, 0, 0), (0, 0, 0, 255))
        unchanged = cv2.erode(unchanged, self.kernel, borderType=cv2.BORDER_CONSTANT, borderValue=0)
        safe = (cv2.bitwise_and(unchanged, self.previous_static)
                if self.previous_static is not None else np.zeros_like(unchanged))
        result = output
        if cv2.countNonZero(safe):
            if safe.shape != output.shape[:2]:
                safe = cv2.resize(safe, (output.shape[1], output.shape[0]), interpolation=cv2.INTER_NEAREST)
            smoothed = cv2.addWeighted(self.previous_output, .75, output, .25, 0)
            # A distant scene change can alter neural lighting in an unchanged
            # area. Bound stale influence to eight 8-bit levels per channel.
            smoothed = cv2.max(cv2.subtract(output, (8, 8, 8, 0)),
                               cv2.min(cv2.add(output, (8, 8, 8, 0)), smoothed))
            result = output.copy()
            cv2.copyTo(smoothed, safe, result)
        self.previous_source = source.copy()
        self.previous_output = result.copy()
        self.previous_static = unchanged
        return result
