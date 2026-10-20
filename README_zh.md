<p align="center">
  <img src="frontend/public/datalab-logo.png" alt="DataLab 标志" width="96" />
</p>

# DataLab

[English](README.md) | 中文

DataLab 是以对话为入口的数据探索与分析实验室，将数据发现、文件获取、可复用 Data Skills、隔离分析与成果交付串成完整流程。

本独立项目以 [AI-DATASEEK 的 `datalab` 分支](https://github.com/km345297256/AI-DATASEEK/tree/datalab)的提交 [`5efc7d6`](https://github.com/km345297256/AI-DATASEEK/commit/5efc7d66dcc3b83c40aee47813d422fd3c2701f0)为初始基线，原项目更早源自 AI-MANUS。DataLab 仓库为 [km345297256/DataLab](https://github.com/km345297256/DataLab)，以清理后的初始快照发布，不包含上游 Git 历史。项目保留原有许可证和归属声明；历史文档及兼容存储标识中仍可能使用 DataSeek 名称。

## 主要能力

- 版本化 Data Skill 包、流程分类、任务内显式选择，以及可复用的 CSV 质检样例，详见 [Data Skills 仓库](data-skills/README.md)。
- 统一的数据连接器契约，内置 NCBI Datasets、GEO、SRA 检索与限额内文件下载，详见 [能力与边界](docs/data-connector-standard.md)。
- 对话检索结果勾选、文件导入任务沙盒，以及使用已选文件继续分析。
- 运行历史、实际技能版本凭据、来源记录与文件内容摘要；运行历史支持追溯，自动恢复执行仍待实现。
- `POST /api/v1/datasets/submissions` 第三方临时数据集提交接口。
- `/dataset/seek/:datasetId` 科学数据探查：递归展示文件、模型生成分析与可视化问题、只读挂载数据目录。
- 通用数据分析探查任务：`/chat` 与 `/chat/:sessionId`。
- Skill、MCP、Renderer 三类插件及数据成果预览。
- 系统管理仅保留：资源用量与配置、任务管理、MCP 管理和技能管理。
- 会话历史、分析文件和沙箱桌面。
- 用于运维和容量分析的 Token 用量统计，不设置用户额度。
- 统一 ModelDriver、单次请求上下文与执行超时边界，以及仅含元数据的上下文压缩追踪，详见 [模型驱动与上下文预算](docs/model-driver-and-context-budget.md)。
- 领域 Preset 与 Cordis 工具按需加载，以及默认关闭的受限 Code Mode、领域 SubAgent 试点，详见 [领域预设与实验执行模式](docs/domain-presets-and-code-mode.md)。

为保证这些功能可真实运行，Agent 执行、会话事件、安全审核、审计、权限、文件存储、MongoDB、Redis 和 Sandbox 等传递依赖也会保留。详细边界见 [架构范围说明](docs/architecture-scope.md)。

## 运行拓扑

```text
浏览器 -> Frontend -> Backend -> MongoDB / Redis / MinIO
                              \
                               -> 每任务独立 Docker Sandbox
                                    -> 数据集服务器目录（只读）
```

DataLab 使用 `docker-compose.yml`，Compose 项目名和资源前缀均默认为 `datalab`，前端默认端口为 `10030`，应用镜像使用 `datalab-*` 命名。网络与数据卷和已有 DataSeek 部署隔离；数据库名、文件桶名等内部标识保留兼容值。

## 快速启动

要求：Docker 20.10+、Docker Compose。

```bash
cp .env.example .env
# 修改 API_KEY、API_BASE、MODEL_NAME 以及 MINIO_SECRET_KEY。
# 将 SERVER_HOST 设置为浏览器实际使用的访问地址。
./run.sh up -d --build
```

访问 `http://localhost:10030`。

DataLab 固定采用免登录模式。浏览器和 API 请求均不使用 Bearer Token
或 `X-API-Key`，所有调用者统一以系统内置管理员身份操作。Token 消耗只用于
用量报表，不校验用户额度，也不从用户余额中扣减。
模型运行时另外约束单次请求的估算上下文和执行超时，不设置任务累计 Token 或调用次数额度。

如果宿主机使用 Snap 版 Docker，需要配置：

```env
DATASET_DOCKER_HOST_ROOT=/var/lib/snapd/hostfs
```

`DATASET_HOST_PATH_ALLOWLIST` 控制允许访问的数据根目录。系统会先校验第三方提交的服务器目录，再以只读方式挂载到分析沙箱。

## 开发与更新

容器化运行与更新统一使用支持的 Compose 配置：

```bash
./run.sh up -d --build
```

可选部署覆盖模板见 [deploy/datalab/compose.env.example](deploy/datalab/compose.env.example)。覆盖默认值时，项目名、资源前缀、镜像名和访问地址应保持一致。本次项目改名不会迁移或修改已有部署的数据。

常用检查：

```bash
cd frontend && npm ci && npm run type-check && npm run build
cd ../backend && uv sync && uv run pytest
cd ../sandbox && uv sync && uv run pytest
docker compose config --quiet
```

## 第三方接入

用户可以通过 `/datasets` 探查数据集，通过 `/dataset/setup` 登记数据。第三方业务系统还可以单独调用临时数据集提交接口，并将用户跳转至 `/dataset/seek/:datasetId`，详见 [第三方接入接口文档](docs/dataset-seek-third-party-api.md)。

## 项目结构

```text
DataLab/
├── frontend/   Vue 3 数据探查界面
├── backend/    FastAPI、Agent、插件与系统管理接口
├── sandbox/    隔离的科学数据分析环境
├── data-skills/ 版本化技能包、样例与贡献说明
├── data-connectors/ 连接器契约与声明
├── docs/       接口与架构文档
└── docker-compose.yml
```

## 安全说明

- DataLab 不提供应用层调用者认证，只能部署在可信网络中。
- 如需跨出可信网络或接入公网，必须先通过 VPN、防火墙 IP 白名单、带认证的反向代理或 API 网关实施网络层访问控制，并启用 HTTPS。
- 任何能够访问服务的调用者都拥有共享系统管理员权限，系统不提供按调用者隔离或审计归属。
- 模型密钥和真实服务器路径不得出现在 URL 或浏览器存储中。
- `DATASET_HOST_PATH_ALLOWLIST` 应只配置专用数据目录。
- 数据集宿主机路径仍须通过该白名单校验，并固定以只读方式挂载到分析沙箱；真实路径不得返回浏览器，也不得持久化到 URL、`localStorage` 或 `sessionStorage`。
- Backend 挂载 Docker Socket 后可管理本机容器，只能部署在可信主机。
- 生产部署前必须替换 `.env.example` 中的全部示例密钥。

## 许可证

见 [LICENSE](LICENSE)。
