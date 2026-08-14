"""EdgeMind 一键演示：云 + 多边缘 + 诊断 Agent 端到端自愈。

运行：
  python demo/run_demo.py

流程：
  t=0    启动云端（CloudHub + HTTP 控制面）、2 个边端 Agent、诊断 Agent
  t~3s   边端完成注册、开始心跳/指标上报
  t=6s   注入 edge-2 心跳丢失  -> 自动标记 NotReady + 任务迁云 + 告警
  t=14s  注入 edge-1 GPU OOM   -> 降级小模型 + 路由到云 + 重启运行时
  t=24s  注入 edge-1 时延突增  -> 云端扩容 + 下沉边端 + 限流
  之后   控制台持续运行，可浏览器打开 http://localhost:8000 看实时看板，
         或另开终端 `python -m agent.troubleshoot --live` 做实时巡检。

说明：本演示零外部依赖，vLLM 默认走 mock；如需真实大模型推理，
设置环境变量 VLLM_BASE_URL 指向你的 vLLM OpenAI 兼容服务即可。
"""
from __future__ import annotations

import os
import sys
import time

# 把 src（云/边/Agent 包）与项目根（demo 包）加入导入路径
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # edgemind/
SRC = os.path.join(ROOT, "src")
for p in (SRC, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from common.config import CONFIG
from common.logging_setup import get_logger
from cloud.controller import Controller
from cloud.cloudhub import CloudHub
from cloud.vllm_gateway import VLLMGateway
from cloud import api as cloud_api
from edge.edge_agent import EdgeAgent
from agent.diagnoser import Diagnoser
from demo.scenarios import SCENARIOS

logger = get_logger("demo.runner", "demo")

# 让演示节奏更紧凑
CONFIG.heartbeat_interval = 2
CONFIG.heartbeat_timeout = 7
CONFIG.diag_interval = 3


def main() -> int:
    logger.info("==== EdgeMind 演示启动 ====")
    # 1) 云端
    vllm = VLLMGateway(base_url=CONFIG.vllm_base_url, model=CONFIG.vllm_model)
    controller = Controller(vllm_gateway=vllm)
    cloudhub = CloudHub(controller)
    controller.cloudhub = cloudhub
    cloudhub.start()
    http_server = cloud_api.start_api(controller, cloudhub, vllm)

    # 2) 边端（两个节点）
    edges = [
        EdgeAgent("edge-1", region="factory-a", gpu="nvidia-t4"),
        EdgeAgent("edge-2", region="factory-b", gpu="nvidia-t4"),
    ]
    for e in edges:
        e.start()

    # 3) 诊断自愈 Agent
    diagnoser = Diagnoser(controller, vllm)
    diagnoser.start()

    logger.info("等待边端注册与首次心跳…")
    time.sleep(4)

    # 4) 按时间线注入故障
    logger.info("开始按场景注入故障（控制台将展示自动自愈）")
    scenario_threads = []
    for node_id, kind, delay in SCENARIOS:
        def _inject(n=node_id, k=kind, d=delay):
            time.sleep(d)
            logger.info(">>> 注入故障 node=%s kind=%s", n, k)
            cloudhub.send(n, __import__("json").dumps({
                "type": "command", "node_id": n,
                "payload": {"command": "inject_fault", "params": {"kind": k}},
            }, ensure_ascii=False))
        import threading
        t = threading.Thread(target=_inject, daemon=True)
        t.start()
        scenario_threads.append(t)

    # 5) 让演示跑一会，观察自愈闭环
    try:
        for remaining in range(40, 0, -5):
            time.sleep(5)
            nodes = controller.get_nodes()
            ready = sum(1 for n in nodes if n.phase.value == "Running")
            open_inc = len(controller.open_incidents())
            logger.info("[状态] 节点 %d 在线/共 %d，未结故障 %d，已执行动作 %d",
                        ready, len(nodes), open_inc, len(controller.actions))
        logger.info("演示注入阶段结束。系统保持运行，可打开看板或运行实时巡检。Ctrl+C 退出。")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("收到中断，正在退出…")
    finally:
        for e in edges:
            e.stop()
        diagnoser.stop()
        cloudhub.stop()
        http_server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
