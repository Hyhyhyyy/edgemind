"""集中式配置。

通过 dataclass + 环境变量覆盖，体现工程化配置管理习惯
（不把端口/地址硬编码在业务代码里）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)


@dataclass
class Config:
    # 云端
    cloud_http_host: str = field(default_factory=lambda: _env_str("EDGEMIND_HTTP_HOST", "0.0.0.0"))
    cloud_http_port: int = field(default_factory=lambda: _env_int("EDGEMIND_HTTP_PORT", 8000))
    cloud_ws_host: str = field(default_factory=lambda: _env_str("EDGEMIND_WS_HOST", "0.0.0.0"))
    cloud_ws_port: int = field(default_factory=lambda: _env_int("EDGEMIND_WS_PORT", 9000))

    # vLLM 推理网关（OpenAI 兼容）。为空则使用内置 mock 推理，便于离线演示。
    vllm_base_url: str = field(default_factory=lambda: _env_str("VLLM_BASE_URL", ""))
    vllm_model: str = field(default_factory=lambda: _env_str("VLLM_MODEL", "mock-7b"))

    # 云边协同
    heartbeat_interval: float = field(default_factory=lambda: _env_int("HB_INTERVAL", 3))
    heartbeat_timeout: float = field(default_factory=lambda: _env_int("HB_TIMEOUT", 9))

    # 诊断 Agent
    diag_interval: float = field(default_factory=lambda: _env_int("DIAG_INTERVAL", 5))
    dry_run: bool = field(default_factory=lambda: os.getenv("DRY_RUN", "0") == "1")

    # 本地推理（边端）。为空则使用内置 mock。
    edge_model: str = field(default_factory=lambda: _env_str("EDGE_MODEL", "mock-mini-0.5b"))


CONFIG = Config()
