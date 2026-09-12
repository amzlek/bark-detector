import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from bark_detector.audio_format import AUDIO_SAMPLE_RATE, CHUNK_BYTES
from bark_detector.audio_source import FfmpegAudioSource, build_ffmpeg_command
from bark_detector.config import build_source_config
from bark_detector.probe import _strip_ffmpeg_banner, probe_source


class AudioIoTests(unittest.TestCase):
    def test_ffmpeg_command_matches_source_protocol(self):
        rtsp = build_ffmpeg_command("rtsp", "rtsp://camera", ["-timeout", "5"], "ffmpeg")
        self.assertEqual(rtsp[:6], ["ffmpeg", "-vn", "-threads", "1", "-rtsp_transport", "tcp"])
        self.assertEqual(rtsp[rtsp.index("-i") + 1], "rtsp://camera")
        self.assertEqual(rtsp[-1], "pipe:")
        self.assertEqual(rtsp.index("-timeout") + 1, rtsp.index("5"))
        tcp = build_ffmpeg_command("esphome_tcp", "tcp://device:6055", [], "ffmpeg")
        self.assertEqual(tcp[tcp.index("-f") + 1], "s16le")
        self.assertIn(str(AUDIO_SAMPLE_RATE), tcp)

    def test_ffmpeg_source_reads_complete_chunk(self):
        source = build_source_config({"name": "Kitchen", "type": "rtsp", "path": "rtsp://camera"})
        audio = FfmpegAudioSource(source, "ffmpeg", threading.Event(), retry_interval=0)
        process = MagicMock()
        process.stdout.read.return_value = bytes(CHUNK_BYTES)
        with patch("bark_detector.audio_source.subprocess.Popen", return_value=process):
            self.assertEqual(audio.read_chunk(), bytes(CHUNK_BYTES))
        process.stdout.read.assert_called_with(CHUNK_BYTES)
        audio.stop()
        process.terminate.assert_called_once()

    def test_stalled_ffmpeg_restarts_and_stop_interrupts_retry(self):
        source = build_source_config({"name": "Kitchen", "type": "rtsp", "path": "rtsp://camera"})
        stopped = threading.Event()
        audio = FfmpegAudioSource(source, "ffmpeg", stopped, retry_interval=30)
        released = threading.Event()
        process = MagicMock()
        process.stdout.read.side_effect = lambda size: (released.wait(), b"")[1]
        process.terminate.side_effect = released.set
        with patch("bark_detector.audio_source.subprocess.Popen", return_value=process), \
             patch("bark_detector.audio_source.STALL_SECONDS", 0.1):
            audio.start()
            started = time.monotonic()
            worker = threading.Thread(target=audio.read_chunk)
            worker.start()
            time.sleep(0.3)
            stopped.set()
            worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 1)
        process.terminate.assert_called()
        self.assertIsNone(audio.process)

    def test_short_audio_gap_does_not_restart_ffmpeg(self):
        source = build_source_config({"name": "Kitchen", "type": "rtsp", "path": "rtsp://camera"})
        audio = FfmpegAudioSource(source, "ffmpeg", threading.Event(), retry_interval=0)
        released = threading.Event()
        process = MagicMock()
        process.poll.return_value = None
        process.stdout.read.side_effect = lambda size: (released.wait(), bytes(CHUNK_BYTES))[1]
        process.terminate.side_effect = released.set
        with patch("bark_detector.audio_source.subprocess.Popen", return_value=process):
            audio.start()
            self.assertIsNone(audio.read_chunk())
            process.terminate.assert_not_called()
            released.set()
            self.assertEqual(audio.read_chunk(), bytes(CHUNK_BYTES))
            audio.stop()

    def test_probe_reports_spawn_error_and_filters_banner(self):
        with patch("bark_detector.probe.subprocess.Popen", side_effect=FileNotFoundError("missing")):
            ok, message = probe_source("rtsp", "rtsp://camera", [], "missing-ffmpeg")
        self.assertFalse(ok)
        self.assertIn("failed to start ffmpeg", message)
        self.assertEqual(_strip_ffmpeg_banner("ffmpeg version 7\n  configuration: x\n  libavcodec 1\nConnection refused"),
                         "Connection refused")


if __name__ == "__main__":
    unittest.main()
