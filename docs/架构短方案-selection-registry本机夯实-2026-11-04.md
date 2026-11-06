# 架构短方案 · selection-registry 本机夯实（2026-11-04）

> 产出角色：架构师侧 · 主对话已确认并派开发 · **本文件只方案不写业务代码**（开发执行中）  
> CLAIM：`selection-registry-local-harden-20261104`（已派发·开发执行中）  
> 对齐：`P-2026-11-04-28`；需求卡 `D:\software\投广客户端\小g工作区\需求卡-共享登记服务本机夯实-2026-11-04.md`  
> HANDOFF：`D:\software\投广客户端\小g工作区\HANDOFF-服务端本机夯实-2026-11-04.md`  
> 优先级：**P2**（不挡千川真实视频闭环 / 发布主线）

---

## 1. 背景与闸门边界

### 1.1 背景（一句话）

工程内已有 `server/selection-registry`（Spring Boot 3.5 + MySQL 8.4 + Flyway + Docker Compose），A-104 曾在隔离环境 `127.0.0.1:8094`（MySQL `3307`）冒烟通过；现行客户端执行口径为 **单电脑本机登记**（`mode: local`），共享服务**保留未部署**。用户闸门确认：可先夯实服务端本机与文档，为后续二十台共享铺路，但**不**恢复客户端共享对接。

### 1.2 闸门（P-2026-11-04-28 / CLAIM 写死）

| 必须做 | 禁止做 |
|---|---|
| 本机/隔离 Docker Compose 一键起 MySQL + 登记服务 | 恢复客户端共享登记 UI / 服务地址 / 密钥 / 启动前联网门槛 |
| Flyway 迁移随启动可证 | 改 `selection_registry.ts` / `selection_registry_local.py` 本机登记语义 |
| health / status / 预留冲突 / 释放 / UNCERTAIN 冒烟可复现（等价 A-104） | 甲方主机 / 公网 / 生产部署；申请或等待甲方主机凭证作为本卡前置 |
| README / 部署 / 历史导入文档对齐「客户端本机登记 + 服务端保留未部署」 | 局域网明文暴露登记端口；使用生产库 / 甲方数据 / 百应·抖音账号冒烟 |
| Bearer 未认证拒绝可观测 | 改去重规则或状态机契约；打包客户端；push；挡千川/发布主线 |

**本卡完成后仍不自动恢复客户端对接**——共享回切需另开闸门与另卡。

### 1.3 客户端现状（只定位，不改）

| 路径 | 角色 |
|---|---|
| `desktop\src\main\selection_registry.ts` | Electron 主进程适配器；硬编码 `mode: 'local'`，经引擎调用本机登记 |
| `engine\selection_registry_local.py` | 本机 SQLite 原子预留/结算（`selection_local_registry`） |
| `engine\selection_registry_export.py` | 启用共享前导出本机商品 ID；**本卡只文档化，不强制跑导入** |

---

## 2. 现状摸底

### 2.1 目录与技术栈

```
server/selection-registry/
  compose.yaml          # MySQL 8.4 + registry；应用端口 127.0.0.1:8094；MySQL 不发布宿主机端口
  Dockerfile            # multi-stage：maven:3.9 + temurin-21 → JRE jar
  pom.xml               # Spring Boot 3.5.16 / Java 21 / JDBC / Validation / Actuator / Flyway MySQL
  .env.example          # DB_PASSWORD / MYSQL_ROOT_PASSWORD / REGISTRY_API_TOKEN
  README.md             # 部署/导入/API；口径部分过时（仍写「每台客户端填写 HTTPS」）
  src/main/java/.../RegistryAuthFilter.java
  src/main/java/.../RegistryController.java   # /health /status /snapshot /reservations* /pending /import
  src/main/java/.../SelectionRegistryApplication.java
  src/main/resources/application.yml          # port 8094；Flyway enabled
  src/main/resources/db/migration/V1__registry_tables.sql
  target/                 # 已有历史 jar（曾 package 成功）
  （无 src/test；无仓库内 smoke.ps1）
```

### 2.2 启动 / 迁移 / 冒烟现状 vs 缺口

