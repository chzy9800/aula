"""说话人聚类单元测试。零依赖方案，纯合成音频验证行为。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from aula.diarize import SpeakerCache, SpeakerTracker  # noqa: E402

SR = 16000


def tone(freq: float, duration: float = 0.6, amplitude: int = 12000) -> bytes:
    t = np.arange(int(SR * duration)) / SR
    return (np.sin(2 * np.pi * freq * t) * amplitude).astype(np.int16).tobytes()


class SpeakerTrackerTest(unittest.TestCase):

    def test_separates_two_distinct_pitches(self):
        """120Hz 与 300Hz 应该被分成两个说话人，且来回切换仍能归位。"""
        tracker = SpeakerTracker()
        labels = [tracker.assign(tone(120)), tracker.assign(tone(120)),
                  tracker.assign(tone(300)), tracker.assign(tone(300)),
                  tracker.assign(tone(120))]
        self.assertEqual(labels[0], labels[1])
        self.assertEqual(labels[2], labels[3])
        self.assertNotEqual(labels[0], labels[2], "不同基频必须分成两个人")
        self.assertEqual(labels[4], labels[0], "回到第一个人时要认得出来")
        self.assertEqual(tracker.speakers, 2)

    def test_same_voice_stays_one_speaker(self):
        tracker = SpeakerTracker()
        for _ in range(5):
            tracker.assign(tone(150))
        self.assertEqual(tracker.speakers, 1)

    def test_silence_returns_empty(self):
        """静音没法算特征，返回空串而不是硬塞一个标签。"""
        tracker = SpeakerTracker()
        self.assertEqual(tracker.assign(b"\x00\x00" * 8000), "")
        self.assertEqual(tracker.speakers, 0)

    def test_max_speakers_is_respected(self):
        """超过上限就不再新建，避免噪音把说话人数吹爆。"""
        tracker = SpeakerTracker(max_speakers=2)
        for freq in (100, 200, 300, 400, 500):
            tracker.assign(tone(freq))
        self.assertLessEqual(tracker.speakers, 2)

    def test_cache_isolates_sessions(self):
        cache = SpeakerCache()
        cache.assign("s1", tone(120))
        cache.assign("s2", tone(300))
        # 两个课堂各自从 A 开始，互不影响
        self.assertEqual(cache.assign("s1", tone(120)), "A")
        self.assertEqual(cache.assign("s2", tone(300)), "A")


if __name__ == "__main__":
    unittest.main()
