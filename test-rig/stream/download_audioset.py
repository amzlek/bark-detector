#!/usr/bin/env python3
"""Fetches a small labeled bark / not-bark test set from Google AudioSet
into test-rig/dataset/, for the local test rig to stream.

AudioSet itself only publishes labels + (YouTube id, start, end) segments,
not the audio - Google doesn't redistribute the clips, for licensing
reasons. This script mirrors that: it downloads the matching segments from
YouTube via yt-dlp on demand, and the result is gitignored, not committed.

No separate label file: ground truth is encoded directly in each filename,
NN_bark.wav or NN_negative.wav - the leading number preserves download/
playback order, the suffix says which it is.

Idempotent: does nothing if test-rig/dataset/ already has any *.wav files,
unless --force is passed.

Usage (from the test-rig/ directory):
    python stream/download_audioset.py                    # fetch defaults if missing
    python stream/download_audioset.py --force             # re-fetch even if present
    python stream/download_audioset.py --bark 15 --negative 15
    python stream/download_audioset.py --out-dir /data --cache-dir /data/.cache

Requires ffmpeg on PATH and the packages in test-rig/pyproject.toml
(pip install . from test-rig/). Normally run automatically by the
stream-publisher container (see entrypoint.sh, in this same directory)
against a bind-mounted dataset/ - running it directly on the host also
works, for quick iteration without rebuilding the image.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

import pandas as pd
import yt_dlp
from yt_dlp.utils import download_range_func

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("download_audioset")

# this script lives in test-rig/stream/, but dataset/ (and its cache) are
# siblings of test-rig/stream/, not children of it
TEST_RIG_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATASET_DIR = TEST_RIG_DIR / "dataset"
DEFAULT_CACHE_DIR = TEST_RIG_DIR / ".audioset_cache"

EVAL_SEGMENTS_URL = (
    "https://storage.googleapis.com/us_audioset/youtube_corpus/v1/csv/eval_segments.csv"
)
BALANCED_SEGMENTS_URL = (
    "https://storage.googleapis.com/us_audioset/youtube_corpus/v1/csv/"
    "balanced_train_segments.csv"
)

# AudioSet ontology ids (verified against
# https://raw.githubusercontent.com/audioset/ontology/master/ontology.json,
# not just copied from memory).
#
# Positives are the exact "Bark" label only - NOT the whole dog-sound
# family. bark-detector's own default `listen` list is [bark, bow-wow,
# howl, yip], so a positive sample whose only dog-related label is e.g.
# Growling or Whimper isn't something the detector is even configured to
# catch - labeling it "bark: true" would just be a wrong ground truth.
BARK_LABEL_ID = "/m/05tny_"  # Bark

# Negatives exclude "Dog" and *every* child label under it (Bark, Yip,
# Howl, Bow-wow, Growling, Whimper, Bay) - a negative must contain no dog
# sound of any kind, not just no barks.
DOG_LABEL_IDS = (
    "/m/0bt9lr",  # Dog (parent)
    "/m/05tny_",  # Bark
    "/m/07r_k2n",  # Yip
    "/m/07qf0zm",  # Howl
    "/m/07rc7d9",  # Bow-wow
    "/m/0ghcn6",  # Growling
    "/t/dd00136",  # Whimper (dog)
    "/m/07srf8z",  # Bay
)

TARGET_SAMPLE_RATE = 16000
CLIP_MAX_SECONDS = 10.0  # AudioSet segments are already ~10s; cap defensively


def load_segments_index(cache_dir: Path) -> pd.DataFrame:
    """Downloads (and caches) AudioSet's two segment CSVs, combined into one
    DataFrame of (YTID, start_seconds, end_seconds, positive_labels)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    eval_cache = cache_dir / "eval_segments.csv"
    balanced_cache = cache_dir / "balanced_train_segments.csv"

    frames = []
    for url, cache_path in ((EVAL_SEGMENTS_URL, eval_cache), (BALANCED_SEGMENTS_URL, balanced_cache)):
        if cache_path.exists():
            logger.info("using cached %s", cache_path.name)
            frames.append(pd.read_csv(cache_path))
            continue

        logger.info("downloading %s", url)
        df = pd.read_csv(url, header=2, quotechar='"', skipinitialspace=True)
        df.columns = ["YTID", "start_seconds", "end_seconds", "positive_labels"]
        df.to_csv(cache_path, index=False)
        frames.append(df)

    return pd.concat(frames, ignore_index=True)


def select_candidates(index: pd.DataFrame, want_bark: bool, count: int, seed: int) -> pd.DataFrame:
    if want_bark:
        pool = index[index["positive_labels"].str.contains(BARK_LABEL_ID, na=False, regex=False)]
    else:
        dog_pattern = "|".join(DOG_LABEL_IDS)
        contains_dog = index["positive_labels"].str.contains(dog_pattern, na=False, regex=True)
        pool = index[~contains_dog]

    # oversample: many AudioSet YouTube ids are dead by now (removed/private
    # videos), so we sample more candidates than needed and fall through
    # them until `count` actually download successfully
    return pool.sample(n=min(len(pool), count * 5), random_state=seed)


