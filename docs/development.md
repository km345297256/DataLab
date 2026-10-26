# DataLab 开发与贡献指南

本文面向需要修改前端、后端、分析运行时或插件的开发者。部署步骤见 [部署与运维指南](deployment.md)，仓库约束以根目录 [AGENTS.md](../AGENTS.md) 为准。

## 1. 项目范围与代码结构

DataLab 来源于 AI-DATASEEK 的 `datalab` 分支，聚焦数据发现、获取、个人数据管理、探索、可视化和隔离分析。已经移除的 Claw、A2A、科学站点发布、知识库索引和泛平台管理不属于默认产品范围；恢复这些域需要明确的架构决定。

| 路径 | 职责 |
| --- | --- |
| `frontend/src/pages`、`components` | Vue 页面和用户界面组件 |
| `frontend/src/api`、`composables` | API 调用、认证与状态逻辑 |
| `frontend/src/visualizations` | 科学数据与文件可视化 |
| `backend/app/interfaces` | HTTP 路由、请求响应模型、依赖注入和中间件 |
| `backend/app/application` | 面向用户操作的应用服务与用例编排 |
| `backend/app/domain` | 分析任务、Agent、工具、领域模型和接口 |
| `backend/app/infrastructure` | MongoDB、Redis、MinIO、模型服务及 Docker 适配 |
| `sandbox/app` | 隔离执行、程序运行、文件与浏览器服务 |
| `sandbox/scripts`、`scientific_operators` | 分析辅助程序与科学计算操作 |
| `jupyter` | Notebook 镜像定义 |
| `plugin-host` | TypeScript / Cordis 插件宿主，通过 stdio 受后端管理 |
| `tools`、`data-skills` | 工具插件及数据技能包 |
| `deploy` | 可选服务和生产发布辅助程序 |
| `docs` | 子系统设计、协议、验收说明与历史变更记录 |

后端入口是 `backend/app/main.py`，API 前缀是 `/api/v1`。路由总装配位于 `backend/app/interfaces/api/routes.py`。新增功能应沿现有分层实现：接口层完成协议处理，应用层协调用例，领域层表达规则，基础设施层处理外部系统。

历史专题文档可能记录旧方案或阶段验收；涉及当前行为时，以实现、根目录贡献约束、配置模板及本指南为准，不把历史记录当作当前默认配置。

## 2. 本地依赖

| 组件 | 开发环境 |
| --- | --- |
| 前端 | Node.js 和 npm；生产构建镜像使用 Node.js 24 |
| 插件宿主 | Node.js `>=22.19.0`，具体要求见 `plugin-host/package.json` |
| 后端 | Python `>=3.12` 与 uv |
| 沙箱 | `pyproject.toml` 声明 Python `>=3.10`，依赖是否支持具体平台与 Python 版本还需按锁文件安装验证 |
| 完整分析 | Docker、MongoDB、Redis、MinIO、可用的沙箱和 Jupyter 镜像及对象挂载环境 |

推荐统一使用满足插件宿主要求的 Node.js。安装仓库锁定依赖：

```bash
(cd frontend && npm ci)
(cd plugin-host && npm ci && npm run build)
(cd backend && uv sync --dev)
(cd sandbox && uv sync --dev)
```

`backend/uv.lock`、`sandbox/uv.lock` 和各 npm 锁文件应与依赖声明一同维护。沙箱包含科学计算、地理信息和文件格式依赖，完整运行环境还依赖镜像中的系统库；在宿主直接安装 Python 包不等于已经复现容器运行环境。沙箱 `dev` 依赖还包含 HTTP 测试客户端以及 `psims`、`pyteomics`、`rosettasciio` 等独立科学格式校验库，用于生成、读取或交叉核验测试样例；仅安装生产依赖无法覆盖完整测试。

## 3. 开发运行方式

### 3.1 前端连接已运行的完整栈

先按部署指南启动 DataLab，再执行：

```bash
cd frontend
BACKEND_URL=http://localhost:10030 npm run dev
```

Vite 默认监听 `5173`。`BACKEND_URL` 启用开发服务器对 `/api` 的代理，并支持 WebSocket；这里指向完整栈前端端口，后者继续代理到后端。浏览器仍通过开发站点访问 API。

`VITE_API_URL` 可以配置独立 API 地址，但通常优先采用同源代理，减少认证和跨域配置差异。不要把内部 Docker 地址写入浏览器可见配置。

