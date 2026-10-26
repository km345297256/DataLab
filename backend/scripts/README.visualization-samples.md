# DataLab 可视化样例门禁

样例目录是测试用来源与格式元数据，不是恢复旧数据集管理功能。`app/resources/visualization-datasets`、`visualization-plugin-datasets` 和 `visualization-descriptors` 记录公开来源、许可证、文件 SHA256、派生链及生成器哈希；插件矩阵位于 `app/resources/visualization-plugin-test-matrix.json`。资源不包含本机源路径，也不把宿主目录暴露给浏览器。迁入不自动下载额外大文件、不注册数据集、不改用户插件偏好。

在 backend 目录执行静态与安全门禁（零模型、不连接数据库）：

```bash
uv run pytest tests/test_plugin_sample_safety.py tests/test_prepare_visualization_datasets.py \
  tests/test_visualization_dataset_catalog.py tests/test_visualization_http_catalog_fixtures.py \
  tests/test_visualization_plugin_samples.py tests/test_database_visualization_http_safety.py \
  tests/test_database_records_http_safety.py tests/test_physical_database_http_safety.py
```

`prepare_visualization_datasets.py --help` 提供严格样例准备入口。默认仅校验已审阅 staging 文件及目录元数据；显式 `--apply` 才将已校验文件复制到所指定的新目录并写来源说明。它不会联网下载或写 MongoDB。派生脚本在 `plugin_sample_reproduction/` 和 `plugin_samples_*.py`，按各 descriptor 的精确来源、SHA256 和参数执行；固定生成器哈希必须随正式来源变更一起审阅。

真实 HTTP 预览门禁使用 DataLab 当前用户已上传的文件。先在本地终端设置当前用户的 `DATALAB_BENCHMARK_TOKEN`，再提供私有绑定 JSON：

```json
{"CATALOG_DATASET_ID":{"ENTRY_FILENAME":"CURRENT_USER_FILE_ID"}}
```

```bash
python scripts/check_plugin_dataset_samples.py baseline \
  --base http://127.0.0.1:10030 --file-bindings /tmp/sample-bindings.json \
  --baseline /tmp/sample-baseline.json
python scripts/check_plugin_dataset_samples.py check \
  --base http://127.0.0.1:10030 --file-bindings /tmp/sample-bindings.json \
  --baseline /tmp/sample-baseline.json \
  --matrix app/resources/visualization-plugin-test-matrix.json --report /tmp/sample-report.json
```

只需要验证部分格式时，提供从正式矩阵提取的子矩阵及对应绑定。缺失文件不会自动上传或注册，禁用插件保持禁用。检查只调用当前用户的 `/files/{id}/info` 与统一可视化接口；版本由实际目录响应返回后固定，不能静态伪造。FastQC 只读取通过 `--completed-jobs-json` 明确提供的已完成任务，不启动或取消任务。

报告区分 `passed`、`disabled`、`not_run`、`failed`。静态目录校验通过不等于完整 84 项插件矩阵已经进行真实 HTTP/浏览器验证；完整跑测需准备所选来源文件和启用所需插件。静态门禁和离线 benchmark oracle 均不调用模型。
