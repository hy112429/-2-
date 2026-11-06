# Step 22 · 客户 Windows Server + Hyper-V + Linux 虚拟机部署

本文件面向一台现有 Windows Server：Windows 只承担 Hyper-V 宿主；同一台 Linux 虚拟机内运行 Spring Boot、MySQL 8 与 Redis 7。Windows 桌面客户端通过内网访问虚拟机的服务端口。中心服务端只存元数据与缩略图，不存视频文件、不处理媒体。

当前工程已完成 Maven 构建和本机健康检查冒烟；**客户服务器尚未部署，物理机重启后的自启尚未验收**。以下命令在客户服务器执行前，先由管理员确认维护窗口和网络规划。不要在开发电脑上执行安装命令。

## 1. Windows Server 主机条件检查（只读）

在客户服务器打开 PowerShell，运行本目录的 `windows-host-precheck.ps1`：

```powershell
& 'D:\software\codex\touguang-center\deploy\windows-host-precheck.ps1'
```

先把本目录的脚本复制到上述路径；也可以把命令中的路径替换成实际存放位置。若客户的 PowerShell 执行策略禁止脚本，由管理员按组织策略审核后运行。记录 Windows Server 版本、内存、D 盘剩余空间、Hyper-V 角色状态和 CPU 虚拟化信息。与管理员确认：客户现有业务能否安排一次重启；服务器是否已经运行别的虚拟化软件；能否提供一个可被桌面客户端访问的内网地址。检查结果不能单独证明虚拟机可用，管理员还需核对 BIOS/UEFI 虚拟化设置及现有负载。

若 Hyper-V 尚未安装，管理员在维护窗口内使用以下命令安装；**该命令可能重启服务器**：

```powershell
Install-WindowsFeature -Name Hyper-V -IncludeManagementTools -Restart
```

安装后用 `Get-WindowsFeature Hyper-V` 核对状态。Hyper-V 的硬件与 Windows Server 版本要求以[微软文档](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/get-started/install-hyper-v?pivots=windows-server&tabs=powershell)为准。

## 2. 创建 Linux 虚拟机

在 Hyper-V 管理器中创建一台 **第 2 代** Ubuntu Server 24.04 LTS 虚拟机。初始规划可用 **2 vCPU、8 GB 内存、80 GB 虚拟磁盘**，实际值由管理员按主机空闲资源和客户负载调整。虚拟机文件优先放在 `D:\software\codex\touguang-center-vm\`；如果客户服务器没有足够的 D 盘空间，由管理员指定其他数据盘。

网络使用管理员确认的内网虚拟交换机，为虚拟机配置固定 IP 或 DHCP 保留地址。若新建外部交换机，先评估它对宿主现有网卡连接的影响。安装 Ubuntu 时启用 OpenSSH Server；MySQL、Redis 与 Java 服务随后都装在这台虚拟机内。

设虚拟机名称为 `touguang-center`，在 Windows Server 管理员 PowerShell 中设置随宿主启动、宿主关机时正常关闭：

```powershell
Set-VM -Name 'touguang-center' -AutomaticStartAction Start -AutomaticStopAction ShutDown
Get-VM -Name 'touguang-center' | Select-Object Name, State, AutomaticStartAction, AutomaticStopAction
```

自动启动参数见[微软 Set-VM 文档](https://learn.microsoft.com/en-us/powershell/module/hyper-v/set-vm)。

## 3. Linux 虚拟机内安装运行环境

以下命令在 Ubuntu 虚拟机的终端运行。先安装系统包，再确认实际版本分别为 Java **17**、MySQL **8.x**、Redis **7.x**；版本不符时先处理软件源，不继续部署应用。

```bash
sudo apt update
sudo apt install -y openjdk-17-jre-headless mysql-server redis-server openssl curl
java -version
mysql --version
redis-server --version
sudo systemctl enable --now mysql redis-server
```

创建应用专用的低权限用户和目录：

```bash
sudo useradd --system --home /opt/touguang-center --shell /usr/sbin/nologin touguang
sudo install -d -o touguang -g touguang -m 0750 /opt/touguang-center
sudo install -d -o root -g touguang -m 0750 /etc/touguang-center
```

## 4. 建库、Redis 安全配置与应用环境文件

以下是**新建专用虚拟机的一次性初始化命令**。在同一个 Bash 会话完整执行；密码由 `openssl` 生成，不显示在屏幕上，也不写入仓库。它会创建 `touguang_center` 库及本地专用账号，限制 Redis 仅监听 `127.0.0.1`，并生成 `/etc/touguang-center/center.env`。

```bash
db_password=$(openssl rand -hex 24)
redis_password=$(openssl rand -hex 24)

sudo mysql <<SQL
CREATE DATABASE IF NOT EXISTS touguang_center CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS 'touguang_app'@'127.0.0.1' IDENTIFIED BY '${db_password}';
ALTER USER 'touguang_app'@'127.0.0.1' IDENTIFIED BY '${db_password}';
CREATE USER IF NOT EXISTS 'touguang_app'@'localhost' IDENTIFIED BY '${db_password}';
ALTER USER 'touguang_app'@'localhost' IDENTIFIED BY '${db_password}';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP, REFERENCES
  ON touguang_center.* TO 'touguang_app'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP, REFERENCES
  ON touguang_center.* TO 'touguang_app'@'localhost';