| 项 | 现状 | 缺口（相对 README / HANDOFF / 需求卡） |
|---|---|---|
| 构建 | `mvn -B -DskipTests package` 历史通过；Dockerfile 同命令 | 本卡需再跑一遍作验收证据 |
| Compose | 已绑定 `127.0.0.1:8094`；MySQL 仅 Compose 内网；命名卷 `registry-mysql` | 缺**隔离数据目录约定**（D 盘临时/隔离，勿与生产 `mysql8` 冲突）；缺一键 local override 说明 |
| 迁移 | Flyway `V1__registry_tables.sql` 随应用启动 | 缺「up 后如何确认 flyway_schema_history」的可复制步骤 |
| 冒烟 | A-104 手工 PowerShell 曾覆盖 health/status/未认证/预留冲突/释放/UNCERTAIN；残留目录 `D:\software\codex\selection-registry-local-20261028-a104`（勿当生产） | **仓库内无可复跑脚本**；证据目录结构未模板化 |
| 文档 | README 部署/API/导入齐全 | 「当前部署状态」与「客户端填写 HTTPS」**与现行本机登记口径冲突**；未写清「本卡后仍不对接客户端」 |
| 测试代码 | 无 `src/test` | 非本卡必须；见路径对比 |

---

## 3. 实施路径对比

| 路径 | 内容摘要 | 改动量 | 风险 | 复杂度/可维护性 | 闸门合规 |
|---|---|---|---|---|---|
| **A · 推荐：最小文档 + Compose 隔离约定 + 可复制冒烟脚本** | ① README/LOCAL-SMOKE 口径对齐；② `compose.local.yaml` 或文档约定隔离卷/数据目录与回环端口；③ `scripts/smoke-local.ps1`（或证据目录内可复制步骤）覆盖 A-104 等价路径；④ `mvn package` + compose up/down 证据 | 小（文档 + 1～2 个辅助文件；**不改** Java 业务逻辑） | 低；端口冲突可换隔离目录规避 | 低复杂度；可维护：脚本可复跑 | ✅ 完全在闸门内 |
| **B · A + 轻量自动化加固** | 在 A 基础上增加：Compose healthcheck 已有可保留；可选 Actuator 探测说明；可选 `src/test` 或 CI job 仅 `package`+脚本冒烟 | 中 | 中（CI 环境需 Docker，易拖长本卡） | 可维护性更好，但对本机夯实收益边际 | ⚠ 仍合规，但超出「先夯实可复现」最小必要，建议另卡或本卡尾声可选 |
| **C · 否决：恢复客户端共享对接 / 甲方部署** | 恢复 UI、服务地址、密钥、联网门槛；或申请甲方主机凭证并生产部署 | 大 | 高；直接违反 P-28 | — | ❌ **否决**（越界） |

**推荐：路径 A。**  
理由：需求卡 F1–F4 与 CLAIM 验收均可被「Compose 可起 + 冒烟可复现 + 文档口径正确」闭合；路径 B 的 CI/单测不挡后续共享，但会抬高本卡工期且非闸门必需。路径 C 明确禁止。

---

## 4. 推荐方案实施步骤（开发卡执行序）

顺序固定：**Docker Compose → 迁移确认 → API 冒烟 → 文档对齐**。全程不改客户端业务源码。

### 4.1 Docker Compose（隔离 + 回环）