### 3.2 修改后端

需要完整数据和任务能力时，优先用 Compose 构建、更新受影响的 backend 服务，保持数据库、对象存储和沙箱网络的一致性。

单独启动后端适用于已经准备好外部依赖的开发环境：

```bash
cd backend
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

此方式从当前目录读取 `.env`。应准备私有 `backend/.env` 或由可信运行环境注入配置，包括 `MONGODB_URI`、`REDIS_HOST`、`MINIO_ENDPOINT`、认证密钥和模型设置。根目录 Compose 不会自动把数据库端口发布给宿主；不要假设启动普通栈后就可以从宿主连接 `localhost:27017`。

后端必须使用 `AUTH_PROVIDER=password` 和至少 32 字符的稳定 JWT 密钥。分析容器还需能访问 `OBJECT_MOUNT_BACKEND_URL`，并使用匹配的 Docker 网络；`127.0.0.1` 在容器内不是宿主后端的地址。单进程启动成功不代表容器执行链路已打通。

### 3.3 修改沙箱与插件

沙箱完整能力依赖 Dockerfile、Supervisor、浏览器、系统工具和 Python 依赖。调整后需重建对应沙箱镜像，并用新任务验证；不要只验证本机模块导入。

插件宿主可以独立构建与测试：

```bash
cd plugin-host
npm run build
npm test
```

后端默认可从当前仓库发现构建后的插件宿主与工具目录，也可通过 `PLUGIN_RUNTIME_*` 配置覆盖。插件宿主由 FastAPI 通过 stdio 管理，无需额外开放网络端口。

## 4. 测试与验收

提交变更前，从仓库根目录执行贡献指南规定的检查：

```bash
(cd frontend && npm run type-check && npm run build)
(cd backend && uv run pytest)
(cd sandbox && uv run pytest)
docker compose config --quiet
```

Compose 检查需要有效的本机 `.env`。使用 `--quiet` 避免将展开后的私有环境变量打印到日志。任何检查失败都应记录原因并修复，不能把未运行、跳过或仅收集测试视为通过。

按变更范围补充适当验证：

| 变更范围 | 检查 |
| --- | --- |
| 前端状态、工具函数或组件行为 | `(cd frontend && npm test)` |
| 浏览器交互、认证、流式会话或布局 | `(cd frontend && npm run test:browser)` |
| 插件宿主、工具定义或协议 | `(cd plugin-host && npm test)`，并运行相关后端契约测试 |
| 生产发布辅助程序 | `python3 -m unittest discover -s deploy/production -v` |
| 数据读取、挂载或文件交付 | 相关存储与挂载测试，加一次真实小文件分析和结果下载验收 |

浏览器测试门槛位于 `frontend/tests/browser/run-gate.mjs`，要求 Chromium 和 WebKit 两个引擎。需要安装 Playwright 和对应浏览器；缺失运行时会失败，不会自动跳过。可以通过 `VISUALIZATION_PLAYWRIGHT_MODULE` 指向已安装的 Playwright 模块，通过 `VISUALIZATION_BROWSER_EXECUTABLE` 指定 Chromium 可执行文件；单场景可以使用 `DATALAB_BROWSER_ENGINE` 选择引擎。涉及运行地址和测试账户的要求应阅读具体场景脚本，不把测试账号或密码写入仓库。

[`scripts/check-regressions.sh`](../scripts/check-regressions.sh) 提供更完整的组合检查，包含可视化契约同步、前端测试与构建、浏览器矩阵、插件宿主、Compose、发布辅助程序、持久化契约和分析基准：

```bash
bash scripts/check-regressions.sh --local
# 或在已经具备相应本地镜像时：
bash scripts/check-regressions.sh --containers
```

容器模式复用现有后端、沙箱镜像，在一次性容器中补充测试依赖，并只读挂载源码；不挂载生产数据卷或传入生产服务端点。需要指定其他已构建镜像时使用 `DATALAB_TEST_BACKEND_IMAGE`、`DATALAB_TEST_SANDBOX_IMAGE`。两种模式的前端与浏览器检查仍在本机运行；容器模式不免除本机 Node.js 和浏览器运行时要求。科学计算门槛通过 `AI_DATASEEK_REQUIRE_GEOSCIENCE_STACK=1` 要求完整地学运行栈，缺失系统工具应修复环境，不能用跳过替代验证。

优先为业务边界、错误处理和回归问题增加有意义的测试。纯文档和可逆低影响修改不需要为了覆盖率编写重复实现的测试。

## 5. 数据与执行边界

以下规则适用于前端、API、后台任务、插件、沙箱和 Jupyter 的所有改动。

1. **对象存储是持久来源。** 下载数据、上传文件和交付结果保存为对象及其文件引用。个人数据和任务输入的选择只记录拥有关系与引用，不复制底层对象。
2. **输入只读，计算可写。** 仅显式选中的输入可见；通过对象挂载进入隔离环境，计算工作区与输入分离。
3. **不做全量输入缓存。** 对象读取可按需传输字节范围并使用有界缓冲区。挂载不可用时报告错误，不回退为整个文件下载、会话复制或共享缓存。
4. **不泄露内部身份。** bucket key、对象存储凭据、宿主路径保持在后端。浏览器响应、URL、localStorage 和 sessionStorage 不得持久化宿主真实路径。
5. **保留兼容而不暗迁移。** `hybrid` 新写 MinIO、兼容读旧 GridFS，旧 `gridfs` 配置是其别名。保留已有数据集与归属，不新增目录登记入口；旧源若必须建立全量分析副本才能读取，应拒绝并说明。
6. **交付结果需持久化。** 生成结果必须形成可下载的对象存储文件引用，不能把获取的源数据重新包装成“新生成结果”。
7. **依赖能力目录不是准入清单。** 不按库名、导出名、API 或属性名建立允许或拒绝列表。缺失依赖可以在任务隔离环境中准备，同时保持授权数据访问与只读输入边界。

对历史宿主数据路径的处理必须保留 `DATASET_HOST_PATH_ALLOWLIST` 与真实路径校验，不能用字符串前缀或未经验证的路径替代。

## 6. 常见扩展入口

### 新数据连接器

参考 [数据连接器标准](data-connector-standard.md)，保持发现结果、下载状态、授权信息和文件引用之间的明确关系。网络下载的临时恢复分片与对象提交是不同生命周期；对象成功提交后回收暂存。

### 新可视化能力

参考 [可视化插件契约](visualization-plugin-contract.md) 和 [科学可视化集成](scientific-visualization-integration.md)。大文件应继续使用窗口、范围或分块读取；不要为了显示预览先完整下载到服务端缓存。浏览器端 SDK、worker 和静态资源变动需要验证实际构建产物与加载路径。

### 新 Agent 工具或技能

参考 [Cordis 插件架构](cordis-plugin-architecture.md)、[技能创建流程](skill-creation-pipeline.md) 和 [数据技能包规范](data-skill-package-v1.md)。保持工具协议、错误表达、用户确认和任务状态一致，不将私有配置放进模型可见参数或前端事件。

### 分析生命周期与交付

参考 [分析交付契约](analysis-delivery-contract.md)、[运行恢复](analysis-runtime-recovery.md)、[任务状态](analysis-jobs.md) 和 [事件回放](session-events-and-replay.md)。新增恢复逻辑应校验原始输入身份与挂载状态，避免重新执行时错误复用结果、遗失用户输入或报告虚假完成。

## 7. Git 与文档约定

当前仓库默认工作分支是 `datalab`。尊重用户明确选择的分支；不要为了同步代码自动切换或重写历史。

提交前检查工作区和暂存范围，保留无关修改，排除 `.env`、私有发布快照、凭据、日志、缓存和本地运行结果。锁文件、必要测试夹具和真正的项目资源应随相应代码提交。

提交与推送必须由用户明确要求，提交说明使用中文。示例工作流：

```bash
git status --short --branch
git diff --stat
git diff --check
# 逐项选择本次变更后再 git add
git diff --cached --stat
git commit -m "完善数据分析功能与项目说明"
git push origin datalab
```

公开文档应说明现有行为、使用步骤、验证方式与限制。涉及配置时使用模板名和占位示例，禁止包含真实密钥、私有用户数据或私有服务器地址。新增 Markdown 文档应从项目说明或对应专题入口链接，修复功能行为时同步检查旧说明是否仍准确。

变更通过规定检查后，按项目约定用 `./run.sh` 更新受影响的 DataLab 服务并验证健康与变更行为；用户明确要求跳过部署时按其要求执行。推送 DataLab 不意味着更新已有 DataSeek 部署。
