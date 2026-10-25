"""说话人分离（轻量版）。

为什么不用 pyannote：它要 PyTorch（约 2GB）+ 200MB 权重 + 最好有 GPU，而课堂场景
多数时间只有一位老师在讲。成本远大于收益。

这里用三个声学特征做**在线聚类**，零额外依赖（numpy 本来就是 whisper 的依赖）：
  · 频谱质心 —— 音色亮度，男女声差别明显
  · 基频 F0  —— 声带振动频率，是最强的性别/个体线索
  · 过零率   —— 清浊音比例，辅助区分

准确率大约 70–85%（干净音频、2 位说话人时更好）。它的用途是给字幕加个
"这句话可能是另一个人说的"提示，不是法医级识别。别当专业 diarization 用。
"""

from __future__ import annotations

import logging
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)

_LABELS = "ABCDEFGH"


class SpeakerTracker:
    """在线说话人聚类：每来一句就判断它属于哪个已有说话人，或者新建一个。"""

    def __init__(self, max_speakers: int = 3, threshold: float = 0.55) -> None:
        self.max_speakers = max_speakers
        self.threshold = threshold
        self._centroids: List[np.ndarray] = []
        self._weights: List[float] = []      # 每个质心累计的样本量，用于滑动平均
        self._counts: List[int] = []

    @property
    def speakers(self) -> int:
        return len(self._centroids)

    def reset(self) -> None:
        self._centroids.clear()
        self._weights.clear()
        self._counts.clear()

    def assign(self, pcm: bytes, sample_rate: int = 16000) -> str:
        """返回说话人标签（A/B/C…）。空音频返回空串。"""
        feature = self._features(pcm, sample_rate)
        if feature is None:
            return ""
        if not self._centroids:
            self._add(feature)
            return _LABELS[0]

        distances = [float(np.linalg.norm(feature - c)) for c in self._centroids]
        nearest = int(np.argmin(distances))

        if distances[nearest] > self.threshold and len(self._centroids) < self.max_speakers:
            self._add(feature)
            return _LABELS[len(self._centroids) - 1]

        # 滑动平均更新质心；用累计量做衰减，越到后面越稳，不会因为一句噪音跑偏
        weight = min(self._weights[nearest], 10.0)
        self._centroids[nearest] = (self._centroids[nearest] * weight + feature) / (weight + 1.0)
        self._weights[nearest] = weight + 1.0
        self._counts[nearest] += 1
        return _LABELS[nearest]

    def _add(self, feature: np.ndarray) -> None:
        self._centroids.append(feature)
        self._weights.append(1.0)
        self._counts.append(1)

    # MARK: 特征

    def _features(self, pcm: bytes, sample_rate: int) -> Optional[np.ndarray]:
        if not pcm or len(pcm) < 3200:      # 少于 0.1 秒没法算，直接放弃
            return None
        try:
            audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        except (ValueError, TypeError):
            return None
        if audio.size < 1600:
            return None

        # 只看有声部分：静音段会把特征拉平
        frame = max(1, sample_rate // 50)   # 20ms
        frames = audio[: audio.size - audio.size % frame].reshape(-1, frame)
        energies = np.sqrt((frames ** 2).mean(axis=1))
        # 整体能量过低（静音/纯底噪）时直接放弃：否则全零信号也会算出"特征"，
        # 被硬归成说话人 A，等于给静音标了个人名。
        if float(energies.mean()) < 1e-4:
            return None
        voiced = frames[energies > max(1e-4, float(energies.mean()) * 0.4)]
        if voiced.size < frame:
            voiced = frames
        signal = voiced.reshape(-1)

        # 1) 频谱质心（归一化到 ~0–2）
        spectrum = np.abs(np.fft.rfft(signal))
        freqs = np.fft.rfftfreq(signal.size, 1.0 / sample_rate)
        centroid = float((spectrum * freqs).sum() / (spectrum.sum() + 1e-9))

        # 2) 基频（自相关；语音范围 70–400Hz）
        pitch = self._pitch(signal, sample_rate)

        # 3) 过零率
        zcr = float(np.mean(np.abs(np.diff(np.sign(signal))) > 0))

        return np.array([centroid / 1500.0, pitch / 150.0, zcr], dtype=np.float32)

    def _pitch(self, signal: np.ndarray, sample_rate: int) -> float:
        """自相关估基频。比 FFT 峰值稳，短音频也够用。"""
        max_lag = int(sample_rate / 70)     # 最低 70Hz
        min_lag = int(sample_rate / 400)    # 最高 400Hz
        if signal.size < max_lag * 2:
            return 150.0
        window = signal[: min(signal.size, sample_rate)]   # 最多取 1 秒
        window = window - window.mean()
        correlation = np.correlate(window, window, mode="full")[window.size - 1:]
        if correlation[0] <= 0:
            return 150.0
        segment = correlation[min_lag:max_lag]
        if segment.size == 0:
            return 150.0
        lag = int(np.argmax(segment)) + min_lag
        return float(sample_rate / lag) if lag > 0 else 150.0


class SpeakerCache:
    """按 session 管理 tracker，避免不同课堂互相污染。"""

    def __init__(self, max_speakers: int = 3) -> None:
        self._trackers: dict[str, SpeakerTracker] = {}
        self._max = max_speakers

    def assign(self, session_id: str, pcm: bytes, sample_rate: int = 16000) -> str:
        tracker = self._trackers.get(session_id)
        if tracker is None:
            tracker = SpeakerTracker(max_speakers=self._max)
            self._trackers[session_id] = tracker
        return tracker.assign(pcm, sample_rate)

    def reset(self, session_id: str) -> None:
        tracker = self._trackers.get(session_id)
        if tracker is not None:
            tracker.reset()

    def drop(self, session_id: str) -> None:
        self._trackers.pop(session_id, None)
