# 轻量共享选品登记服务

服务端 API 使用 Spring Boot、MySQL 8.4 和 Docker Compose，提供商品 ID 与账号末级类目的预留、结算、查询和历史导入能力。它不保存百应登录凭据，也不代理平台浏览器。

## 当前执行口径

客户端未配置 `TOUGUANG_REGISTRY_URL` 与 `TOUGUANG_REGISTRY_TOKEN` 时仍走本机 SQLite（`mode: local`）。两项同时配置后，主进程通过 HTTPS 访问本服务（`mode: server`）；服务不可达或响应不完整时停止本轮并保留检查点，不回退到本机结果。凭据仅由主进程环境提供，不写入仓库或选品日志。**当前服务尚未生产部署，历史记录尚未导入，甲方客户端尚未切换。**

## 数据规则

- 服务端商品 ID 在全部账号和电脑间全局唯一。状态为 `RESERVED`、`ATTEMPTING`、`COMMITTED`、`UNCERTAIN` 的记录都占用商品 ID。
- 同一抖音账号的末级类目只能登记一次。账号身份使用稳定的 `douyin_id`；客户端账号表本地自增 ID 不跨电脑复用。
- 客户端账号缺少可用 `douyin_id` 时，以机器 ID 加本地账号 ID 作为账号键；商品 ID 仍全局互斥，但该账号的末级类目历史不能跨电脑合并，启用前应补齐账号身份。
- 跨账号类目不设唯一约束。快照返回类目使用次数，调用方可把它作为错开偏好。
- API 流程为加车前预留；确认加入后提交；确定未加入时释放；执行结果不明时进入 `UNCERTAIN` 并继续锁定。
- `UNCERTAIN` 记录不会自动过期或释放。人工核对后才可通过 `/resolve` 结算。

## 启用二十台共享前

只有在另行批准启用共享后，才进入以下准备与部署工作：

1. 汇总各台电脑的历史商品 ID，完成去重并导入共享服务，避免空库造成重复选择。可先只读导出：

   ```powershell
   python engine/selection_registry_export.py --db "D:\path\to\ad_client.db" --output "D:\selection-registry-import.json"
   ```

   导出会合并已确认商品、加车配额和本机登记；`COMMITTED` 与 `UNCERTAIN` 分别导入，执行中或预留未结算的记录按 `UNCERTAIN` 保守锁定，`RELEASED` 不导入。已有末级类目 ID 时一并导入；缺少时只能恢复商品全局去重。各电脑都须导入，且须先核对同一 ID 的冲突状态。导出文件包含商品和账号标识，存放在受控目录，不提交 Git。

2. 确认 HTTPS、服务域名与访问密钥的签发和保管方式。只在部署和历史导入完成后，为客户端运行环境一次性配置 `TOUGUANG_REGISTRY_URL`（服务根地址）和 `TOUGUANG_REGISTRY_TOKEN`；普通选品无需每次设置。仅本机隔离可使用 `http://127.0.0.1`。
3. 在私有网络中部署服务，配置 HTTPS 反向代理、数据库备份与恢复演练。MySQL 不应直接发布到局域网或公网。
4. 先核对服务端已导入历史、客户端稳定账号身份和待核查记录，再启用客户端共享模式。启用后新候选入池前查询服务器；恢复旧池也剔除已占用 ID，未达目标保留缺额并继续补池。加车前仍由服务端原子预留，避免快照后其他电脑抢先选中。

## 本机隔离冒烟

可复跑脚本：[`scripts/smoke-local.ps1`](scripts/smoke-local.ps1)。本卡运行数据、`.env` 副本和日志放在 CLAIM 的 `runtime/` 下；服务只绑定 `127.0.0.1:8094`，MySQL 默认不发布宿主机端口。脚本只使用随机后缀的 `smoke-p28-*` 假商品 ID 和账号键。

在 PowerShell 中从本目录执行（先在隔离 runtime 目录生成 `.env`，不要使用生产凭据）：

```powershell
$runtime = 'D:\software\投广客户端\小g工作区\selection-registry-local-harden-20261104\runtime'
$evidence = 'D:\software\投广客户端\小g工作区\selection-registry-local-harden-20261104\evidence'
docker compose -p selection-registry-local-harden-20261104 --env-file "$runtime\.env" up -d --build
& .\scripts\smoke-local.ps1 -EnvFile "$runtime\.env" -EvidenceDirectory $evidence
docker compose -p selection-registry-local-harden-20261104 --env-file "$runtime\.env" down -v
```

唯一的 Compose 项目名把命名卷隔离在本卡范围内；本卡结束使用 `down -v` 删除的只是该项目卷。完成关闭后，`127.0.0.1:8094` 应无监听。需要时可用独立 override 将 MySQL 仅绑定到 `127.0.0.1:3307` 排障；默认不发布 MySQL 端口。

## API

所有 `/api/v1/registry/*` 接口都要求 Bearer token；仅 `/health` 公开。

| 路径 | 用途 |
|---|---|
| `GET /api/v1/registry/health` | 连通与数据库健康检查 |
| `GET /api/v1/registry/status` | 使用 Bearer token 验证凭据与数据库连接 |
| `POST /api/v1/registry/snapshot` | 检查本轮候选商品 ID / 末级类目是否已登记，并返回账号历史类目与候选类目用量 |
| `POST /api/v1/registry/reservations` | 按商品 ID + 账号末级类目原子预留；`requestId` 支持响应丢失后的幂等重试 |
| `POST /api/v1/registry/reservations/{token}/begin` | 标记即将调用百应加车 |
| `POST /api/v1/registry/reservations/{token}/finish` | `ADDED` / `NOT_ADDED` / `UNKNOWN` 结算 |
| `GET /api/v1/registry/pending` | 查看预留、执行中及结果不明记录 |
| `POST /api/v1/registry/reservations/{token}/resolve` | 管理员人工核对后，以 `manuallyConfirmed: true` 结算 `ADDED` 或 `NOT_ADDED` |
| `POST /api/v1/registry/import` | 启用共享前导入既有商品 ID / 可用账号类目历史；每项可标注 `COMMITTED` 或 `UNCERTAIN` |

`UNCERTAIN` 记录不会自动过期或释放。管理员应先按账号和商品 ID 在百应选品车核对，再调用 `/resolve`；不能确认结果时保持锁定。

## 当前部署状态

服务端源码、Compose 和 Flyway 迁移具备；本机隔离冒烟的历史执行记录见 `D:\software\投广客户端\小g工作区\selection-registry-local-harden-20261104\evidence\`。客户端共享适配已写入源码，但本轮未运行服务端或客户端验证；**未**生产部署、导入历史或切换甲方运行环境。
