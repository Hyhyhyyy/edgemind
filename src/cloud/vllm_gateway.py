"""vLLM 推理网关（云端大模型接入层）。

vLLM 以 OpenAI 兼容接口对外提供服务：POST {base_url}/v1/chat/completions。
本网关做两件事：
1. 真实模式：把请求转发到 vLLM 服务（演示「了解 vLLM 等大模型推理服务」）；
2. 离线 mock 模式：base_url 为空时，用确定性规则生成结构化回复，
   让整个 PoC 不依赖 GPU / 网络也能跑通端到端演示。

无论哪种模式，对外暴露统一接口 `chat(messages)`，供诊断 Agent 与普通
推理请求共用——这体现了「云边协同推理」中云端重型模型的位置。
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from common.config import CONFIG
from common.logging_setup import get_logger

logger = get_logger("cloud.vllm_gateway", "cloud")


@dataclass
class ChatMessage:
    role: str
    content: str


@dataclass
class NodeView:
    node_id: str = ""
    healthy: bool = True


class VLLMGateway:
    def __init__(self, base_url: str = "", model: str = "mock-7b"):
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.model = model

    def chat(self, messages: list[ChatMessage], temperature: float = 0.2,
             max_tokens: int = 512) -> str:
        """返回模型文本回复。失败（网络/vLLM 不可用）时降级到 mock。"""
        if not self.base_url:
            return self._mock(messages)
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            return data["choices"][0]["message"]["content"]
        except Exception as e:  # vLLM 不可达 -> 降级（也是真实故障场景之一）
            logger.warning("vLLM 调用失败，降级 mock: %s", e)
            return self._mock(messages)

    def _mock(self, messages: list[ChatMessage]) -> str:
        """离线确定性回复。

        若提示词要求输出 root_cause（诊断 Agent 的 RCA 请求），则基于
        用户消息中的 incident 字段生成结构化 JSON；否则返回通用文本。
        """
        combined = "\n".join(m.content for m in messages)
        if "root_cause" in combined and "JSON" in combined:
            return self._mock_rca(combined)
        last = messages[-1].content if messages else ""
        return f"[mock-7b] 已收到请求：{last[:80]} …（离线演示回复，配置 VLLM_BASE_URL 可接入真实 vLLM）"

    def _mock_rca(self, combined: str) -> str:
        """从 incident 上下文推导一个合理的根因分析 JSON。

        这是「故障排查经验」的规则化体现：即便没有 LLM，也能给出
        可解释的结论；接入真实 vLLM 后由模型做更泛化的归因。
        """
        import re
        node = re.search(r"node_id=([\w-]+)", combined)
        node_id = node.group(1) if node else "unknown"
        kind = re.search(r"kind=([\w_]+)", combined)
        kind = kind.group(1) if kind else "unknown"

        table = {
            "heartbeat_lost": {
                "root_cause": "边缘节点与云的 WebSocket 长连接中断（网络分区/断电/进程崩溃），超过心跳超时阈值未收到心跳。",
                "severity": "critical",
                "actions": ["mark_not_ready", "reschedule_to_cloud", "alert_oncall"],
            },
            "gpu_oom": {
                "root_cause": "边端 GPU 显存不足，本地大模型推理进程 OOM 被杀，model_loaded 变为空。",
                "severity": "warning",
                "actions": ["fallback_edge_to_small_model", "route_inference_to_cloud", "restart_edge_runtime"],
            },
            "latency_spike": {
                "root_cause": "云侧 vLLM 实例排队长度过高或边端到云 RTT 突增，导致端到端推理 P95 超 SLA。",
                "severity": "warning",
                "actions": ["scale_cloud_replicas", "route_to_local_edge", "throttle_low_priority"],
            },
            "model_load_fail": {
                "root_cause": "边端拉取/加载模型权重失败（镜像仓库不可达或磁盘满），本地推理不可用。",
                "severity": "warning",
                "actions": ["route_inference_to_cloud", "restart_edge_runtime", "alert_oncall"],
            },
        }
        result = table.get(kind, {
            "root_cause": f"未识别的异常类型 {kind}，建议人工介入。",
            "severity": "info",
            "actions": ["alert_oncall"],
        })
        result["node_id"] = node_id
        result["kind"] = kind
        return json.dumps(result, ensure_ascii=False)
