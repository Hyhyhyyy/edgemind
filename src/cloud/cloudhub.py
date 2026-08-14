"""CloudHub：云端 WebSocket 网关（模拟 KubeEdge CloudCore 中的 cloudhub 模块）。

KubeEdge 中 cloudhub 是云端与所有边缘节点建 WebSocket 长连接的入口，
负责把云侧下发的消息按节点路由出去，并把边端上报的消息交给
EdgeController / DeviceController 处理。这里做同构的精简实现：

- 接受边端 EdgeHub 的长连接（自研 ws.WSServer）；
- 维护 node_id <-> 连接 的路由表（断线重连后自动更新）；
- 把每条 Envelope 交给 Controller 处理；
- 提供 send(node_id, text) 按节点定向下发指令。
"""
from __future__ import annotations

import threading
from typing import Optional

from common.config import CONFIG
from common.logging_setup import get_logger
from common.protocol import Envelope
from common.ws import WSServer, Connection
from cloud.controller import Controller

logger = get_logger("cloud.cloudhub", "cloud")


class CloudHub:
    def __init__(self, controller: Controller):
        self.controller = controller
        self._node_to_peer: dict[str, str] = {}
        self._peer_to_node: dict[str, str] = {}
        self._lock = threading.Lock()
        self.server = WSServer(
            host=CONFIG.cloud_ws_host,
            port=CONFIG.cloud_ws_port,
            on_message=self._on_message,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
        )

    def start(self) -> None:
        self.server.start()
        logger.info("CloudHub 监听 ws://%s:%s", CONFIG.cloud_ws_host, CONFIG.cloud_ws_port)

    def _on_connect(self, conn: Connection) -> None:
        logger.debug("新边端连接: %s", conn.peer_id)

    def _on_disconnect(self, conn: Connection) -> None:
        with self._lock:
            node_id = self._peer_to_node.pop(conn.peer_id, None)
            if node_id and self._node_to_peer.get(node_id) == conn.peer_id:
                self._node_to_peer.pop(node_id, None)
        if node_id:
            logger.warning("边端连接断开: node=%s peer=%s", node_id, conn.peer_id)

    def _on_message(self, conn: Connection, text: str) -> None:
        try:
            env = Envelope.from_json(text)
        except Exception:
            logger.warning("收到非法消息: %s", text[:120])
            return
        # 维护路由表：把当前连接绑定到 node_id
        with self._lock:
            self._node_to_peer[env.node_id] = conn.peer_id
            self._peer_to_node[conn.peer_id] = env.node_id
        self.controller.handle(env)

    def send(self, node_id: str, text: str) -> None:
        with self._lock:
            peer = self._node_to_peer.get(node_id)
        if peer:
            self.server.send(peer, text)
        else:
            logger.warning("无法下发指令：节点 %s 当前无在线连接", node_id)

    def stop(self) -> None:
        self.server.stop()
