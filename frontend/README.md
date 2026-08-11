# ai-ops-auto React console

这是 `ai-ops-auto` 的 React + TypeScript 运营台开发工程。它与 FastAPI 后端分开构建；
后端自带的 `/ui` 服务端页面不需要本目录。

## Local development

先在仓库根目录启动 API：

```bash
source .venv/bin/activate
ai-ops serve
```

再启动前端：

```bash
cd frontend
npm ci
npm run dev
```

Vite 开发服务器会把 `/api/*` 代理到 `http://127.0.0.1:8000`。如需改用其他后端地址，
设置 `VITE_API_BASE`。

前端请求失败时必须显式报错，不得用 mock 数据伪造查询或写入成功。这是运营控制面的数据真实性边界。

## Checks

```bash
npm run lint
npm run build
```

CI 使用 `npm ci` 从 `package-lock.json` 安装，然后执行上述两个命令。

## Production boundary

`npm run build` 将静态产物写入 `frontend/dist`。当前根目录 Dockerfile **不包含** React 构建产物；
部署时需要把 `dist` 交给静态托管/反向代理，或单独构建前端镜像。

当前 React 客户端没有完成与后端 `API_KEY`/UI session 的生产鉴权集成，因此应视为本地开发界面。
对外部署前需要先完成同源鉴权契约或在反向代理层受限访问，不要绕过后端鉴权。

不要把 API key 烘焙进 `VITE_*` 变量：Vite 变量会进入浏览器可见的 JavaScript。
对外部署应由同源反向代理处理 TLS 与鉴权边界。
