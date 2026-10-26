# DataLab 部署与运维指南

本文说明仓库当前的 Compose 部署方式、配置项、存储边界和升级检查。命令默认从仓库根目录执行；项目功能与使用流程见 [项目说明](../README_zh.md)，开发流程见 [开发指南](development.md)。

## 1. 部署身份与服务组成

DataLab 是独立项目。默认使用 `docker-compose.yml`、Compose 项目名 `datalab`、资源前缀 `datalab`，应用镜像使用 `datalab-*` 名称。不要复用现有 DataSeek 的网络、数据卷、服务名或部署入口。

| 服务 | 职责 | 默认对外端口 |
| --- | --- | --- |
| `frontend` | Nginx 托管 Vue 应用，并代理 API、流式事件和 WebSocket | `10030`，容器内为 `80` |
| `backend` | FastAPI、账户认证、分析调度、数据管理、插件运行时 | 不直接发布；应用网络内使用 `8000` |
| `mongodb` | 用户、会话、文件引用、任务状态等元数据 | 不直接发布 |
| `redis` | 任务协调、缓存、认证相关状态 | 不直接发布 |
| `minio` | 上传文件、采集数据和分析结果的持久对象存储 | 不直接发布 |
| `sandbox-image` | 准备分析镜像，执行 `/bin/true` 后退出 | 无 |
| `jupyter-image` | 准备 Notebook 镜像，执行 `/bin/true` 后退出 | 无 |

实际分析沙箱、只读对象挂载辅助容器和 Jupyter 容器由后端按任务创建。`sandbox-image`、`jupyter-image` 显示 `Exited (0)` 属于正常状态，不能据此判定分析服务异常。

MongoDB 数据库名 `ai_dataseek` 和 MinIO bucket 名 `ai-dataseek-files` 是保留的内部兼容标识。它们位于 DataLab 自己的数据库服务和数据卷中；不要仅为统一命名而修改，也不要隐式迁移现有数据。

## 2. 环境准备

需要 Docker Engine 或可用的 Docker 环境，以及支持当前 Compose 文件的 Docker Compose。`run.sh` 优先使用 `docker compose`，同时兼容存在的 `docker-compose` 命令。使用生产发布辅助程序还需要 Python 3。

分析输入采用 FUSE 对象挂载：Docker 所在主机必须支持 `/dev/fuse` 和共享挂载传播，挂载辅助容器必须能访问后端内部对象网关。不要把“前端页面可以打开”当作对象挂载已可用；非 Linux 宿主或受限 Docker 环境应先完成一次真实输入分析验收。

构建前检查 Docker 磁盘占用和宿主可用空间：

```bash
docker version
docker compose version
docker system df
df -h
```

前端科学可视化依赖、分析镜像和 Jupyter 镜像均需要一定构建空间；前端 Dockerfile 为构建阶段配置了 4 GiB Node 堆上限。应结合实际并发量配置 Docker 内存和磁盘，不要通过删除数据卷释放构建空间。

## 3. 首次配置

```bash
git clone --branch datalab https://github.com/km345297256/DataLab.git
cd DataLab
cp .env.example .env
chmod 600 .env
```

已有 `.env` 时保留并按差异补充配置，不要再次复制覆盖。`.env` 是本机私有配置，应由编辑器修改，不要作为 Shell 脚本执行、提交到 GitHub 或放入镜像。

### 必须核对的配置

