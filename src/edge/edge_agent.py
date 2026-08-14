"""EdgeAgent：边缘节点智能体（模拟 KubeEdge edgecore 上的业务容器）。

职责：
- 通过 EdgeHub 与云端 CloudHub 维持长连接，上线即 REGISTER；
- 周期性发送 HEARTBEAT + METRICS（带模拟的真实指标：CPU/GPU/网络/时延/队列）；
- 接收并执行云端下发的 COMMAND（注入故障、换模型、重启运行时、重启节点）；
- 持有 LocalInference（边端轻量推理），并在本地不可用时把请求路由到云端；
- 本地异常（GPU OOM / 模型加载失败）主动上报 EVENT，触发云端自愈。

整个边端是一个可被 KubeEdge DaemonSet 调度的「边缘工作负载」雏形。
"""
from __future__ import annotations

import os
import random
import threading
import time
from typing import Optional

from common.config import CONFIG
from common.logging_setup import get_logger
from common.protocol import Envelope, MsgType, NodeMetrics
from edge.edgehub import EdgeHub
from edge.local_inference import LocalInference

logger = get_logger("edge.agent", "edge")


class EdgeAgent:
    def __init__(self, node_id: str, region: str = "default", gpu: str = "none",
                 cloud_ws_url: Optional[str] = None, cloud_http_url: Optional[str] = None,
                 model: Optional[str] = None):
        self.node_id = node_id
        self.region = region
        self.gpu = gpu
        self.cloud_ws_url = cloud_ws_url or f"ws://127.0.0.1:{CONFIG.cloud_ws_port}"
        self.cloud_http_url = cloud_http_url or f"http://127.0.0.1:{CONFIG.cloud_http_port}"
        self.local = LocalInference(model or CONFIG.edge_model)
        self.hub = EdgeHub(self.cloud_ws_url, node_id, on_command=self._on_command)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # 故障模拟状态
        self._pause_heartbeat = False
        self._fault_gpu_oom = False
        self._fault_latency_until = 0.0
        self._healthy = True
        self.pending_workloads = 0
        self._loop_thread: Optional[threading.Thread] = None

    # ---- 生命周期 ----
    def start(self) -> None:
        self.hub.connect()
        self._loop_thread = threading.Thread(target=self._loop, daemon=True)
        self._loop_thread.start()
        logger.info("[%s] EdgeAgent 启动", self.node_id)

    def stop(self) -> None:
        self._stop.set()
        self.hub.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                pause = self._pause_heartbeat
            if not pause:
                self._send_register_if_needed()
                self.hub.send(Envelope.build(MsgType.HEARTBEAT, self.node_id, {}))
                self.hub.send(Envelope.build(MsgType.METRICS, self.node_id,
                                             self._collect_metrics().to_dict()))
            time.sleep(CONFIG.heartbeat_interval)

    def _send_register_if_needed(self) -> None:
        # 简化处理：每次心跳附带一次 register 语义（云端幂等）
        self.hub.send(Envelope.build(MsgType.REGISTER, self.node_id,
                                     {"region": self.region, "gpu": self.gpu}))

    # ---- 指标采集（模拟真实遥测）----
    def _collect_metrics(self) -> NodeMetrics:
        with self._lock:
            oom = self._fault_gpu_oom
            latency_until = self._fault_latency_until
        now = time.time()
        if oom:
            return NodeMetrics(
                cpu_percent=55.0, mem_percent=70.0,
                gpu_util_percent=0.0, gpu_mem_percent=98.0,
                net_rtt_ms=30.0, inf_queue=0, inf_p95_ms=0.0,
                model_loaded=None, healthy=False,
            )
        lat = 60.0 + random.uniform(-10, 30)
        rtt = 25.0 + random.uniform(-5, 15)
        if now < latency_until:
            lat = 1500.0
            rtt = 420.0
        return NodeMetrics(
            cpu_percent=30.0 + random.uniform(0, 25),
            mem_percent=45.0 + random.uniform(0, 20),
            gpu_util_percent=40.0 + random.uniform(-10, 30) if self.gpu != "none" else None,
            gpu_mem_percent=self.local.gpu_mem_percent if self.local.loaded else 0.0,
            net_rtt_ms=rtt, inf_queue=self.pending_workloads,
            inf_p95_ms=lat, model_loaded=self.local.model if self.local.loaded else None,
            healthy=self._healthy,
        )

    # ---- 指令处理 ----
    def _on_command(self, env: Envelope) -> None:
        cmd = env.payload.get("command", "")
        params = env.payload.get("params", {})
        logger.info("[%s] 收到指令: %s %s", self.node_id, cmd, params)
        if cmd == "inject_fault":
            self._inject_fault(params.get("kind", ""))
        elif cmd == "load_model":
            self.local.swap(params.get("model", CONFIG.edge_model))
            with self._lock:
                self._fault_gpu_oom = False
                self._healthy = True
        elif cmd == "restart_runtime":
            self.local.release()
            self.local._load()
            with self._lock:
                self._fault_gpu_oom = False
                self._healthy = True
            self._emit_event("runtime_restarted", "info", "本地推理运行时已重启并恢复")
        elif cmd == "reboot":
            self._reboot()
        else:
            logger.warning("[%s] 未知指令: %s", self.node_id, cmd)

    def _inject_fault(self, kind: str) -> None:
        with self._lock:
            if kind == "heartbeat_lost":
                self._pause_heartbeat = True
                logger.error("[%s] 模拟故障：停止发送心跳（网络分区）", self.node_id)
            elif kind == "gpu_oom":
                self._fault_gpu_oom = True
                self.local.release()
                self._healthy = False
                logger.error("[%s] 模拟故障：GPU OOM，本地模型被杀死", self.node_id)
                self._emit_event("gpu_oom", "warning", "GPU 显存耗尽，本地推理进程 OOM 退出")
            elif kind == "model_load_fail":
                self._healthy = False
                self.local.release()
                logger.error("[%s] 模拟故障：模型权重加载失败", self.node_id)
                self._emit_event("model_load_fail", "warning", "拉取/加载模型权重失败，本地推理不可用")
            elif kind == "latency_spike":
                self._fault_latency_until = time.time() + 60.0
                logger.warning("[%s] 模拟故障：推理/网络时延突增", self.node_id)
            else:
                logger.warning("[%s] 未知故障类型: %s", self.node_id, kind)

    def _emit_event(self, kind: str, severity: str, message: str) -> None:
        self.hub.send(Envelope.build(MsgType.EVENT, self.node_id,
                                     {"kind": kind, "severity": severity, "message": message}))

    def _reboot(self) -> None:
        logger.warning("[%s] 模拟节点重启：清空故障态并重建连接", self.node_id)
        with self._lock:
            self._pause_heartbeat = False
            self._fault_gpu_oom = False
            self._fault_latency_until = 0.0
            self._healthy = True
        self.local.release()
        self.local._load()
        self.hub.close()  # 触发 WSClient 自动重连 -> 重连后重新 register

    # ---- 推理路由 ----
    def infer(self, prompt: str) -> str:
        """优先本地推理；本地不可用时把请求路由到云端 vLLM。"""
        if self.local.loaded and self._healthy:
            return self.local.infer(prompt)
        # 路由到云端（云边协同推理：边做预处理/缓存，云做重推理）
        try:
            import json
            import urllib.request
            data = json.dumps({"model": CONFIG.vllm_model,
                               "messages": [{"role": "user", "content": prompt}]}).encode()
            req = urllib.request.Request(f"{self.cloud_http_url}/v1/chat/completions",
                                         data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())["choices"][0]["message"]["content"]
        except Exception as e:
            return f"[edge] 本地与云端推理均不可用: {e}"


def _main() -> None:
    node_id = os.getenv("EDGE_NODE_ID", "edge-1")
    region = os.getenv("EDGE_REGION", "factory-a")
    gpu = os.getenv("EDGE_GPU", "nvidia-t4")
    agent = EdgeAgent(node_id, region, gpu)
    agent.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        agent.stop()


if __name__ == "__main__":
    _main()
