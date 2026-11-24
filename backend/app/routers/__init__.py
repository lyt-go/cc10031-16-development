"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import gap_evidence, resource

# 证据包专用路由含固定路径，先于通用资源路由注册，避免被 /{module} 动态抢占。
ROUTERS = [gap_evidence, resource]
