# Docker 构建与推送记录（2026-10-14）

状态：构建、隔离烟测、推送与远端摘要核验均完成。沿用 `alphaply712/crypto-agent`，平台 `linux/amd64`。生产容器尚未更新。

| 项目 | 结果 |
| --- | --- |
| 版本标签 | `alphaply712/crypto-agent:workspace-20261014-43658cfb2e7f` |
| 常用标签 | `alphaply712/crypto-agent:latest` |
| 两标签共同 digest | `sha256:381a744bc2aa989a9f3fa01861a95ac3a1b1a9274738825a87dd529a30383942` |
| AMD64 runtime manifest | `sha256:5a38ec0c5781e84612f6527e362d54ac1031962dff5bee2e5070c741b72219e6` |
| 构建源 SHA256 | `43658cfb2e7f23c24cd49fbee69516ba8990fa98743a0cd24950ad0106fb2fe4` |
| 构建基础 Git HEAD | `8594060d3e6513560423f5ff81b2a42fedd467fd` |
| 推送前 registry latest digest | `sha256:be0b5ee3fdc89a148840c77f459405ab70b1e9d25a99babdef5df77c889242e5` |

当前工作区有未提交代码，镜像包含本轮独占管理、MCP 加固、记忆/收益实现及工作区已有的 provider-model-discovery 代码。版本标签使用源码摘要，不将未提交内容标作旧 Git commit 的构建。此次未执行 OpenSpec 归档、Git 提交/推送或生产 rollout。

构建采用最新前端产物及 `Dockerfile.prebuilt`。在冻结的最小上下文中复制 Git 可见的 backend 文件、锁文件、README 和前端 dist；没有复制 .env、pricing.json、交易数据库、日志、.agents、测试或本地忽略文件。上下文 218 文件、5657476 字节；镜像中 216 个实际复制的源码/前端文件逐一与清单 SHA256 核对通过。

验证：
- 前端重新构建通过；补充 provider-model-discovery 回归 37 项通过。
- 空数据目录、`--network none`、`ENABLE_SCHEDULER=false`、`RUN_SCHEDULER_IN_WEB=false` 的临时容器：health、前端、管理员登录、MCP diagnostics、配置、记忆、收益路由及重复 schema 初始化全部通过。
- 在本次镜像的 Linux runtime 中验证金融 scope 文件锁：存活 worker 阻止竞争、进程退出释放 OS 锁、原未知操作仍保留数据库 reservation，不因进程退出自动允许新提交。
- 构建前源与推送前工作区匹配；两远端标签均解析至同一 digest。远端版本标签核验首次遇到一次 EOF，只读重试成功，没有重发构建或交易。
- OpenSpec strict 校验通过。临时烟测容器已清理，未挂载生产数据库或执行金融调用。

拉取：
```bash
docker pull alphaply712/crypto-agent:workspace-20261014-43658cfb2e7f
# 精确固定本次构建：
docker pull alphaply712/crypto-agent@sha256:381a744bc2aa989a9f3fa01861a95ac3a1b1a9274738825a87dd529a30383942
```

生产更新是单独的后续操作。Web 和独立 scheduler 应使用同一 digest，保留原共享数据卷及密钥；步骤见 [部署指南](DEPLOYMENT_GUIDE.md)。

本地冻结输入及来源清单保留于：
- `C:\Users\ASUS\AppData\Local\Temp\crypto-agent-image-mx9kg2ot`
- `C:\Users\ASUS\AppData\Local\Temp\crypto-agent-image-mx9kg2ot-source.json`

这两个临时路径用于本机复核，不是服务器部署路径。
