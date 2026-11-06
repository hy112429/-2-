# 投广管理客户端中心服务端（Step 22–23）

Spring Boot 3.5 / Java 17，使用 MySQL 8、Redis 7、MyBatis-Plus 与 Flyway。Step 22 提供健康检查；Step 23 增加账号、角色、权限与客户端登记/绑定接口。不承载视频或媒体处理。

## 目录

```text
src/main/java/com/touguang/center/
  common/       统一返回体、业务异常与全局异常处理
  config/       MyBatis-Plus、Redis 与接口权限拦截器
  controller/   HTTP 接口
  service/      健康检查、认证与客户端绑定逻辑
  repository/   MyBatis-Plus Mapper
  entity/       用户与客户端实体
  dto/          API 数据对象
src/main/resources/
  application.yml
  application-prod.yml
  db/migration/  Flyway 建表与初始角色权限
```

## 本机构建与启动

要求 JDK 17、Maven 3.6.3+、MySQL 8 与 Redis 7。先创建专用数据库与账号，再通过环境变量传入连接信息；不要把真实密码写入配置文件或版本库。

```powershell
$env:JAVA_HOME = 'D:\jdk17'
$env:Path = "$env:JAVA_HOME\bin;$env:Path"
$env:DB_HOST = '127.0.0.1'
$env:DB_PORT = '3306'
$env:DB_NAME = 'touguang_center'
$env:DB_USERNAME = 'touguang_app'
$env:DB_PASSWORD = '<本机秘密管理器中的数据库密码>'
$env:REDIS_HOST = '127.0.0.1'
$env:REDIS_PORT = '6379'
$env:REDIS_PASSWORD = '<本机秘密管理器中的 Redis 密码>'
mvn -B -DskipTests package
java -jar target/touguang-center-0.1.0-SNAPSHOT.jar
```

开发态若本机 Redis 未设密码，`REDIS_PASSWORD` 可留空。生产态建议使用 `SPRING_PROFILES_ACTIVE=prod` 并单独管理 `DB_URL`、`DB_USERNAME`、`DB_PASSWORD`、`REDIS_HOST`、`REDIS_PORT`、`REDIS_USERNAME`、`REDIS_PASSWORD`。`DB_MIGRATION_ENABLED` 默认为 `true`，应用启动时由 Flyway 校验/执行迁移。

## Compose 本机联调（可选）

`.env.example` 仅含占位值。复制为 `.env`，换成仅本机联调的强密码后再运行；`.env` 不应提交或发送给他人。容器端口只绑定 `127.0.0.1`。默认映射为 MySQL `13306`、Redis `16379`、服务 `8080`，避免占用常用本机数据库端口。

```powershell
Copy-Item .env.example .env
# 将 MYSQL_ROOT_PASSWORD、MYSQL_PASSWORD、REDIS_PASSWORD 改为不同的随机强密码
docker compose up -d --build
Invoke-WebRequest http://127.0.0.1:8080/api/health
```

Compose 会启动中心服务和隔离的本地 MySQL/Redis，仅绑定本机回环地址供联调；不会连接或部署到甲方环境。

## 接口

`GET /api/health` 返回 `{ "code", "message", "data" }`。`data` 包含服务状态、构建版本、MySQL 与 Redis 的 `UP`/`DOWN` 状态。两个依赖均正常时 HTTP 200、`code=0`；任一依赖不可用时 HTTP 503、非零 `code`，消息为中文。响应不会包含堆栈、连接串或密码；详细异常仅写服务端日志。

Step 23 的建表 SQL、初始化管理员、登录、角色权限、客户端登记与绑定、完整 PowerShell 调用及响应示例见 [账号与客户端绑定操作说明](docs/step23-identity.md)。首次启动时必须设置 `CENTER_BOOTSTRAP_ADMIN_USERNAME` 与 `CENTER_BOOTSTRAP_ADMIN_PASSWORD`（至少 12 位）；只在用户表为空时创建，不存在默认密码。登录会话使用 Redis 中的随机令牌哈希，退出可立即撤销。配置 `TOUGUANG_CENTER_URL` 后，桌面客户端每次启动会幂等上报本机机器标识；未配置时维持当前本机模式。

## 内网单机部署

客户现有 Windows Server 作为 Hyper-V 宿主、Linux 虚拟机承载本服务及 MySQL/Redis 的完整步骤见 [Windows Server + Linux 虚拟机部署指南](deploy/windows-hyperv-linux.md)。该指南包含只读主机检查、开机自启配置与物理机重启验收；客户服务器尚未实际部署。

1. 在专用内网主机安装受支持的 **JDK 17**、MySQL 8.x、Redis 7.x；校验 `java -version`、`mysql --version` 与 `redis-server --version`。
2. 创建专用服务账号与目录（Linux 示例）：
   ```sh
   sudo useradd --system --home /opt/touguang-center --shell /usr/sbin/nologin touguang
   sudo install -d -o touguang -g touguang -m 0750 /opt/touguang-center
   sudo install -o touguang -g touguang -m 0640 target/touguang-center-0.1.0-SNAPSHOT.jar /opt/touguang-center/app.jar
   ```
   Windows 可使用专用低权限服务账号，并限制 JAR 与配置文件 ACL。
3. MySQL 使用管理员账号创建专用库和应用账号（不要让应用使用 `root`；将示例主机、密码替换成实际值，并通过安全终端输入密码）：
   ```sql
   CREATE DATABASE touguang_center CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
   CREATE USER 'touguang_app'@'<应用服务器内网地址>' IDENTIFIED BY '<随机强密码>';
   GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP, REFERENCES
     ON touguang_center.* TO 'touguang_app'@'<应用服务器内网地址>';
   ```
   仅允许应用主机或受控内网网段访问；按组织策略开启 TLS，并定期轮换凭证。
4. Redis 设置强口令/ACL 用户，启用 `protected-mode`，`bind` 到 loopback 或受控内网地址；禁止公网监听。例如 `redis.conf` 至少配置：
   ```conf
   bind 127.0.0.1 <应用服务器内网地址>
   protected-mode yes
   port 6379
   requirepass <随机强密码>
   ```
5. 通过受限服务账号运行。Windows 可使用受管 Windows Service 包装器；Linux 可使用 `systemd`。将 `SPRING_PROFILES_ACTIVE=prod` 和连接凭证注入服务环境或秘密管理器，不写入 JAR、命令历史或日志。配置服务开机自启与失败重启策略。
6. 防火墙仅向获准的内网客户端开放应用端口（默认 8080）；MySQL 3306 与 Redis 6379 仅允许应用服务器访问，禁止公网监听或放行。
7. 从允许的内网客户端验证 `/api/health`，并在重启后确认服务自动拉起。生产环境应配置 TLS 终止、访问控制、日志轮转、备份与恢复演练。

## 安全与职责边界

- 改掉所有默认密码；不使用数据库 root 账号；凭证只通过环境变量/秘密管理器注入。
- MySQL 与 Redis 仅内网监听、最小网段放行；管理端口不得暴露公网。
- 健康检查只报告依赖状态，不回显密钥、连接串或异常堆栈。
- 按 SRS G.2，中心服务端只存元数据与缩略图、只做账号权限/任务调度/日志/授权校验；**不存视频文件、不做媒体处理**。
- Step 23 只实现身份与机器绑定基础，不修改客户端本机选品登记或 `selection-registry`，不部署甲方。心跳、到期锁定与中心任务调度属于后续步骤。
