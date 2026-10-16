# 仓库指南

## 沟通方式
- 用户是新手（小白），对代码和技术细节不熟悉。
- 回复用户时使用简单、通俗易懂的话，少用术语；必须用术语时用一句话解释清楚。
- 说明改动时先讲清楚「做了什么、为什么做、对用户有什么影响」，不要只罗列代码或命令。
- 需要用户做决定时，给出推荐选项并说明各自的影响，避免让用户自己猜。
- 结论先行，细节在后；用户追问时再展开。

## 需求范围与确认
- 只实现用户明确要求的行为。
- 不得自行增加业务需求、替代数据源、跨交易所查询、自动降级或默认值填充。
- 未明确的业务行为先提出问题，不得直接写入 specs、design 或 tasks 当作已确认需求。
- 必要的内部实现细节可以自行决定，但会改变查询范围、结果含义或错误处理方式的选择必须先确认。
- 建议与已确认需求分开列出。

## 项目结构与模块划分
核心交易逻辑位于 `backend/` 下。

- `backend/app/`：FastAPI 应用、JWT 鉴权、路由、服务层
- `backend/app/core/scheduler.py`：调度器实现
- `backend/agent/`：agent 图、工具、提示词模板
- `backend/utils/`：行情数据、指标、日志、LLM 辅助工具
- `backend/config.py`：运行时配置加载
- `backend/database.py`：数据库访问层
- `frontend/`：React + Vite 前端
- `docs/`：运维与产品文档
- 运行时状态：`.env`、`trading_data.db`

## 构建、测试与开发命令
- `uv sync`：根据 `pyproject.toml` / `uv.lock` 安装 Python 依赖
- `npm install --prefix frontend`：安装前端依赖
- `uv run python -m backend.app`：在 `http://localhost:7860` 启动 FastAPI 与调度器
- `npm run dev --prefix frontend`：在 `http://localhost:5173` 启动 React 开发服务器
- `uv run backend/utils/test_agent_connection.py`：冒烟测试模型/API 连通性
- `npm run build --prefix frontend`：构建前端以供 FastAPI 静态托管

## 编码风格与命名约定
遵循 Python 3.10+ 约定，使用 4 空格缩进与 PEP 8 风格。
函数与变量使用 `snake_case`，类使用 `PascalCase`，模块名保持简短。
保持 FastAPI 路由处理函数精简，可复用逻辑移入 `backend/app/services/`。
优先使用带类型标注的函数签名，并用 `pydantic` 模型承载结构化 API 载荷。

前端应使用现代 React 模式，采用函数组件与 hooks。

## 测试指南
目前尚无强制执行的自动化测试套件，现有验证方式为：

- `uv run backend/utils/test_agent_connection.py`
- `npm run build --prefix frontend`
- 手动验证关键路由以及 chat/dashboard/admin 流程

新增测试时，放在 `tests/` 下并以 `test_*.py` 命名。

## 提交与 Pull Request 指南
使用聚焦的前缀式提交信息，例如 `feat: ...`、`fix: ...`、`refactor: ...`。

PR 应包含：
- 目的与范围
- 受影响的模块或 API
- 手动验证步骤
- 涉及 React UI 变更时，尽量附上截图

## 安全与配置提示
- 切勿提交真实 API key 或密钥
- `pricing.json` 仅保留在本地
- 非本地环境使用强 `ADMIN_PASSWORD`
- JWT 签名使用 `JWT_SECRET`，必要时回退到已有的本地密钥

## 文档维护
- 保持 `README.md` 与 `docs/` 和实际的 FastAPI/React 行为同步
- 当 Flask/Jinja 流程已不存在时，移除对它们的引用

## OpenSpec 归档与发布
- 归档请求默认包含将所选变更同步进主规范并发布已验证的变更，除非用户明确缩小范围。
- 遵循 `openspec/specs/archive-release-workflow/spec.md` 与 `docs/DEPLOYMENT_GUIDE.md`。在将变更移入带日期的归档目录前，先校验同步后的规范与已完成的任务。
- 仅暂存该变更的实现、测试、文档、同步后的规范与归档。保留无关的本地文件，包括未跟踪的 `.agents/` 内容。在 `beta` 上以聚焦的信息提交；发版时绝不强推或丢弃无关工作。
- 提交后运行 `powershell -NoProfile -File scripts/release-archived-change.ps1 -ArchiveName <YYYY-MM-DD-change-name>`。该脚本会重建前端与 Docker 镜像，运行隔离的、禁用调度器的冒烟测试，推送 `origin/beta`，并发布 `alphaply712/crypto-agent:git-<short-sha>` 与 `:latest`。
- 报告归档路径、检查项、Git commit 与镜像 digest。若某步骤失败，停止依赖它的后续步骤，并报告确切已完成与待完成的阶段；仅归档文件并不意味着发布成功。
- 发布镜像不会重启生产容器。生产上线是单独的动作，需用户请求后再执行。