SQL

sudo sed -i -E '/^[[:space:]]*(bind|protected-mode|requirepass)[[:space:]]/d' /etc/redis/redis.conf
printf '\nbind 127.0.0.1\nprotected-mode yes\nrequirepass %s\n' "$redis_password" | sudo tee -a /etc/redis/redis.conf >/dev/null
sudo systemctl restart redis-server

cat <<ENV | sudo tee /etc/touguang-center/center.env >/dev/null
SPRING_PROFILES_ACTIVE=prod
SERVER_BIND_ADDRESS=0.0.0.0
SERVER_PORT=8080
DB_URL="jdbc:mysql://127.0.0.1:3306/touguang_center?sslMode=DISABLED&serverTimezone=Asia/Shanghai"
DB_USERNAME=touguang_app
DB_PASSWORD=${db_password}
DB_MIGRATION_ENABLED=true
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_PASSWORD=${redis_password}
ENV
sudo chown root:root /etc/touguang-center/center.env
sudo chmod 0600 /etc/touguang-center/center.env
unset db_password redis_password
```

数据库连接的 `sslMode=DISABLED` **只适用于应用与 MySQL 同在此虚拟机、并且 MySQL 仅监听回环地址**的部署。部署前检查端口监听；如果 3306 或 6379 监听了外部地址，先更正 MySQL/Redis 配置，不开放数据库端口：

```bash
sudo ss -ltnp | grep -E ':(3306|6379)[[:space:]]'
```

MySQL 的 Ubuntu 配置通常在 `/etc/mysql/mysql.conf.d/mysqld.cnf`，其中 `bind-address` 应为 `127.0.0.1`。Redis 已由上面的命令设置 `bind 127.0.0.1` 和密码。正式环境文件只保存在 Linux 虚拟机内，不发到聊天、截图或版本库。

## 5. 构建并安装中心服务

在开发电脑的工程目录 `D:\抖音电商\投广管理客户端\server\touguang-center` 构建：

```powershell
Set-Location -LiteralPath 'D:\抖音电商\投广管理客户端\server\touguang-center'
mvn -B -DskipTests package
```

把 `target\touguang-center-0.1.0-SNAPSHOT.jar` 和本目录的 `touguang-center.service` 用客户批准的 SFTP 工具上传到虚拟机 `/tmp/`。随后在虚拟机执行：

```bash
sudo install -o touguang -g touguang -m 0640 /tmp/touguang-center-0.1.0-SNAPSHOT.jar /opt/touguang-center/app.jar
sudo install -o root -g root -m 0644 /tmp/touguang-center.service /etc/systemd/system/touguang-center.service
sudo systemctl daemon-reload
sudo systemctl enable --now touguang-center
sudo systemctl status touguang-center --no-pager
curl -sS -i http://127.0.0.1:8080/api/health
```

预期 HTTP 200，响应中 `code=0`，`data.status=UP`，版本号存在，MySQL 和 Redis 均为 `UP`。若启动失败，查 `sudo journalctl -u touguang-center -n 100 --no-pager`；不要把含密钥的环境文件贴到日志或聊天中。

## 6. 内网访问与防火墙

只向获准的客户端内网网段开放虚拟机的 TCP **8080**。保留管理员使用的 SSH 访问策略；MySQL 3306、Redis 6379 仅监听虚拟机回环地址，不对内网客户端开放。若使用 Ubuntu 的 UFW，管理员把下面的示例网段换成客户实际网段后执行，并确认不会切断当前 SSH：

```bash
sudo ufw allow from 192.168.10.0/24 to any port 8080 proto tcp
sudo ufw status verbose
```

若 UFW 尚未启用，由管理员先补齐 SSH 允许规则，再决定是否启用；Windows 宿主和内网边界防火墙也按客户策略放行虚拟机的 8080。桌面客户端访问 `http://<虚拟机内网IP>:8080/api/health`；正式使用前按客户网络规范提供 HTTPS/TLS 终止与访问控制。

## 7. 重启验收

1. 虚拟机内先检查 `systemctl is-enabled mysql redis-server touguang-center` 均为 `enabled`；服务 `status` 均为 `active`。
2. 在维护窗口由客户管理员重启 **Windows Server 宿主**，核对 Hyper-V 中 `touguang-center` 自动进入 `Running`。
3. 在虚拟机内复查三个服务与 `/api/health`；再从一台获准 Windows 客户端访问同一接口。结果应为 HTTP 200、版本号、MySQL UP、Redis UP。
4. 将客户实际服务器版本、虚拟机 IP、服务状态和重启验收时间记入交付记录；不记录密码、Token 或连接密钥。

服务端依旧遵守 SRS 附录 G.2：只存元数据和缩略图；账号权限、任务调度、日志和授权校验在后续步骤实现。Step 22 不存视频、不做媒体处理。
