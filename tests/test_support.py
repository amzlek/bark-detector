"""Scratch directories for tests, including Windows sandboxed runs."""

from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path


class ScratchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.scratch = Path(__file__).parent / ".tmp" / uuid.uuid4().hex
        self.scratch.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.scratch)
