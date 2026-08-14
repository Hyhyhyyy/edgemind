"""云端服务入口（容器化场景）。

启动云端全部组件：vLLM 网关 + 控制器 + CloudHub + HTTP 控制面 + 诊断 Agent。
边端 Agent 由独立容器（edge 镜像）运行并通过 CloudHub 接入。
"""
from __future__ import annotations

import sys
import time

from common.config import CONFIG
from common.logging_setup import get_logger
from cloud.controller import Controller
from cloud.cloudhub import CloudHub
from cloud.vllm_gateway import VLLMGateway
from cloud import api as cloud_api
from agent.diagnoser import Diagnoser

logger = get_logger("cloud.serve", "cloud")


def serve() -> int:
    vllm = VLLMGateway(base_url=CONFIG.vllm_base_url, model=CONFIG.vllm_model)
    controller = Controller(vllm_gateway=vllm)
    cloudhub = CloudHub(controller)
    controller.cloudhub = cloudhub
    cloudhub.start()
    http_server = cloud_api.start_api(controller, cloudhub, vllm)
    diagnoser = Diagnoser(controller, vllm)
    diagnoser.start()
    logger.info("云端就绪：CloudHub=ws://0.0.0.0:%s  HTTP=http://0.0.0.0:%s  vLLM=%s",
                CONFIG.cloud_ws_port, CONFIG.cloud_http_port,
                CONFIG.vllm_base_url or "mock")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        diagnoser.stop()
        cloudhub.stop()
        http_server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(serve())
