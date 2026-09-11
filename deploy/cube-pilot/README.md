# 云端分析试点准备包（未部署）

Cube Core（[仓库](https://github.com/cube-js/cube)，⭐ 20,797，2026-09-10 API核对；[官方说明](https://cube.dev/blog/cube-dev-raises-62m-to-accelerate-cubejs-development)记载2019年3月开源）。本次查询最新release为v1.7.36；此包没有拉取或运行镜像，不把release标签当作镜像摘要核验。

`compose.yaml`按[官方部署组件](https://docs.cube.dev/admin/deployment/core)准备一套API、刷新进程、Store Router和Store Worker，仅开放回环4400，Store不映射公网端口，磁盘卷独立。参数缺失时Compose拒绝启动。容器内存上限合计3584MiB只是试点约束，不是官方最低配置或已验证容量；现有2GiB云主机不能承载，需独立资源后实测调整。

必须先供应隔离的BLUE只读影子数据源及经过核验的镜像摘要，再补官方日销量模型、历史修订刷新键、店铺行权限和对应scheduledRefreshContexts。GREEN的数据源、凭证、schema/预聚合命名空间另行隔离，不能指向同一BLUE分析上下文。凭证不进入仓库或发布包。

当前`conf/cube.js`主动拒绝查询，定时刷新上下文为空；没有接入ERP请求、启用业务模型、建立数据通道、导出业务数据或启动容器。先按[验收记录](../../docs/RADAR_PRECOMPUTE_20260910.md)核对完整业务口径，再解除拒绝查询并运行影子对比。不能将这个准备包称为可用的生产Cube服务。

取得资源后顺序：核验镜像摘要及配置 → 独立BLUE源只读连通 → 官方销量模型与覆盖/修订验证 → 单并发预聚合与重启持久性 → 端到端/故障/权限/资源测试 → 决定是否接入ERP。停止试点仅停止此Compose项目，保留分析数据卷；不操作代理、仲裁或MySQL服务。