1. 约定本卡冒烟隔离根目录（示例，实机可微调，须在 D 盘临时/隔离，**禁止**生产库）：  
   `D:\software\投广客户端\小g工作区\selection-registry-local-harden-20261104\runtime\`  
   （日志、`.env` 副本、冒烟输出落此；Docker 命名卷或 bind-mount 亦指向其下子目录）。
2. 自 `.env.example` 生成隔离 `.env`（三密码互异高强度随机；**勿提交**；`.gitignore` 已忽略 `.env`）。
3. 端口约定（与 A-104 / README 对齐，仅回环）：  
   - 登记服务：`127.0.0.1:8094`（`compose.yaml` 已具备）  
   - MySQL：**默认不发布宿主机端口**（当前 compose 已如此，优先保持）。若需宿主机排障，仅用 override 绑 `127.0.0.1:3307:3306`，且确认本机 `3307` 空闲、不碰原有 `mysql8`。  
4. 建议新增（二选一，开发择轻）：  
   - `server/selection-registry/compose.local.yaml`：可选 bind-mount 数据目录 + 注释说明；或  
   - 仅在 `LOCAL-SMOKE.md` / README「本机夯实」节写清 `docker compose up -d --build` / `down -v`（是否 `-v` 须在证据中声明：本卡隔离卷可删）。  
5. 验收命令：`docker compose up -d --build` → 容器 healthy → `down` 后 `8094`（及若映射的 `3307`）无监听。

### 4.2 迁移

1. 应用启动即跑 Flyway（`application.yml`：`spring.flyway.enabled: true`）。  
2. 冒烟前确认：`V1__registry_tables.sql` 已创建 `selection_reservation` / `account_category_history`。  
3. 证据：容器日志含 Flyway success，或只读查询 `flyway_schema_history`（若开了 3307 排障口）。  
4. **禁止**手改生产/非隔离库；**禁止**新增业务迁移（本卡不改契约表语义）。

### 4.3 API 冒烟（等价 A-104）

建议落盘脚本：`server/selection-registry/scripts/smoke-local.ps1`  
（或证据目录 `...\selection-registry-local-harden-20261104\smoke-local.ps1`，仓库内复制一份便于复跑。）

最低覆盖（Bearer = `.env` 中 `REGISTRY_API_TOKEN`）：

| # | 步骤 | 期望 |
|---|---|---|
| 1 | `GET /api/v1/registry/health` | `ok: true` |
| 2 | `GET /api/v1/registry/status` + Bearer | 200，库连通 |
| 3 | 同上无 Bearer / 错 token | 401（或未配置时的 503，应在配置正确后验 401） |
| 4 | 两账号并发 `POST .../reservations` 争同一 `productId` | 恰一成功一冲突 |
| 5 | 明确未加车 `finish`=`NOT_ADDED`（释放）后可重试预留 | 可再次预留成功 |
| 6 | `finish`=`UNKNOWN` → `UNCERTAIN`；`GET .../pending` 可见；锁保持 | 符合契约 |
| 7 | （可选）`finish`=`ADDED` → `COMMITTED` | 符合契约 |

证据目录结构建议：

```
D:\software\投广客户端\小g工作区\selection-registry-local-harden-20261104\
  CLAIM.md
  REPORT.md / HANDOFF.md          # 开发交付
  evidence\
    mvn-package.txt
    compose-ps.txt / compose-logs.txt
    smoke-health.json
    smoke-status.json
    smoke-unauth.txt
    smoke-reserve-conflict.json
    smoke-release-retry.json
    smoke-uncertain-pending.json
    ports-after-down.txt
