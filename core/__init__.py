"""《她的一天》纯逻辑层。

这一层不 import ``plugin.sdk``，可以脱离宿主直接跑：

    python -c "from core.signals import idle_seconds; print(idle_seconds())"

分层理由：「永远的陪伴」的 core/ 只判"数据长什么样"，mixins/ 才判
"要不要发请求"。本插件沿用同一纪律 —— 判定和持久化策略不混进 SDK 胶水层。
"""
