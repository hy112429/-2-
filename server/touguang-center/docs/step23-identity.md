# Step 23：账号权限与客户端绑定

## 方案

- MySQL 8 存用户、角色、权限和机器绑定记录；表结构与初始三类角色在 `src/main/resources/db/migration/V1__identity_and_clients.sql`，由 Flyway 自动迁移。
- 密码使用 BCrypt（强度 12）哈希。登录签发 32 字节随机令牌，Redis 只存令牌 SHA-256 哈希与用户、机器标识，最长 12 小时。选服务端会话而非 JWT，是为了退出登录后立即撤销、角色变动后立即生效。
- 接口拦截器默认拒绝无权限标记的 `/api/**` 路由。仅健康检查、登录和机器登记公开；其余路由核对 Bearer 令牌和具体权限。未登录或权限不足返回 HTTP 403 和中文原因。
- 客户端首次启动生成机器信息加随机盐的稳定标识，存在 Electron `userData/center-machine-id.json`。服务器上登记为 `PENDING`；管理员绑定后为 `BOUND`。普通用户登录必须提供已绑定的机器标识，后续请求同时提供 `X-Machine-Id`。
- 首个管理员没有已绑定机器时，可作为管理端不带机器标识登录并执行绑定。该入口仅允许管理员；首次上线应限制管理接口仅内网可达，并使用 TLS。机器标识和请求头可被持有本机文件与令牌的人复制，**不是硬件级防克隆保证**；后续 Step 24 的授权、心跳和到期锁定需继续加固。

## 启动准备

在已配置 MySQL 8、Redis 7 和数据库环境变量的 Step 22 服务端基础上，首次启动时设置初始管理员。不要把密码写入脚本、仓库、截图或命令历史。下面示例通过交互输入密码，不回显：

```powershell
Set-Location 'D:\抖音电商\投广管理客户端\server\touguang-center'
$env:JAVA_HOME = 'D:\jdk17'
$env:Path = "$env:JAVA_HOME\bin;D:\MAVEN\apache-maven-3.9.14\bin;$env:Path"
$env:CENTER_BOOTSTRAP_ADMIN_USERNAME = 'center_admin'
$secret = Read-Host '输入至少 12 位的初始管理员密码' -AsSecureString
$env:CENTER_BOOTSTRAP_ADMIN_PASSWORD = [System.Net.NetworkCredential]::new('', $secret).Password
mvn -B -DskipTests package
java -jar 'target\touguang-center-0.1.0-SNAPSHOT.jar'
```

数据库、Redis 环境变量按 [README](../README.md) 配置。服务端第一次成功启动后立即从服务环境移除初始密码；该环境变量不会重置现有管理员。生产环境用秘密管理器注入，避免命令行中出现明文。`CENTER_SESSION_TTL_SECONDS` 默认 43200，可设 300–86400 秒。

## 接口与权限

| 方法与路径 | 权限 | 用途 |
| --- | --- | --- |
| `GET /api/health` | 公开 | 服务与依赖健康 |
| `POST /api/auth/login` | 公开 | 用户名、密码、可选机器标识；返回令牌 |
| `POST /api/auth/logout` | `user:me` | 撤销当前令牌 |
| `GET /api/users/me` | `user:me` | 当前用户与角色权限 |
| `POST /api/users` | `user:create`（管理员） | 创建账号并分配角色 |
| `GET /api/roles` | `role:read` | 三类角色 |
| `GET /api/permissions` | `permission:read` | 权限字典 |
| `POST /api/clients/register` | 公开 | 幂等登记机器，初始待授权 |
| `GET /api/clients` | `client:read` | 管理员/运营查看客户端列表 |
| `POST /api/clients/{id}/bind` | `client:bind`（管理员） | 绑定已有账号 |

`ADMIN` 有全部权限；`OPERATOR` 可查看角色、权限和客户端；`READ_ONLY` 只能查看自身、角色和权限。普通客户端会话每次请求需带 `Authorization: Bearer <token>` 与 `X-Machine-Id: <machineId>`；机器不再处于绑定状态或用户停用后，会话不能继续使用。初始管理员不带 `machineId` 登录得到管理会话，用于首次绑定。

## 接口自测（PowerShell）

