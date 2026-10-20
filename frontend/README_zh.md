# DataLab 前端

DataLab 的 Vue 3、TypeScript、Vite、Tailwind 前端。

## 保留页面

- `/dataset/seek/:datasetId`：科学数据探查与分析。
- `/chat`、`/chat/:sessionId`：通用数据分析探查任务。
- `/chat/plugins`：Skill、MCP、Renderer 插件。
- `/chat/admin`：仅资源用量、任务、MCP、技能四项管理。

## 开发与部署

```bash
cd ..
./run.sh up -d --build
```

前端随 DataLab 的隔离 Compose 服务构建和部署，默认通过 `http://localhost:10030` 访问。容器更新统一从仓库根目录使用 `./run.sh`。

## 检查

```bash
npm run type-check
npm run build
node --test tests/*.test.mjs
```

## Docker

```bash
docker build -t datalab-frontend .
docker run --rm -p 10030:80 -e BACKEND_URL=http://backend:8000 datalab-frontend
```

完整部署请通过 `./run.sh` 使用仓库根目录的 `docker-compose.yml`，隔离环境默认配置见 `deploy/datalab/compose.env.example`。
