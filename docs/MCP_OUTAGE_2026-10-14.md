# MCP 只读故障记录（2026-10-14）

初始诊断位置：开发机至公开域名 `crypto-agent.elpha.top`，时间 **2026-10-14 13:52–13:54 CST（Asia/Shanghai）**。初始阶段只有公网/已授权只读工具访问；随后主代理取得服务器 SSH 证据并修复标准 OAuth 路由，**14:05 CST** 完成公网及已授权 MCP 复验。没有重启容器、改变镜像或执行金融写入。此前 TLS 超时是用户会话中的历史观察，本轮未复现，不归因于已确认的 HTTP 404 故障。

| 检查 | 实测结果 | 失败层或范围 |
| --- | --- | --- |
| DNS | A 记录解析为 43.134.43.11 | DNS 当前可用 |
| 公网首页 `/` | HTTP 200，TLS 0.574 秒，总耗时 0.941 秒 | 公网 Web 可读 |
| 公网 `/health` | HTTP 200，TLS 3.023 秒，总耗时 3.369 秒 | HTTP 成功；尚未核验服务器内资源和完整健康正文 |
| `/.well-known/oauth-authorization-server/oauth` | HTTP 404，TLS 0.595 秒，总耗时 0.936 秒 | 标准发现路径缺失/路由或旧部署，TLS 成功 |
| `/.well-known/oauth-protected-resource/mcp` | HTTP 404，TLS 0.987 秒，总耗时 1.146 秒 | 标准资源发现路径缺失/路由或旧部署 |
| `/oauth/.well-known/oauth-authorization-server` | HTTP 200，JSON issuer 为公开域名 `/oauth`，token/authorize/register 地址一致，PKCE S256 | 兼容发现正常 |
| `/oauth/resource-metadata` | HTTP 200，JSON resource 为公开域名 `/mcp`，authorization_servers 为公开 issuer | 兼容资源发现正常 |
| 已授权 connector `list_profiles` | 成功，返回 spot 与 swap 两个 enabled 连接配置 | 实际已授权 MCP 读链路当前可用 |
| swap `get_balance` | 成功，约 5.400 秒 | 未保存金额明细，仅记可用性 |
| swap `get_orders`，ETH/USDT:USDT，limit=10 | 成功，约 5.752 秒 | 旧运行版本返回普通列表；不能据此确认全部条件通道覆盖 |
| swap `get_market`，ETH/USDT:USDT，1h，limit=5 | 成功，约 5.598 秒，ticker/K 线字段返回 | 单标的最小样本，不代表大批量或持续负载可用 |
| 服务器本机/容器内 health、状态与资源 | 后续取得，见下文补充 | Web 正常，资源有负载；不足以归因历史超时 |
| 同一时段代理 access/error、容器应用日志 | 后续 SSH 排查取得代理错误及容器状态 | 发现 404 有明确代理路径证据；历史 TLS 超时仍未复现 |

初始事实：公网 TLS、首页、兼容 OAuth 及已授权核心读取成功；两条标准发现路径 404。初始假设为入口精确路由或运行镜像版本差异。后续服务器证据确认了此处 404 的代理静态路径拦截；历史 TLS 超时的根因仍无证据。

## 服务器证据与现场修复

主代理取得 SSH 后确认：Web 与 scheduler 容器均运行，restart=0、oom=false，使用相同镜像 `sha256:f9eee469780dbe6533068f3acc563fcf51fd46789ebece9be857c484121186a0`；服务器映射端口 `127.0.0.1:31421/health` 为 HTTP 200。scheduler 没有 HTTP 服务，其继承 Web healthcheck 的 unhealthy 不能单独解释为应用宕机。

Nginx 主站点配置 `/www/server/panel/vhost/nginx/crypto-agent.elpha.top.conf` 中 `location /.well-known` 将发现路径作为静态文件处理。error log 实际记录了 OAuth authorization-server/oauth 和 protected-resource/mcp 文件不存在。该证据解释当前发现 404；不会解释 TLS 尚未完成的历史超时。

