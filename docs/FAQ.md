# FAQ

## 1. 本地怎么启动？

```bash
uv run python -m backend.app
```

这个命令会同时启动 FastAPI 和后台 scheduler。

## 2. 前端开发时怎么调试？

```bash
npm run dev --prefix frontend
```

Vite 会把 `/api` 请求代理到 `http://localhost:7860`。

## 3. 现在还是 Flask 吗？

不是。

当前 Web 层已经迁移为：

- FastAPI
- React + Vite
- JWT 鉴权

旧的 Flask/Jinja 页面和对应入口已经移除。

## 4. 调度器还保留吗？

保留，但不再提供独立启动文件。

统一使用 `uv run python -m backend.app`，后端启动时会一起拉起调度线程。

## 5. 为什么有些实盘接口会报交易所鉴权错误？

通常是 `.env` 中的交易所凭证、IP 白名单或权限配置问题，不是前后分离改造本身导致的。
