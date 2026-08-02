"""TFLite audio classification, ported from Frigate's frigate/events/audio.py.

Reuses the same YAMNet-derived model (cpu_audio_model.tflite) and label map
(audio-labelmap.txt) Frigate ships, and the same chunking/normalization math,
with no dependency on Frigate's app/process framework.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Optional

import numpy as np

from .audio_format import AUDIO_MAX_BIT_RANGE, AUDIO_SAMPLE_RATE

try:
    from ai_edge_litert.interpreter import Interpreter
except ModuleNotFoundError:  # pragma: no cover - platform dependent
    try:
        from tflite_runtime.interpreter import Interpreter
    except ModuleNotFoundError:
        from tensorflow.lite.python.interpreter import Interpreter

logger = logging.getLogger(__name__)

AUDIO_MIN_CONFIDENCE = 0.5


def load_labels(path: str, encoding: str = "utf-8") -> dict[int, str]:
    with open(path, "r", encoding=encoding) as f:
        lines = f.readlines()
    return {index: line.strip() for index, line in enumerate(lines)}


@contextlib.contextmanager
def _suppress_stderr():
    """Silence native stderr chatter from TFLite delegate init."""
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        saved_fd = os.dup(2)
        os.dup2(devnull_fd, 2)
    except OSError:
        yield
        return
    try:
        yield
    finally:
        os.dup2(saved_fd, 2)
        os.close(devnull_fd)
        os.close(saved_fd)


class AudioTfl:
    def __init__(
        self,
        model_path: str = "/app/cpu_audio_model.tflite",
        labelmap_path: str = "/app/audio-labelmap.txt",
        num_threads: int = 2,
    ):
        self.labels = load_labels(labelmap_path)

        with _suppress_stderr():
            self.interpreter = Interpreter(
                model_path=model_path,
                num_threads=num_threads,
            )
            self.interpreter.allocate_tensors()

        self.tensor_input_details = self.interpreter.get_input_details()
        self.tensor_output_details = self.interpreter.get_output_details()

    def _detect_raw(self, tensor_input: np.ndarray) -> np.ndarray:
        self.interpreter.set_tensor(self.tensor_input_details[0]["index"], tensor_input)
        self.interpreter.invoke()

        res = self.interpreter.get_tensor(self.tensor_output_details[0]["index"])[0]
        non_zero_indices = res > 0
        class_ids = np.argpartition(-res, 20)[:20]
        class_ids = class_ids[np.argsort(-res[class_ids])]
        class_ids = class_ids[non_zero_indices[class_ids]]
        scores = res[class_ids]

        detections = np.zeros((20, 2), np.float32)
        for i in range(len(scores)):
            if scores[i] < AUDIO_MIN_CONFIDENCE or i == 20:
                break
            detections[i] = [class_ids[i], float(scores[i])]

        return detections

    def detect(
        self, waveform: np.ndarray, threshold: float = AUDIO_MIN_CONFIDENCE
    ) -> list[tuple[str, float]]:
        """waveform must be float32 samples normalized to [-1, 1]."""
        raw_detections = self._detect_raw(waveform)

        detections = []
        for class_id, score in raw_detections:
            if score < threshold:
                break
            detections.append((self.labels[int(class_id)], float(score)))
        return detections


def pcm_chunk_to_waveform(chunk: bytes) -> tuple[np.ndarray, float, float]:
    """Convert a raw s16le PCM chunk into a normalized float32 waveform plus
    (rms, dBFS) audio level, matching frigate/events/audio.py's gating logic."""
    audio = np.frombuffer(chunk, dtype=np.int16)
    audio_as_float = audio.astype(np.float32)

    rms = float(np.sqrt(np.mean(np.absolute(np.square(audio_as_float)))))
    dBFS = float(20 * np.log10(rms / AUDIO_MAX_BIT_RANGE)) if rms > 0 else 0.0

    waveform = (audio / AUDIO_MAX_BIT_RANGE).astype(np.float32)
    return waveform, rms, dBFS