def download_segment(ytid: str, start: float, end: float, out_path: Path) -> bool:
    """Downloads just [start, end) of the given YouTube video directly as a
    16kHz mono wav - no full-video download + re-encode round trip."""
    duration = min(end - start, CLIP_MAX_SECONDS)
    start_int = int(start)
    end_int = start_int + max(1, int(round(duration)))
    ydl_opts = {
        "format": "bestaudio/best",
        "paths": {"home": str(out_path.parent)},
        "outtmpl": {"default": out_path.stem + ".%(ext)s"},
        "download_ranges": download_range_func([], [(start_int, end_int)]),
        "force_keyframes_at_cuts": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "wav"},
        ],
        # key is lowercase "extractaudio", confirmed by actually inspecting
        # what yt_dlp.options's own CLI parser produces for
        # `--postprocessor-args "ExtractAudio:..."` - two other guesses
        # ("ffmpegextractaudio", "ExtractAudio") both failed *silently*
        # (wrong keys are dropped, not errored), producing untouched
        # stereo/44.1-48kHz files instead of mono 16kHz
        "postprocessor_args": {
            "extractaudio": ["-ar", str(TARGET_SAMPLE_RATE), "-ac", "1"],
        },
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }

    try:
        # yt-dlp's _Params TypedDict stub is notoriously hard for a plain
        # options dict to satisfy structurally (a well-known yt-dlp typing
        # complaint); this exact dict has been verified working via live
        # runs against real YouTube videos, not just type-checked
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
            ydl.download([f"https://youtube.com/watch?v={ytid}"])
    except Exception as exc:
        logger.warning("  skip %s: %s", ytid, exc)
        return False

    return out_path.exists()


def fetch_dataset(dataset_dir: Path, cache_dir: Path, bark_count: int, negative_count: int, seed: int) -> None:
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found on PATH - install it before running this script")

    index = load_segments_index(cache_dir)

    bark_candidates = select_candidates(index, want_bark=True, count=bark_count, seed=seed)
    negative_candidates = select_candidates(index, want_bark=False, count=negative_count, seed=seed)

    dataset_dir.mkdir(parents=True, exist_ok=True)
    fetched_paths: list[tuple[Path, bool]] = []  # (tmp path, is_bark), in download order

    for label, candidates, target in (
        ("bark", bark_candidates, bark_count),
        ("negative", negative_candidates, negative_count),
    ):
        fetched = 0
        logger.info("fetching %d %s sample(s)...", target, label)
        for _, row in candidates.iterrows():
            if fetched >= target:
                break
            tmp_path = dataset_dir / f"_tmp_{row['YTID']}.wav"
            ok = download_segment(row["YTID"], row["start_seconds"], row["end_seconds"], tmp_path)
            if not ok:
                continue
            fetched += 1
            fetched_paths.append((tmp_path, label == "bark"))
            logger.info("  [%d/%d] %s -> ok", fetched, target, row["YTID"])

        if fetched < target:
            logger.warning(
                "only got %d/%d %s sample(s) (many AudioSet YouTube ids are dead by now)",
                fetched,
                target,
                label,
            )

    # interleave bark/negative for final numbering, so the published test
    # stream alternates rather than playing all barks then all negatives
    barks = [p for p, is_bark in fetched_paths if is_bark]
    negatives = [p for p, is_bark in fetched_paths if not is_bark]
    ordered: list[tuple[Path, bool]] = []
    while barks or negatives:
        if barks:
            ordered.append((barks.pop(0), True))
        if negatives:
            ordered.append((negatives.pop(0), False))

    for i, (tmp_path, is_bark) in enumerate(ordered, start=1):
        suffix = "bark" if is_bark else "negative"
        tmp_path.rename(dataset_dir / f"{i:02d}_{suffix}.wav")

    logger.info("wrote %d sample(s) to %s", len(ordered), dataset_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bark", type=int, default=5, help="number of bark samples (default: 5)")
    parser.add_argument("--negative", type=int, default=5, help="number of non-bark samples (default: 5)")
    parser.add_argument("--seed", type=int, default=0, help="random seed for sample selection")
    parser.add_argument("--force", action="store_true", help="re-fetch even if dataset already exists")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_DATASET_DIR, help="where to write samples")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="where to cache AudioSet's segment CSVs")
    args = parser.parse_args()

    if not args.force and args.out_dir.exists() and any(args.out_dir.glob("*.wav")):
        logger.info("%s already has samples, skipping (use --force to re-fetch)", args.out_dir)
        return

    fetch_dataset(args.out_dir, args.cache_dir, args.bark, args.negative, args.seed)


if __name__ == "__main__":
    main()
