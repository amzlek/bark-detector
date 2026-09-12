"""Evaluate clip-level bark classification against an external CSV manifest.

Manifest columns: path,category,is_bark. Paths are relative to the manifest.
This deliberately runs outside the normal unit-test suite and keeps licensed
audio out of the repository.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from collections import defaultdict
from pathlib import Path

from bark_detector.audio_format import AUDIO_SAMPLE_RATE, CHUNK_BYTES
from bark_detector.detector import AudioTfl, pcm_chunk_to_waveform


def summarize(rows: list[dict], threshold: float) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["category"]].append(row)

    def metrics(items: list[dict]) -> dict:
        tp = sum(row["is_bark"] and row["score"] >= threshold for row in items)
        fp = sum(not row["is_bark"] and row["score"] >= threshold for row in items)
        fn = sum(row["is_bark"] and row["score"] < threshold for row in items)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        scores = sorted(row["score"] for row in items)
        return {
            "count": len(items), "precision": precision, "recall": recall,
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
            "false_positives": fp, "false_negatives": fn,
            "score_distribution": {
                "min": scores[0] if scores else None,
                "median": scores[len(scores) // 2] if scores else None,
                "max": scores[-1] if scores else None,
                "scores": scores,
            },
        }

    return {
        "threshold": threshold,
        "overall": metrics(rows),
        "by_negative_category": {
            category: metrics(items)
            for category, items in sorted(groups.items())
            if all(not row["is_bark"] for row in items)
        },
        "clips": rows,
    }


def score_clip(path: Path, detector: AudioTfl, ffmpeg: str, labels: set[str]) -> float:
    pcm = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-vn", "-f", "s16le",
         "-ar", str(AUDIO_SAMPLE_RATE), "-ac", "1", "pipe:"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
    ).stdout
    if not pcm:
        raise ValueError(f"no audio in {path}")
    peak = 0.0
    for offset in range(0, len(pcm), CHUNK_BYTES):
        chunk = pcm[offset:offset + CHUNK_BYTES]
        if len(chunk) < CHUNK_BYTES:
            chunk = chunk.ljust(CHUNK_BYTES, b"\0")
        waveform, _, _ = pcm_chunk_to_waveform(chunk)
        for label, score in detector.detect(waveform, threshold=0.0):
            if label in labels:
                peak = max(peak, score)
    return peak


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--labelmap", default="audio-labelmap.txt")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--labels", nargs="+", default=["bark"])
    args = parser.parse_args()
    if not 0 < args.threshold <= 1:
        parser.error("--threshold must be in (0, 1]")
    detector = AudioTfl(model_path=args.model, labelmap_path=args.labelmap)
    rows = []
    with args.manifest.open(newline="", encoding="utf-8") as stream:
        for item in csv.DictReader(stream):
            if item["is_bark"].strip().lower() not in ("true", "false", "1", "0"):
                raise ValueError(f"invalid is_bark for {item['path']}")
            path = (args.manifest.parent / item["path"]).resolve()
            rows.append({
                "path": item["path"], "category": item["category"],
                "is_bark": item["is_bark"].strip().lower() in ("true", "1"),
                "score": score_clip(path, detector, args.ffmpeg, set(args.labels)),
            })
    print(json.dumps(summarize(rows, args.threshold), indent=2))


if __name__ == "__main__":
    main()
