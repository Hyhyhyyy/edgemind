"""边端本地轻量推理（模拟 KubeEdge 边缘节点上跑的小模型）。

真实场景下这里会接 llama.cpp / 量化后的 vLLM / Ollama 等，在边端做
低时延、可离线的推理。本模块用确定性 mock 模拟「小模型」：吞吐更高、
时延更低但能力弱于云端大模型——这正是「云边协同分层推理」的出发点。

对外暴露 infer() 与加载状态，供 edge_agent 上报指标与做路由决策。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from common.logging_setup import get_logger

logger = get_logger("edge.local_inference", "edge")


@dataclass
class LocalModel:
    name: str
    params_b: float          # 参数量（十亿）
    base_latency_ms: float   # 单条基准时延


_KNOWN = {
    "mock-mini-0.5b": LocalModel("mock-mini-0.5b", 0.5, 40.0),
    "mock-small-1.5b": LocalModel("mock-small-1.5b", 1.5, 90.0),
}


class LocalInference:
    def __init__(self, model: str = "mock-mini-0.5b"):
        self.model = model
        self.loaded = False
        self.gpu_mem_percent = 0.0
        self._load()

    def _load(self) -> None:
        spec = _KNOWN.get(self.model, _KNOWN["mock-mini-0.5b"])
        # 模拟加载占用显存（参数量越大占越多）
        self.gpu_mem_percent = min(95.0, 20.0 + spec.params_b * 18.0)
        self.loaded = True
        logger.info("本地模型已加载: %s (显存约 %.0f%%)", self.model, self.gpu_mem_percent)

    def swap(self, model: str) -> None:
        self.model = model
        self._load()

    def infer(self, prompt: str) -> str:
        if not self.loaded:
            raise RuntimeError("本地模型未加载")
        spec = _KNOWN.get(self.model, _KNOWN["mock-mini-0.5b"])
        # 时延随输入长度线性增长（模拟）
        latency = spec.base_latency_ms + len(prompt) * 0.5
        time.sleep(min(0.3, latency / 1000.0))  # 演示用，限制真实等待
        return f"[edge:{self.model}] 本地轻量推理完成（{latency:.0f}ms）: {prompt[:40]}…"

    def release(self) -> None:
        self.loaded = False
        self.gpu_mem_percent = 0.0