以下假设服务在 `127.0.0.1:8080`，已经通过 Step 22 环境设置并创建管理员。请把示例用户名、机器标识换成本机实际值；密码通过交互输入。

```powershell
$base = 'http://127.0.0.1:8080'
$adminName = 'center_admin'
$secret = Read-Host '管理员密码' -AsSecureString
$adminPassword = [System.Net.NetworkCredential]::new('', $secret).Password
$loginBody = @{ username = $adminName; password = $adminPassword } | ConvertTo-Json
$login = Invoke-RestMethod -Method Post -Uri "$base/api/auth/login" -ContentType 'application/json' -Body $loginBody
$token = $login.data.token
$headers = @{ Authorization = "Bearer $token" }
$me = Invoke-RestMethod -Method Get -Uri "$base/api/users/me" -Headers $headers
$roles = Invoke-RestMethod -Method Get -Uri "$base/api/roles" -Headers $headers
$permissions = Invoke-RestMethod -Method Get -Uri "$base/api/permissions" -Headers $headers
$machineId = ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')).ToLowerInvariant()
$registration = Invoke-RestMethod -Method Post -Uri "$base/api/clients/register" -ContentType 'application/json' -Body (@{ machineId = $machineId; machineName = 'Step23-Test-PC' } | ConvertTo-Json)
$clients = Invoke-RestMethod -Method Get -Uri "$base/api/clients" -Headers $headers
$bindBody = @{ userId = $me.data.id } | ConvertTo-Json
$bound = Invoke-RestMethod -Method Post -Uri "$base/api/clients/$($registration.data.id)/bind" -Headers $headers -ContentType 'application/json' -Body $bindBody
$machineLoginBody = @{ username = $adminName; password = $adminPassword; machineId = $machineId } | ConvertTo-Json
$machineLogin = Invoke-RestMethod -Method Post -Uri "$base/api/auth/login" -ContentType 'application/json' -Body $machineLoginBody
$machineHeaders = @{ Authorization = "Bearer $($machineLogin.data.token)"; 'X-Machine-Id' = $machineId }
$machineMe = Invoke-RestMethod -Method Get -Uri "$base/api/users/me" -Headers $machineHeaders
$logout = Invoke-RestMethod -Method Post -Uri "$base/api/auth/logout" -Headers $headers
```

典型响应，实际 ID 和令牌会不同：

```json
{"code":0,"message":"成功","data":{"token":"<随机令牌>","expiresInSeconds":43200,"user":{"id":1,"username":"center_admin","displayName":"初始管理员","roles":["ADMIN"],"permissions":["client:bind","client:read","permission:read","role:read","user:create","user:me"]}}}
```

登记返回 `{"code":0,"message":"成功","data":{"id":1,"status":"PENDING"}}`；列表可见同一 `machineId`，绑定后返回 `status=BOUND` 和 `boundUserId`。未带令牌访问列表返回 HTTP 403：`{"code":40300,"message":"请先登录或重新登录"}`。只读角色访问列表也返回 HTTP 403。再次用同一 `machineId` 登记保持同一记录和绑定状态。

本地隔离一键联测：

```powershell
Set-Location 'D:\抖音电商\投广管理客户端\server\touguang-center'
& '.\scripts\step23-smoke.ps1'
```

该脚本使用独立 D 盘目录、MySQL 13317、Redis 16387、服务 18091，自动生成测试密码，验证健康、登录、机器绑定、权限 403、令牌撤销并停止它启动的进程。它依赖本机现有的 JDK 17、MySQL 8 和 Windows Redis 二进制；本机 Redis 为 5.0.14.1，**不能代替目标 Redis 7 的验收**。脚本不会修改已有业务库或客户服务器。

## 桌面客户端接入边界

仅设置 `TOUGUANG_CENTER_URL` 后，桌面客户端启动时才向 `/api/clients/register` 上报；例如 `http://127.0.0.1:8080`。没有设置时，本地选品、素材和发布流程保持原状。机器标识文件损坏时拒绝自动换号，需管理员核对原绑定。当前尚无管理界面和普通用户登录界面；可通过上述接口完成登记、列表和绑定。Step 24 才处理心跳、到期锁定与进一步授权校验；尚未部署客户服务器。
