# 轻量共享选品登记服务

面向 20 台客户端共用的商品 ID 与账号末级类目登记服务。采用 Spring Boot、MySQL 8.4 和 Docker Compose；不保存百应登录凭据，也不代理平台浏览器。

## 数据规则

- 商品 ID 在全部账号和电脑间全局唯一。状态为 `RESERVED`、`ATTEMPTING`、`COMMITTED`、`UNCERTAIN` 的记录都占用商品 ID。
- 同一抖音账号的末级类目只能登记一次。账号身份使用稳定的 `douyin_id`；客户端账号表本地自增 ID 不跨电脑复用。
- 跨账号类目不设唯一约束。快照返回类目使用次数，客户端把它作为错开偏好。
- 加车前先预留；确认加入后提交；确定未加入时释放。执行结果不明时进入 `UNCERTAIN` 并继续锁定。
- API 连接或预留失败时客户端不会发送百应加车命令。

## 部署

1. 在服务主机安装 Docker Engine 和 Compose 插件。
2. 复制 `.env.example` 为 `.env`，将三个密码替换为不同的高强度随机值。不要把 `.env` 加入版本控制。
3. 在私有网络中部署，并在服务主机配置 HTTPS 反向代理，将请求转发到 `127.0.0.1:8094`。Compose 只把应用端口绑定到本机回环地址，不在局域网开放明文 HTTP；客户端拒绝非 localhost 的 HTTP 地址。
4. 执行 `docker compose up -d --build`。
5. 检查 `https://<服务域名>/api/v1/registry/health` 返回 `{"ok":true,...}`。
6. 在每台客户端的「二十台电脑共享选品登记」中填写 HTTPS 服务地址与 `REGISTRY_API_TOKEN`，保存后点「检查连接」。密钥由 Windows 系统加密后保存在客户端用户数据目录。

服务端只在 Compose 私有网络开放 MySQL；不要把 3306 端口发布到局域网或公网。正式启用前应为数据库卷设置定期备份与恢复演练。

## 首次启用前导入旧商品

必须先合并 20 台电脑已有的商品 ID，再开放选品，避免新服务空库时重复选择旧商品。

在每台电脑分别以只读方式导出本地已记录商品 ID：

```powershell
python engine/selection_registry_export.py --db "D:\path\to\ad_client.db" --output "D:\selection-registry-import.json"
```

汇总去重全部导出文件后，向服务端 `POST /api/v1/registry/import` 发送 `{"items":[...]}`，请求头使用 `Authorization: Bearer <REGISTRY_API_TOKEN>`。单次最多 5000 项；较大的清单拆分多次导入。重复商品 ID 会被跳过。

旧 SQLite 仅保存商品 ID 与账号 ID，没有历史末级类目 ID，因此初始导入只能恢复商品全局去重；旧账号类目历史无法从当前表结构还原。启用后每次新加车都会从百应当前来源商品的完整三级类目 ID 建立历史。

## API

所有 `/api/v1/registry/*` 接口都要求 Bearer token；仅 `/health` 公开。

| 路径 | 用途 |
|---|---|
| `GET /api/v1/registry/health` | 连通与数据库健康检查 |
| `GET /api/v1/registry/status` | 使用 Bearer token 验证客户端凭据与数据库连接 |
| `POST /api/v1/registry/snapshot` | 只检查本轮候选商品 ID / 末级类目是否已登记，并返回指定账号历史类目与本轮候选类目用量；不会下载整张商品登记表 |
| `POST /api/v1/registry/reservations` | 按商品 ID + 账号末级类目原子预留；`requestId` 支持响应丢失后的幂等重试 |
| `POST /api/v1/registry/reservations/{token}/begin` | 标记即将调用百应加车 |
| `POST /api/v1/registry/reservations/{token}/finish` | `ADDED` / `NOT_ADDED` / `UNKNOWN` 结算 |
| `GET /api/v1/registry/pending` | 查看预留、执行中及结果不明记录 |
| `POST /api/v1/registry/reservations/{token}/resolve` | 管理员人工核对后，以 `manuallyConfirmed: true` 结算 `ADDED` 或 `NOT_ADDED` |
| `POST /api/v1/registry/import` | 首次导入既有商品 ID / 可用账号类目历史 |

`UNCERTAIN` 记录不会自动过期或释放。管理员应先按账号和商品 ID 在百应选品车核对，再调用 `/resolve`；如果不能确认结果，保持锁定。

## 当前部署状态

源码、迁移脚本、Compose 与客户端配置/调用已加入项目。服务主机、域名、HTTPS 证书和访问密钥由甲方网络环境提供；本次没有部署到任何主机，也没有导入客户数据库或启动百应加车。