现场变更：保留原主配置，在 `/www/server/panel/vhost/nginx/extension/crypto-agent.elpha.top/codex-mcp-discovery-20261014.conf` 新增四条精确 OAuth 发现路径转发，upstream 为 `127.0.0.1:31421`，保留公网 Host 与 X-Forwarded-Proto，关闭缓存并声明 no-store。变更前/后 `nginx -t` 均通过，`nginx -s reload` 成功。没有容器重建、数据卷/密钥调整、数据库回滚或镜像替换；未知操作记录继续位于原数据卷。

回退：仅移除上述新 extension snippet，再执行 `nginx -t`，通过后 reload。原主配置未修改；无需回退数据库或重启容器。回退会恢复原标准发现路径 404，执行前需明确其影响。

## 修复后复验（14:05:22 CST）

| 检查 | 实测结果 |
| --- | --- |
| 公网首页 | HTTP 200，TLS 0.575 秒，总耗时 0.942 秒 |
| 公网 `/health` | HTTP 200，TLS 0.575 秒，总耗时 0.928 秒 |
| 标准授权服务器发现 | HTTP 200，issuer 为 `https://crypto-agent.elpha.top/oauth`，token/authorize/register 地址一致 |
| 标准资源发现 | HTTP 200，resource 为 `https://crypto-agent.elpha.top/mcp`，authorization_servers 指向公开 issuer |
| 已授权 `list_profiles` | 成功，约 3.548 秒 |
| swap `get_balance` | 成功，约 6.016 秒 |
| swap `get_orders`，ETH/USDT:USDT，limit=10 | 成功，约 4.405 秒 |
| swap `get_market`，ETH/USDT:USDT，1h，limit=5 | 成功，约 4.921 秒，ticker/K 线返回 |

复验只使用现有授权 connector 查询，未在命令行或日志中提供 Bearer，也未修改/撤销/提交订单。这里证明当前公开入口和上述最小读取恢复；批量/高并发治理仍以新增离线故障注入和后续应用版本部署验证为依据。运行镜像尚未包含本次本地并发/预算代码。

## 服务器资源补充（14:18–14:24 CST）

主机内存 3399 MiB、available 640 MiB，swap 已使用 708/1024 MiB；根盘使用 70%、剩余 18 GiB。14:23 的单次 Docker 资源观察中，Web CPU 0.29%、内存 393.8 MiB；scheduler CPU 105.47%、内存 1.205 GiB。容器内部通过 Python HTTP 请求 `/health` 返回 HTTP 200，database/config 均正常，scheduler.enabled=true、run_in_web=false。容器没有 curl，使用内置 Python 核验，没有安装依赖或改变进程。

后续仅筛选 Web 容器最近日志中的 health 路径及 HTTP 状态，最近 15 条均为 `/health` 200；未导出完整应用正文、交易决策或密钥配置。代理 error log 的文件不存在记录和 Web 容器健康访问分别对应入口路径层与应用 HTTP 层。

该资源观察说明 scheduler 当时有较高负载，不能证明历史 TLS 超时由此引起。后续 SSH 曾出现一次 banner EOF，重连成功；没有据此重启应用。上述证据与入口修复时间分别记录，不将异时资源采样伪称为故障发生时的状态。

本地故障层定位演练：离线测试令 regular 订单通道成功、algo 通道抛超时，工具返回明确 `missing_sources=[algo]` 而非空列表；令 ticker 成功/K 线失败，返回 partial 与原始 ticker 观察时间；占满 MCP 查询池后 Web/health 与独立认证仍响应。它们验证应用层诊断与隔离，不替代公网和服务器现场验收。

验收状态：已完成当前标准 OAuth 发现 404 的证据驱动修复及公网/认证读取复验；没有声称历史 TLS 超时已经根治。分层流程、记录模板及离线故障层演练已完成。生产容器仍运行原镜像，本次本地应用加固的发布/生产部署与这次代理现场修复分开验收。