| 配置 | 用途与要求 |
| --- | --- |
| `COMPOSE_PROJECT_NAME`、`STACK_PREFIX` | 保持 `datalab`，确保与其他部署隔离 |
| `API_KEY`、`API_BASE` | 配置可用的模型服务凭据与接口地址 |
| `MODEL_PROVIDER`、`MODEL_NAME` | 与模型服务实际支持的提供方、模型标识一致；模板值只是起点 |
| `AUTH_PROVIDER` | 必须为 `password`；当前后端启动时强制检查 |
| `JWT_SECRET_KEY` | 稳定保存的随机密钥，至少 32 字符；空值或示例值会阻止后端启动 |
| `BOOTSTRAP_ADMIN_EMAIL`、`BOOTSTRAP_ADMIN_PASSWORD` | 首次创建管理员；密码至少 12 字符。只创建不存在的账号，不会提升或重置已有账号 |
| `MINIO_ACCESS_KEY`、`MINIO_SECRET_KEY` | 配置对象存储凭据，替换模板中的示例密码 |
| `FRONTEND_BIND_HOST`、`FRONTEND_PORT` | 默认 `0.0.0.0:10030`；仅本机访问可改为 `127.0.0.1:10030` |
| `SERVER_HOST` | 用户实际访问的应用地址，普通 Compose 部署默认 `http://localhost:10030` |

生成 JWT 随机密钥的方法：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

将结果填入私有 `.env`，不要粘贴到公开文档、Issue 或日志。重启和升级时保持该值稳定。

`deploy/datalab/compose.env.example` 提供部署身份模板，默认值与根目录一致。需要独立管理此类变量时，可以创建自己的私有环境文件并通过 Compose 的 `--env-file` 传入；后端服务仍读取根目录 `.env`，两处同名变量应保持一致。

### 常用可选配置

| 配置 | 说明 |
| --- | --- |
| `FILE_STORAGE_PROVIDER=hybrid` | 新文件写入 MinIO，同时兼容读取旧 GridFS 对象；旧 `gridfs` 值按该混合行为处理 |
| `SANDBOX_MAX_CONCURRENT` | 分析容器并发容量；模板为 `3`，根据机器资源调整 |
| `DATASET_HOST_PATH_ALLOWLIST` | 兼容历史宿主数据源时校验真实路径的根目录；不向浏览器公开真实路径 |
| `OBJECT_MOUNT_IMAGE` | 可覆盖配置中的挂载辅助镜像；默认镜像及摘要见 `backend/app/core/config.py` |
| `OBJECT_MOUNT_BACKEND_URL` | 挂载辅助容器访问后端的内部地址，默认 `http://backend:8000` |
| `OBJECT_MOUNT_HOST_ROOT` | Docker 主机上的私有挂载根目录，默认由后端配置；它是挂载位置，不是全文件缓存 |
| `CREDENTIAL_ENCRYPTION_KEY` | 工具凭据库的独立 Fernet 密钥；缺失或无效时凭据创建和使用不可用，普通分析不因此禁用 |
| `EXECUTION_SNAPSHOT_IDENTITY_KEY` | 推荐配置独立稳定密钥，用于执行环境的隐私标识 |
| `SPILL_ENABLED`、`SPILL_*` | 大型工具输出的私有持久存储、预览与分块读取参数 |
| `VISION_MODEL_*` | 可选独立视觉模型，用于图片和栅格检查 |
| `MODEL_REQUEST_CAPABILITIES` | 为明确的提供方与模型标识声明已核实能力，不依据模型名推断支持范围 |
| `OPEN_DATA_HUGGINGFACE_PROXY` | 可选 Hugging Face 官方 API 与文件主机代理，凭据仅放私有配置 |
| `EMAIL_*` | 按需配置 SMTP；当前注册采用管理员审核，不能把邮件验证视为自动激活 |
| `JUPYTER_WORKSPACES_*` | Notebook 工作区配置；默认保留任务独立的 Docker 工作卷，宿主归档模式需按代码要求预建匹配的父卷 |

完整参数以 [配置模板](../.env.example) 和 [后端配置定义](../backend/app/core/config.py) 为准。Compose 中明确设置的服务环境变量会覆盖 `.env` 中相同字段，例如内部数据库、网络和对象存储地址。

## 4. 首次启动与验收

`run.sh` 是带有项目保护逻辑的 Compose 入口；必须提供相应子命令。首次部署：

```bash
./run.sh config --quiet
./run.sh up -d --build
./run.sh ps -a
```