```

### 4.4 文档对齐

改动聚焦（**不**暗示「现在就去客户端填地址」）：

1. **`server/selection-registry/README.md`**  
   - 文首或「当前执行口径」专节：客户端走本机登记（`selection_registry.ts` → `selection_registry_local.py`，`mode: local`）；本服务**保留未部署 / 未对接**。  
   - 将「在每台客户端填写 HTTPS…」整段改为 **「启用二十台共享前」** 条件清单：汇总导入各机历史商品 ID、HTTPS/域名/访问密钥（凭证闸门）、私有网络部署；并写明「本卡完成后仍不自动恢复客户端对接」。  
   - 「当前部署状态」改为：源码/Compose/迁移具备；本机夯实冒烟见 CLAIM 证据；**未**生产部署、**未**改客户端登记路径。  
   - 增加「本机隔离冒烟」短节：指向 `scripts/smoke-local.ps1` 或 `LOCAL-SMOKE.md`、回环端口、隔离目录、`down` 后无监听。  
2. **可选** `server/selection-registry/LOCAL-SMOKE.md`：可复制 PowerShell 全文（若不想 README 过长）。  
3. **交叉引用（只文档）**：`docs/TODO.md` 本机夯实条目在关卡时勾选并链到证据（开发/主对话收口，非本架构文件职责）。  
4. **不改** SRS/RTM/客户端文案（除非主对话另批）；导出工具仅文档提及 `engine/selection_registry_export.py`。

---

## 5. 影响面

| 层 | 影响 | 说明 |
|---|---|---|
| Electron 主进程 | **无代码改动** | `selection_registry.ts` 保持 `mode: 'local'`；仅 README 交叉引用 |
| Python 引擎 | **无代码改动** | `selection_registry_local.py` 语义不变；`selection_registry_export.py` 本卡不强制执行 |
| 客户端 SQLite | **无改动** | 本机登记表结构不动 |
| server MySQL / Spring | **运行时本机隔离实例** | Compose 拉起的隔离库 + 既有 Flyway V1；**不改** Controller/Filter 业务契约为宜；若冒烟发现缺陷另开缺陷卡，不在本卡扩 scope |
| 文档 | **有改动** | README ± LOCAL-SMOKE ± 冒烟脚本说明 |

---

## 6. 风险与回滚

| 风险 | 缓解 | 回滚 |
|---|---|---|
| README 继续暗示「客户端立刻填服务地址」→ 口径回归 | 路径 A 强制改「启用共享前」措辞；评审对照 P-28 | `git checkout` 文档文件 |
| 隔离 Compose 端口与本机已有服务冲突（8094/3307） | 启动前 `netstat`/Get-NetTCPConnection；冲突则换隔离端口并记入证据 | `docker compose down`；不碰 `mysql8` |
| 误连非隔离库 / 误 `-v` 删错卷 | 数据目录与项目名写死在 CLAIM 证据根；禁止生产连接串 | 仅删本卡 runtime 卷/目录 |
| 冒烟脚本写入真实商品语义被误用到生产 | 脚本只用假 `productId`/`accountKey`；文档声明 | 删隔离库数据 |
| 范围蠕变（恢复共享 UI / 甲方部署 / 改 local 语义） | CLAIM 禁区；本方案路径 C 否决；开发拒绝越界需求 | 不合并越界 diff |
| 旧 A-104 残留目录干扰 | 只读参考；本卡使用新 runtime 目录 | 不删除旧目录除非用户明示 |

---

## 7. 验收点（可勾选）

- [ ] `mvn -B -DskipTests package`（在 `server/selection-registry`）通过，日志入证据。  
- [ ] 隔离 Compose `up -d --build` 成功；`127.0.0.1:8094` 可访问；MySQL 未对局域网明文开放。  
- [ ] Flyway V1 生效可证（日志或 schema_history）。  
- [ ] `/health` 返回 ok；已认证 `/status` 成功；未认证/错 token 拒绝可观测。  
- [ ] 预留冲突（同商品两账号恰一成功）、释放后可重试、`UNCERTAIN`+`pending` 可观测——等价 A-104。  
- [ ] `compose down` 后回环端口无残留监听（证据可查）。  
- [ ] README（及可选 LOCAL-SMOKE）口径与「客户端本机登记 + 服务端保留未部署」一致；启用共享前门槛写明；**写明本卡后不自动恢复客户端对接**。  
- [ ] **无**客户端业务源码 diff；**无**甲方/生产部署动作；交付 REPORT/HANDOFF + 证据目录。

---

## 8. 交付物清单与建议路径

| 交付物 | 建议路径 |
|---|---|
| 本架构短方案 | `D:\抖音电商\投广管理客户端\docs\架构短方案-selection-registry本机夯实-2026-11-04.md`（本文） |
| 开发 REPORT / HANDOFF | `D:\software\投广客户端\小g工作区\selection-registry-local-harden-20261104\REPORT.md`、`HANDOFF.md` |
| 冒烟证据 | `...\selection-registry-local-harden-20261104\evidence\`（见 §4.3） |
| 仓库内辅助（路径 A） | `server/selection-registry/README.md`（必改）；可选 `LOCAL-SMOKE.md`、`scripts/smoke-local.ps1`、`compose.local.yaml` |
| CLAIM | 已有；开发认领后改状态；**待派发时禁止开干** |

### 建议开发卡验收日志路径

`D:\software\投广客户端\小g工作区\selection-registry-local-harden-20261104\evidence\`

---

## 9. 材料矛盾 / 注意点（供主对话）

1. **README vs 现行口径**：README 仍写客户端「二十台电脑共享选品登记」填 HTTPS——与 A-104「单电脑本机登记」及 P-28 **直接冲突**；本卡文档对齐为最高优先级之一。  
2. **README「客户端配置/调用已加入」**：历史属实，但现行已切 `mode: local`；应改为「共享适配曾具备，当前未启用」。  
3. **A-104 用宿主机 MySQL `3307` vs 现行 compose 不发布 MySQL 端口**：推荐保持「MySQL 不发布」；仅排障时用 override 回环 `3307`。两种都合规，开发选一种写进证据即可。  
4. **旧残留** `D:\software\codex\selection-registry-local-20261028-a104` 仍在：只读参考，勿当本卡 runtime，勿当生产。  
5. **无闸门冲突的需求侧矛盾**：HANDOFF / 需求卡 / CLAIM / P-28 / TODO 条目口径一致；本方案与之对齐。

---

## 10. 本次架构做了什么 / 没做什么

- **做了**：只读摸底服务目录与客户端入口；对照 P-28/CLAIM/需求卡/README/TODO；落入本文短方案（路径对比 + 推荐 A + 步骤/验收/回滚）。  
- **没做**：未改任何客户端/服务端业务源码；未 `compose up`；未派开发；未申请甲方凭证；未恢复共享对接。

> 主对话确认本方案（或批注修订）后，再派开发认领 `selection-registry-local-harden-20261104`。