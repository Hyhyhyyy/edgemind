"""EdgeHub：边端 WebSocket 客户端（模拟 KubeEdge edgecore 中的 edgehub 模块）。

KubeEdge 的 edgehub 负责与云端 cloudhub 维持一条 WebSocket 长连接，
所有「边->云」的上报（状态码、设备孪生、元数据）和「云->边」的下发
都走这条隧道，并自带断线重连。本类用自研 ws.WSClient 实现同构能力，
把 Envelope 收发封装好，业务层只需关心 COMMAND 回调。
"""
from __future__ import annotations

from typing import Callable, Optional

from common.logging_setup import get_logger
from common.protocol import Envelope, MsgType
from common.ws import WSClient

logger = get_logger("edge.edgehub", "edge")


class EdgeHub:
    def __init__(self, cloud_url: str, node_id: str,
                 on_command: Callable[[Envelope], None]):
        self.node_id = node_id
        self.on_command = on_command
        self.client = WSClient(cloud_url, on_message=self._on_message,
                               on_open=self._on_open, on_close=self._on_close)

    def connect(self) -> None:
        self.client.connect()

    def _on_open(self) -> None:
        logger.info("[%s] 已连上 CloudHub", self.node_id)

    def _on_close(self) -> None:
        logger.warning("[%s] 与 CloudHub 断开，将自动重连", self.node_id)

    def _on_message(self, conn, text: str) -> None:
        try:
            env = Envelope.from_json(text)
        except Exception:
            return
        if env.type == MsgType.COMMAND.value:
            self.on_command(env)

    def send(self, env: Envelope) -> None:
        self.client.send(env.to_json())

    def close(self) -> None:
        self.client.close()
