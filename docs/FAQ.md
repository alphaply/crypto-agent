# FAQ

## 1. 本地能完整运行吗？

可以。现在本地运行方式是：

```bash
uv run dashboard.py
```

这会启动 FastAPI 服务；如果已经构建了前端，也会直接提供 React 页面。

## 2. 前端开发时怎么调试？

```bash
npm run dev --prefix frontend
```

Vite 会把 `/api` 请求代理到 `http://localhost:7860`。

## 3. 现在还是 Flask 吗？

不是。新的 Web 层已经迁移到：

- FastAPI
- React + Vite
- JWT 鉴权

旧的 Flask/Jinja 页面已经从这个分支清理掉。

## 4. 调度器是否还保留？

保留。你可以：

- 用 `uv run dashboard.py` 通过 Web 服务带起调度线程
- 或者用 `uv run main_scheduler.py` 只跑调度器

## 5. 为什么有些实盘接口会报交易所鉴权错误？

这通常是 `.env` 中的交易所凭证、IP 白名单或权限配置问题，不是前后分离迁移本身导致的。
