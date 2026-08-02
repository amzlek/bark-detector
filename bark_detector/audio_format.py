"""Shared audio format constants, kept dependency-free (no numpy/tflite)
so any module can use them without pulling in the TFLite backend."""

AUDIO_SAMPLE_RATE = 16000
AUDIO_DURATION = 0.975  # seconds per inference chunk, matches the model's input window
AUDIO_MAX_BIT_RANGE = 32768.0
CHUNK_SAMPLES = int(round(AUDIO_DURATION * AUDIO_SAMPLE_RATE))
CHUNK_BYTES = CHUNK_SAMPLES * 2  # 16-bit PCM
