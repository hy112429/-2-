# Step 23 本机验证记录 · 2026-11-06

- 服务端：`D:\jdk17` + `D:\MAVEN\apache-maven-3.9.14` 下 `mvn -B -DskipTests clean package` → `BUILD SUCCESS`（29 个 Java 源文件编译，未运行 JUnit）。
- 桌面主进程：`npm run typecheck:node` → PASS。
- 桌面渲染器：`npm run typecheck:web` → FAIL，既有 `ProductQueryView.vue:303` 的 `taskId` 类型错误；与本次 Step 23 主进程/服务端改动无关，本轮未改此模块。
- 隔离联测：`scripts/step23-smoke.ps1` → PASS。独立 MySQL 8.0.29 / Redis 5.0.14.1 / Spring Boot，端口 13317 / 16387 / 18091；测试健康 UP、未带令牌 403、未绑定普通用户登录 403、机器请求头缺失 403、只读用户访问管理接口 403、登记 PENDING、绑定 BOUND、同机器重复登记保持同 ID、退出后令牌 403。测试实例已停止，数据和日志留在 `D:\software\codex\step23-smoke-f883b5329ca447f5a1c4d2aef545845b` 供本机追查；没有接触已有业务库。
- 本机仅有 Redis 5；Redis 7 目标环境、桌面端实际启动登记、客户服务器重启恢复和 Step 24 心跳均未验收。未部署客户服务器，也未生成客户端安装包。
