# Step 23 本机验证记录 · 2026-11-06

- 服务端：`D:\jdk17` + `D:\MAVEN\apache-maven-3.9.14` 下 `mvn -B -DskipTests clean package` → `BUILD SUCCESS`（29 个 Java 源文件编译，未运行 JUnit）。
- 桌面主进程：`npm run typecheck:node` → PASS。
- 桌面渲染器：`npm run typecheck:web` → FAIL，既有 `ProductQueryView.vue:303` 的 `taskId` 类型错误；与本次 Step 23 主进程/服务端改动无关，本轮未改此模块。
- 隔离联测：`scripts/step23-smoke.ps1` → PASS。独立 MySQL 8.0.29 / Redis 5.0.14.1 / Spring Boot，端口 13317 / 16387 / 18091；测试健康 UP、未带令牌 403、未绑定普通用户登录 403、机器请求头缺失 403、只读用户访问管理接口 403、登记 PENDING、绑定 BOUND、同机器重复登记保持同 ID、退出后令牌 403。测试实例已停止，数据和日志留在 `D:\software\codex\step23-smoke-f883b5329ca447f5a1c4d2aef545845b` 供本机追查；没有接触已有业务库。
- Redis 7 隔离联测：Docker Desktop Linux 引擎中预有 `redis:7-alpine` 镜像；执行 `& '.\scripts\step23-smoke.ps1' -UseDockerRedis7` → PASS，实际 Redis 版本 7.4.11。专属 Redis 容器只绑定 `127.0.0.1:16387`，数据目录在 `D:\software\codex\step23-smoke-c94671ed9d414de4bb9c89d0539d087a\redis-data`；脚本结束已停止该容器，不影响原有 `wmremover-redis`。独立 MySQL 8 + Spring Boot 的 `/api/health` 两项依赖为 UP；无令牌 403、未绑定用户登录 403、机器头缺失 403、只读用户访问管理接口 403、登记 PENDING、绑定 BOUND、重复登记保持 ID、退出后令牌 403 均通过。该次测试目录留在 D 盘供追查，未连接已有业务库或客户服务器。
- 真实桌面端启动登记、管理员列表/绑定真实机器、机器会话与重启保持机器 ID、客户服务器部署/重启恢复、Step 24 心跳均未验收。未部署客户服务器，也未生成客户端安装包。
