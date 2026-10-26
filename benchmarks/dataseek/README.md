# DataLab 离线评测与预实验

本框架从 18 份仓库公开样例生成 36 道结构与分析题，冻结输入大小、SHA256 和隐藏答案，严格检查真正交付的 `answer.json`。沿用 `benchmarks.dataseek` 模块路径和 `dataseek_default` 方法 ID，便于读取上游 V2 记录；运行目标、鉴权和文件输入已适配 DataLab。

`oracles`、`prepare`、测试和 `report` 不调用模型。只有 `run` 会使用部署中的模型并产生费用。迁入和离线自检不代表完成真实模型质量评测，也不继承上游预实验成绩。开发样例不是盲测集。

## 零模型离线门禁

仓库根执行：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s benchmarks/dataseek/tests -p 'test_*.py'
```

宿主缺少 numpy、Pillow、xlrd、netCDF4、astropy、GDAL 或 pypdf 时，完整来源校验会跳过。发布门禁应使用已有科学沙盒镜像，确保所有来源校验实际执行：

```bash
docker run --rm --network none --read-only --tmpfs /tmp \
  -e PYTHONDONTWRITEBYTECODE=1 -e DATALAB_BENCHMARK_DATASETS=/datasets \
  -v "$PWD/benchmarks:/verification/benchmarks:ro" \
  -v "$PWD/backend/app/resources/datasets:/datasets:ro" \
  -w /verification --entrypoint /app/.venv/bin/python datalab-sandbox:latest \
  -m unittest discover -s benchmarks/dataseek/tests -p 'test_*.py'
```

该命令只读挂载仓库公开样例，不连接数据库、不挂用户卷、不访问网络，也不调用模型。测试覆盖独立来源计算、真实字节变异、严格评分、隐藏答案隔离、SSE 中断取消、当前用户文件绑定及临时资源清理。

## 冻结输入与答案

```bash
mkdir -p benchmarks/dataseek/private
chmod 700 benchmarks/dataseek/private
docker run --rm --network none --read-only --tmpfs /tmp \
  -v "$PWD/backend/app/resources/datasets:/datasets:ro" \
  -v "$PWD/benchmarks/dataseek/oracles.py:/oracle.py:ro" \
  --entrypoint /app/.venv/bin/python datalab-sandbox:latest \
  /oracle.py /datasets > benchmarks/dataseek/private/gold.json

python3 -m benchmarks.dataseek prepare \
  --gold benchmarks/dataseek/private/gold.json \
  --experiment-dir benchmarks/dataseek/runs/pilot-new \
  --base-url http://127.0.0.1:10030 --backbone YOUR_DEPLOYED_MODEL \
  --methods dataseek_default,generic_react --repeats 1 --wall-seconds 300 --snapshot
```

`private/`、`runs/` 已忽略提交；不要将 gold、实验目录或完整评测仓库挂入模型沙盒。可用 `--task-ids` 限定题目；省略表示准备全部 36 题，并不执行它们。默认检查 DataLab 的 `datalab-frontend-1`、`datalab-backend-1` 和实际映射端口。

## 当前用户文件绑定

通过 DataLab 上传本次题目需要的原始文件，记录当前用户拥有的 `file_id`。将绑定保存在 `private/file-bindings.json`，例如：

```json
{"open-uci-iris":{"iris.csv":"CURRENT_USER_FILE_ID"}}
```

外层 `dataset_id` 仅是离线题库的来源标识，不要求数据集注册。绑定值只接受文件 ID，拒绝宿主路径。不得上传 `gold.json`、SOURCE 说明或隐藏答案。对于 shapefile 等多文件格式，选择题目 `input_files` 列出的全部伴随文件，并保留各自文件名。

在当前终端设置 `DATALAB_BENCHMARK_TOKEN` 为当前登录用户的访问令牌；令牌只在内存和请求头中使用，不写入配置、报告、模型输入或 URL。`run` 通过 `/auth/me` 确认用户，再逐个通过该用户权限检查文件信息及下载字节，复验冻结 SHA256。没有令牌、匿名用户、缺失/错配文件、自动启用技能或运行环境不一致都会停止。

## 真实模型跑测与评分

只有需要真实质量测量并已授权模型费用时才执行：

```bash
python3 -m benchmarks.dataseek run \
  --experiment-dir benchmarks/dataseek/runs/pilot-new \
  --file-bindings benchmarks/dataseek/private/file-bindings.json --limit 1

python3 -m benchmarks.dataseek report \
  --experiment-dir benchmarks/dataseek/runs/pilot-new
```

默认 Agent 以 `attachments + input_file_ids` 走 DataLab 现有会话鉴权、输入快照和文件同步链；不调用旧 `/datasets` 管理接口。通用 ReAct 在现有后端内按当前用户从 FileStorage 再次授权下载并验哈希，把这些字节写入随机命名的专属临时卷，再只读挂给新沙盒。它不会挂现有数据卷或宿主文件目录；只清理已确认属于本次执行的容器和输入卷，清理不确定时停止批次。

`--limit` 限制新启动次数，已有 run_id 即使失败也不会自动重放。API 模式通过墙钟截止取消其专属会话，断流不重复提交。通用 ReAct 有估算 Token 准入和物理调用数限制；API 模式没有相同的每请求准入，因此两组资源控制不同。取消、成果下载和清理可能延长总耗时。未知 Token 或费用保留为空，trace 不是完整账单。

通用 ReAct 仅暴露 `shell_run`、`file_read`，不主动加入产品规划、科学目录、交付修复；但镜像中的项目辅助库仍可由 Shell 访问。这不是“同工具 ReAct”基准。评分只采信下载文件，拒绝 NaN/Infinity、重复 JSON 键、以布尔值伪装数值等；仍未包含独立代码重跑和主观图表评分。报告同时保留已观测分母与完整计划分母，不应据此作正式方法优劣结论。
