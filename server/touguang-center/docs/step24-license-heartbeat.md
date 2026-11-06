# Step 24：机器授权与心跳

## 范围

本步实现总管理员对已绑定机器授予天数、续期、停用和查询，以及服务端心跳判定。Step 23 的用户、角色、机器绑定和 12 小时登录会话继续使用。多级代理、时长池、授权树和级联失效属于 Step 28–30；客户端自动心跳、断网计时和界面锁定属于 Step 27。本步不改本地选品、下载、发布或千川登录。

只有管理员可授予、续期和停用；管理员和运营可查询。新增 `license:write`、`license:read` 权限随 V2 Flyway 迁移写入。客户端用现有 Bearer 令牌和 `X-Machine-Id` 心跳；令牌失效需重新登录，Step 24 不保存密码或新增长期登录凭据。

## 服务端规则

| 场景 | 判定 |
| --- | --- |
| 已绑定但从未授予 | `UNGRANTED`，`shouldLock=true` |
| 距到期超过 3 天 | `ACTIVE` |
| 距到期不超过 3 天且未到期 | `EXPIRING`，仍可用 |
| 管理员停用 | `DISABLED`，立即锁定；再次授予可重新启用 |
| 已到期 | `EXPIRED`，立即锁定 |
| 无有效心跳超过 3 分钟 | 客户端列表显示 `OFFLINE` |
| 无有效心跳超过 10 分钟 | 服务端拒绝该机器的受授权保护业务接口；下一次心跳重新判定 |

授权按服务端连续 24 小时计。首次授予从服务端当前时间开始；续期从当前到期时间与服务端当前时间中较晚者开始。停用保留历史到期时间；停用后重新授予从新的服务端时间开始。单次授予或续期为 1–3650 天。

`center_license.status` 保存管理员可改变的 `ACTIVE/DISABLED`；`EXPIRING/EXPIRED` 随时间变化，由接口实时计算，避免数据库缓存状态失准。剩余秒数和向上取整的展示天数同样由服务端计算。每次授权变更写入 `center_license_operation`；请求中的 `requestId` 是 UUID，重复提交不重复增加天数。经过鉴权的心跳写入 `center_heartbeat`，包含服务端接收时间、判定、剩余秒数和客户端自报时间。客户端时间偏差超过 5 分钟只记异常，不参与到期判定。

业务接口默认要求有效机器授权及最近 10 分钟内的心跳。Step 23 身份/管理接口及 Step 24 授权/心跳接口豁免，以便机器到期后仍可登录、查询和取得锁定指令。Step 25 新增任务接口会经过同一门禁，还需通过该步权限限制机器客户端身份。现有 machine_id 和令牌可被持有本机文件的人复制，不能声称硬件级防克隆。

## 接口

所有响应沿用 `{"code":0,"message":"成功","data":...}`。授权接口需要管理会话的 `Authorization: Bearer <token>`；心跳额外需要 `X-Machine-Id: <64位机器标识>`。

| 方法与路径 | 权限 | 请求体 |
| --- | --- | --- |
| `GET /api/clients/{id}/license` | `license:read` | 无 |
| `POST /api/clients/{id}/license/grant` | `license:write` | `{"requestId":"UUID","days":5}` |
| `POST /api/clients/{id}/license/renew` | `license:write` | `{"requestId":"UUID","days":5}` |
| `POST /api/clients/{id}/license/disable` | `license:write` | `{"requestId":"UUID"}` |
| `POST /api/clients/heartbeat` | `user:me` 且绑定当前机器 | `{"sequence":1,"clientTime":"2026-11-06T16:00:00+08:00","offlineSince":null}`；三个字段都可省略，发送 `{}` 即可 |

授权和心跳返回 `clientId`、`status`、`valid`、`shouldLock`、`grantedDaysTotal`、`remainingSeconds`、`remainingDays`、`expiresAt`、`serverTime`、`heartbeatIntervalSeconds=60`、`offlineGraceSeconds=600`。机器标识不匹配返回 HTTP 403；鉴权通过但授权无效的心跳返回 HTTP 200 与 `shouldLock=true`，使客户端能读到明确锁定结果。

## 断网规则与客户端边界

Step 27 客户端应从最后一次有效心跳起使用进程内单调计时，最多继续 600 秒，且不能超过该次响应的 `remainingSeconds`。修改 Windows 墙上时间不能延长这段时长。离线重启没有可信的进程计时，须先连接服务端；失败则限制使用。恢复后立即发心跳，可附 `offlineSince` 供审计；该值是客户端自报，不延长授权。管理员在客户端离线时停用授权，服务端立即拒绝新业务请求，但无法向断网机器即时推送，最多仍有本地 10 分钟宽限；纯软件也无法绝对阻止客户端代码被人为破解。

## 本机验证

新增 `spring-boot-starter-test` 仅供 JUnit/Mockito 校验边界和接口门禁，不进入运行包。需要本机已有 JDK 17、Maven、MySQL 8 二进制及 Docker Desktop。隔离脚本支持 Windows PowerShell 5.1 和 PowerShell 7；文件使用带 BOM 的 UTF-8，避免 5.1 将中文误读成语法字符。脚本使用全新 MySQL 数据目录和 Redis 7 容器，端口为 13318、16388、18092；不会访问已有业务库或复用其他 Redis 容器。

```powershell
Set-Location -LiteralPath 'D:\抖音电商\投广管理客户端-step24-20261106\server\touguang-center'
$env:JAVA_HOME = 'D:\jdk17'
$env:Path = "$env:JAVA_HOME\bin;D:\MAVEN\apache-maven-3.9.14\bin;$env:Path"
mvn -B test
mvn -B package
& '.\scripts\step24-smoke.ps1'
```

预期：Maven 报 `Tests run: 9, Failures: 0, Errors: 0`；脚本输出 `"Result":"PASS"`，并显示 `UNGRANTED/EXPIRING/DISABLED/EXPIRED`、错机器 HTTP 403、授权变更 2 条、心跳记录至少 4 条。脚本停止自己启动的服务、MySQL 和 Redis 容器；隔离日志留在输出的 `IsolatedRoot` 目录。真实 Electron 自动心跳、断网 10 分钟限制和到期界面锁定仍须在 Step 27 实机验收。