默认访问 [http://localhost:10030](http://localhost:10030)。使用初始管理员账号登录，其他用户提交注册申请后，由管理员审核通过再登录。

检查静态页面、前后端协议和认证配置：

```bash
curl --fail --silent --show-error http://localhost:10030/ -o /dev/null
curl --fail --silent --show-error http://localhost:10030/api/v1/config/client-contract
curl --fail --silent --show-error http://localhost:10030/api/v1/auth/status
./run.sh logs --tail=100 backend frontend
```

`client-contract` 应返回包含 `schema_version` 和 `supported_client_protocols` 的 JSON，`auth/status` 应报告 `password`。这些接口只能确认基础服务与路由；完整验收还需要：

1. 登录并确认账户、个人数据和历史任务可正常读取。
2. 上传一个小型 CSV，保存到个人数据后再次选择，确认复用原文件引用。
3. 新建分析并仅选择该文件，确认输入以只读方式可访问，计算文件写入独立工作区。
4. 完成一次分析，预览并下载返回的结果文件。
5. 需要 Notebook 时打开任务 Jupyter，验证同样的只读输入边界。

当前 Compose 没有为所有服务定义统一健康状态。使用容器运行状态、日志、HTTP 检查及上述业务验证共同判断部署结果，不要只依赖 `docker compose ps`。

## 5. 存储与备份

### 数据流与挂载规则

下载数据、用户上传和交付结果的持久来源是对象存储。个人数据库和任务输入保存带归属的文件引用，选择文件或“保存到我的数据”不复制底层对象。

对象读取通过后端按授权范围提供的网关进行，使用有界缓冲区读取所需字节范围。挂载辅助容器只获得针对已选文件的授权，不获得整个 bucket 视图；分析容器只获得只读文件挂载，不获得 bucket key、存储凭据或宿主挂载路径。

挂载失败时应修复挂载服务并重试，不能下载全量输入到会话目录、引入共享输入缓存或通过复制兜底。采集阶段可以保留可恢复传输的临时分片，但对象提交成功后应清理；计算阶段可创建临时工作文件。两者都不能充当持久来源副本或分析输入缓存。

### 持久数据范围

| 卷 | 保存内容 |
| --- | --- |
| `datalab-mongodb-data`、Mongo 配置卷 | 元数据、用户和数据库状态 |
| `datalab-minio-data` | 持久文件对象 |
| Redis 数据卷 | Redis 持久状态，已有部署可能保留历史卷名 |
| `datalab-dataset-data` | 兼容已有登记数据相关内容 |
| `datalab-skills-data` | 用户技能 |
| `datalab-open-data-downloads` | 采集传输暂存 |
| Jupyter 父卷及任务工作卷 | Notebook 和用户计算工作区 |

备份时应同时保留 MongoDB 元数据、MinIO 对象、用户技能、Notebook 工作区，以及加密保存的私有配置与密钥。按照各存储服务支持的方式制作一致备份并演练恢复；仅复制正在写入的数据卷目录不能证明备份一致性。

不要执行 `down -v` 或删除数据卷来升级、排障或释放空间。升级前记录实际卷身份；改变 `STACK_PREFIX` 或卷名会令应用指向另一套存储，并不是数据迁移。

## 6. 更新现有普通 Compose 部署

变更通过 [开发指南中的检查](development.md#4-测试与验收) 后，只更新受影响服务。以下适用于尚未启用第 7 节生产发布保护的普通部署。

只修改前端时：

```bash
docker system df
df -h
./run.sh build frontend
./run.sh up -d --no-deps frontend
```

前后端同时修改时：

```bash
./run.sh build frontend backend
./run.sh up -d --no-deps frontend backend
```

未变更的 sandbox、Jupyter 镜像应复用；只有其代码、运行时依赖或镜像构建定义确实变化时才重建。分析运行环境更新还应验证新任务采用的镜像与后端配置一致，并考虑正在运行任务的生命周期。

每次更新后执行第 4 节检查，并重点验证变更影响的用户操作。只改变 Markdown 文档通常无需重建应用镜像。

## 7. 已启用发布快照的生产部署

仓库包含 `deploy/production/prepare.py`、`deploy.py` 和 `sandbox_archive.py`。发布元数据默认位于私有 `.local/production`，可用 `DATALAB_DEPLOY_ROOT` 指定已配置的部署根目录。发布快照保存私有配置、明确版本的镜像身份和存储身份；不要提交这些内容。

当存在活动发布快照时，`run.sh` 会限制普通 Compose 修改入口；应使用生产命令，不能绕过保护直接重建同一栈。

| 命令 | 行为 |
| --- | --- |
| `./run.sh production` | 预检当前选中发布，不执行发布 |
| `./run.sh production --build --services frontend backend` | 在源码工作区构建指定应用镜像，不重启服务 |
| `./run.sh production --apply --services frontend` | 预检后发布选中快照的前端镜像 |
| `./run.sh production --apply --services frontend backend` | 发布选中快照的两个应用服务 |
| `./run.sh production --apply --stack` | 按快照启动固定全栈，应仅用于确有全栈需要的场景 |

构建和发布是分离步骤：新镜像必须经过 `prepare.py` 建立与选择快照后，才会成为生产发布对象。`prepare.py --help` 列出 `--name`、`--activate`、`--from-current` 及固定镜像身份参数；首次准备会检查已有 DataLab 服务及实际挂载卷，不是空机安装命令。准备快照本身不重启服务。

生产保护程序当前校验固定的端口 `10030` 和 `SERVER_HOST=http://localhost:10030`，以及镜像清单、网络和持久卷身份。此入口面向该受控部署约定，不是任意域名配置器。普通 Compose 的主机和端口配置仍可通过 `.env` 调整。

生产发布会使用部署锁并核对本地镜像身份；不要在它运行时从其他终端并行修改同一栈。需停止或归档动态沙箱时，遵循辅助程序现有流程，保留计算工作区与数据卷。

## 8. 可选服务

`office` profile 用于可选的 ONLYOFFICE 本地只读预览，采用独立浏览器来源、内部网络和网关。部署前阅读 [Office 预览说明](onlyoffice-local-viewer.md)，使用其秘密配置准备流程；默认普通栈不要求开启该 profile。

`database-readers` profile 中的 `sqlserver-reader-probe` 是隔离运行的实验性就绪探测，不是已接入的通用数据库预览服务。状态和边界见 [SQL Server 实验运行时](sqlserver-experimental-runtime.md)。

## 9. 常见问题

| 现象 | 排查方向 |
| --- | --- |
| 后端启动即退出 | 检查 JWT 密钥长度、`AUTH_PROVIDER=password`、管理员引导字段是否完整、数据库是否就绪 |
| 页面可开但 API 返回 502 | 查看 backend 日志和 frontend 代理配置，确认应用网络内后端监听 `8000` |
| 注册后无法登录 | 检查管理员是否审核、账号是否启用；邮件验证不代替审核 |
| 分析提示只读对象挂载不可用 | 检查 Docker 主机 FUSE、挂载传播、辅助镜像、内部网关和所选文件授权；不能改为复制输入 |
| 任务等待执行资源 | 查看运行中的分析容器与 `SANDBOX_MAX_CONCURRENT`；容量等待与模型请求失败是不同问题 |
| 模型返回认证或限流错误 | 检查私有模型配置、服务端额度和网络；不要公开完整凭据或请求日志 |
| 更新后浏览器仍显示旧界面 | 核对正在运行的 frontend 镜像及 `/release.json`，再检查代理缓存 |
| 普通 Compose 修改被拒绝 | 检查是否有活动生产发布快照，改用 `./run.sh production` 流程 |

排障输出可能含路径、用户内容或临时授权。分享前只保留必要的错误类型、状态和已脱敏片段。
