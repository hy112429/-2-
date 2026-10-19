# 投广管理客户端 —— 项目状态 · 待办与已知风险

> 最后更新：2026-10-14
> **本文档是项目唯一的进度与上下文来源。新会话请先读「零、新会话接续指南」。**
> 完成一项就在这里打勾并注明日期，不要另开文档。

---

## 零、新会话接续指南 ★ 先读这个

### 0.1 项目一句话定位

抖音电商选品素材工具：**把「人工下载视频 → 人工去水印 → 人工转码」改成一条自动化流水线**。
形态是 Windows 桌面客户端（Electron），处理引擎用 Python 伴生进程，本地 SQLite 记账。

### 0.2 关键路径（都在这里，别再来回找）

```powershell
# 项目根
$PROJ = "D:\抖音电商\投广管理客户端"

# Python 引擎
$ENG  = "$PROJ\engine"
$PY   = "$ENG\.venv\Scripts\python.exe"

# Electron 客户端
$APP  = "$PROJ\desktop"

# 文档
$DOC  = "$PROJ\docs"

# 会话开场提示词（**每个新会话先调用**）：$DOC\提示词手册.md（开场 Prompt + 二期复用约定）

# 数据目录（D 盘，两个用途分开）
$DATA = "D:\software\投广客户端"
```

### 0.3 启动与验证命令

**Python 引擎**（首次或换机器时）：

```powershell
$PROJ = "D:\抖音电商\投广管理客户端"
$ENG  = "$PROJ\engine"
$PY   = "$ENG\.venv\Scripts\python.exe"
python -m venv "$ENG\.venv"
& $PY -m pip install -r "$ENG\requirements.txt"
```

**Electron 客户端**：

```powershell
$APP = "D:\抖音电商\投广管理客户端\desktop"
Set-Location $APP
npm run dev          # 启动（Ctrl+C 停止）
npm run typecheck    # 类型检查
npm run build        # 构建到 out/
```

**解析一条抖音链接**（会真的联网）：

```powershell
& $PY "$ENG\douyin_parse.py" "https://v.douyin.com/xxxxx/" --verify
```

### 0.4 已确认决策清单（**不要重复询问用户**）

以下都是用户明确答复过的，新会话直接采用，不要再问一遍：

| # | 决策项 | 结论 |
|---|---|---|
| 1 | 项目结构 | `ad-client/` 下分 `engine/`（Python）+ `desktop/`（Electron）+ `docs/` |
| 2 | 来源平台 | **抖音 / 快手** |
| 3 | 输入方式 | **单条链接粘贴** |
| 4 | 输出要求 | **原分辨率 + 保留音轨**（MP4 / H.264 / AAC 128k） |
| 5 | 水印策略 | **关键帧锚点 + 自动跟踪**（但实测发现「换源」就够了，见 §8.2） |
| 6 | Python 运行 | **系统 Python 3.13 + venv**，打包时再内置便携 Python |
| 7 | 数据根目录 | `D:\software\投广客户端`，分 **downloads**（视频）和 **data**（数据库）两层 |
| 8 | Electron 位置 | `ad-client\desktop`（与 engine 平级） |
| 9 | 序号格式 | 补零 **2 位**（`01`、`02`…，避免 10 排在 2 前面） |
| 10 | 时间戳格式 | **`yyyyMMddHHmmss`**（不是 epoch 秒） |
| 11 | 日期目录格式 | **`YYYY-MM-DD`** |
| 12 | 分辨率上限 | **按方向**：竖屏 1080x1920、横屏 1920x1080（见 §8.4） |
| 13 | 重试退避 | **1s → 2s → 5s**（用户指定，非严格指数） |
| 14 | 查重依据 | **`video_id` 全局唯一** |
| 15 | 「账号」含义 | 抖音发布账号 / 矩阵号（表字段留通用，当店铺也能装） |
| 16 | 登录目标 | 抖音主站 + 巨量百应（**百应 URL 待用户确认**，见 T-15） |
| 17 | 登录态给 Python | **stdin 传 JSON，不落盘** |
| 18 | WAF 绕过 | **不做**（明确不做对抗性反爬绕过） |
| 19 | UI 语言 | **全中文** |
| 20 | UI 组件库 | 暂不引入（Step 4 零依赖手写 CSS，见 T-12） |

**用户偏好（贯穿全程）**：

- 用专业商业框架语言，避免过于通俗的解释
- **分步骤模块化**，每次只做指定的这一步，不要顺手改别的文件
- 每步先给 3–5 行方案说明，再给完整代码
- 命令要给**可直接复制的 PowerShell 完整形式**
- 引入新依赖前先说明**为什么需要它**
- **不确定的地方先问**，不要自己假设
- 每步结束要说明**如何验证成功**
- 存储尽量放 **D 盘**
- 项目信息与进度落 **md 文件**

### 0.5 当前状态与阻塞

| 项目 | 状态 |
|---|---|
| Step 1 `douyin_parse.py` | ✅ 完成（重试逻辑的成功路径**待复测**，见 T-03） |
| Step 2 `download.py` | ✅ 完成，端到端实测通过 |
| Step 3 `db.py` | ✅ 完成（自测 22/23 项未跑，见 T-06） |
| Step 4 `desktop/` Electron 空壳 | ✅ 完成，窗口实测 1280x800 |
| Step 5 登录态管理 | ✅ 完成（登录后加密取证 + 百应 URL 待确认） |

**⚠️ 当前状态（2026-10-12 更新）**：WAF 硬封锁已解除（10-08 触发，4 天衰减；10-12 实测 `html_length` 38000+ 正常页、无挑战、一次成功）。真实解析+下载链路已打通（2 条抽纸视频成功落盘）。**可以正常对抖音发请求**，但仍需克制（见 T-01/T-14）。

**🔑 百应接入现状（2026-10-12，L2 抓包已验证）**：
- 入口：`https://buyin.jinritemai.com/dashboard?universal_page_params_id=a95eb4c6-75c7-4108-8334-12f7527a451c`（用户提供，已配入 LOGIN_TARGETS）
- 登录态已建立（jinritemai.com 域 cookie，douyin-login 分区）
- 选品路径：选品 → 选品中心 → 选品广场（商品卡片网格，product_id 19 位数字藏于详情 URL）
- **商品→视频数据源**：商品详情接口 `pack_detail` 响应含 `media.video_url`（真实 mp4 直链）——已实测下载 7MB 可播放（瑞幸咖啡 16.8s）。接口带 `a_bogus`/`msToken` 签名，引擎无法直接构造（合规不逆向）
- **实现方案（已确认，待实现）**：「探针方案」——登录视图挂 `probe.ts`（已挂载于 login.ts ensureView），用户打开商品详情时探针解析 `pack_detail` 响应，缓存 `product_id → {标题/封面/佣金/video_url}` 到 SQLite（新表 `baiying_products`），商品查询用真实数据、下载队列直接用 video_url
- 探针日志：`%TEMP%\baiying_probe.jsonl`（含已抓到的 3 个商品 video_url，**URL 带时效签名会过期**）
- ⚠️ **mock 源陷阱**：`product_query.py` 的 mock 视频 URL 指向 `127.0.0.1:8000`（验收用临时 HTTP 服务，**已停**）——UI 点「下载」会失败，属预期现象。待 T-15 接入 `baiying_products` 缓存后替换为真实 video_url

**👤 账号管理现状（Step 11，2026-10-13）**：
- 数据层：`db.py` 增量迁移（accounts 补 8 列 + 新增 `account_groups` / `account_group_members` / `qianchuan_accounts` / `account_qianchuan` 四表 + `videos.published`，schema v2）；表与索引拆分执行，修复了历史 tasks 表缺 `video_row_id` 导致 init 中断的问题
- 引擎：`account_manager.py`（账号/分组/千川 CRUD + 统计 + 自动分组）已入白名单，CLI 全链路自测通过（3 账号自动归组 美妆/食品，千川 1:N 绑定 2 账号）
- 登录态隔离：`account_login.ts`——每抖音账号分区 `persist:acct-{id}`、每千川账户分区 `persist:qc-{id}`，切换即换分区不串号；canvas 指纹注入（一账号一指纹，FR-G7-01）；safeStorage 分账号加密备份至 `D:\software\投广客户端\data\login_credential_{acct|qc}_{id}.enc`；导出走 stdin 不落盘
- UI：`AccountManagementView.vue` 挂「账号管理」导航（全局统计卡 + 每账号统计列 + 批量控制 + 分组 + 千川账户管理），桌面实测渲染正常
- ✅ **自动验证已过**：未登录查登录态正确报告「未登录」；分区目录 `Partitions/acct-1、acct-2` 独立创建；未登录不生成 `.enc`；拉取信息探针链路跑通（假号 10001 正确报「未抓到用户数据」）
- ✅ **三平台登录入口已打通**：抖音（扫码）✓ / 百应抖店（`fxg.jinritemai.com/login/common?from=buyin`，手机号+验证码，或切扫码）✓ / 千川（`qianchuan.jinritemai.com` → `business.oceanengine.com/login?appKey=80`，手机号/邮箱验证码）✓
- ✅ **targetDomains 已修复**：账号分区登录态判定/导出域从仅 douyin.com 扩展为 抖音+百应（douyin.com+jinritemai.com+douyinec.com），否则登录百应后查登录态仍报未登录
- ⏳ 待用户真实扫码登录验证：不串号、`.enc` 备份生成、拉取信息抓真实昵称/粉丝/作品数（需把某账号抖音号改为真实值）
- ✅ **下载文件路径提示（2026-10-13）**：`download_queue.py` 成功/跳过结果与 task 事件回传 `file_path`，商品查询下载队列行显示「📁 打开所在文件夹 + 完整路径」，解决用户找不到下载文件问题
- ✅ **动态水印去除初版（2026-10-13~14）**：`remove_watermark.py`（OpenCV 逐帧：CLAHE 增强 → 文字 mask → 预扫描采样+中值平滑轨迹 → 逐帧插值定位 → inpaint 修复）已入白名单；**实测结论（9 轮诊断）**：①`TM_CCORR_NORMED` 在 CLAHE 增强画面上处处 0.96 高分，轨迹全错；②`TM_CCOEFF_NORMED` 对正确手工模板可定位（偏差 38px），但**自动捕获的模板质量不可靠**（半透明文字梯度弱、扩大后易包入内容/贴纸），自动定位反复失败；③仅手工裁剪正确模板时 18s/6s 帧可去除。**结论：纯 OpenCV 对半透明移动水印的自动定位不可靠，可靠方案需「半自动标注轨迹」或 AI 分割（超当前范围）**。当前 UI 框选入口可用但效果视模板质量
- ✅ **滚动修复已完成（2026-10-13）**：根因 = `.acct-view` 是 flex 滚动容器，子元素默认 `flex-shrink:1` 把内容压缩进视口导致永不溢出、无滚动条；已给子元素统一加 `flex-shrink:0` / `flex:none`（stats/toolbar/batch-bar/table-wrap/group-grid），内容超高时自然溢出触发 `overflow-y:auto`。实测：注入 30 占位块后 `scrollHeight 3284 > clientHeight 880`，滚动条出现、`scrollTop` 滚动生效；另补充「滚动时重报浏览器视图 bounds」（onScroll→syncBounds）避免 WebContentsView 覆盖层错位

- ⛔ **去水印功能（已搁置 2026-10-14，用户裁定；2026-10-14 再裁定：GPU 方案跳过，维持搁置）**：半透明移动水印（抖音号昵称）去除经多轮实现未达可用。技术结论存档：
  ①纯 OpenCV 自动定位不可靠（9 轮诊断：CCORR 在增强画面处处高分、CCOEFF 对自动捕获模板定位偏差大）；
  ②半自动链路已通（`frame://` 协议加载抽帧 JPEG + 点击/「自动找水印」红点 + 引擎 `--points` 轨迹修复），
    但用户肉眼难辨半透明水印、引擎自动检测结果偏差大，最终处理效果仍失败。
  ③结论：可靠去除需 AI 水印检测/分割模型（重工程量）或走商业源（源头无水印，百应/罗盘直链已验证）。
  ④2026-10-14 候选开源方案评估：ProPainter（ICCV2023 视频修复 SOTA，质量达标但 **需 GPU**）→ 用户裁定**跳过**；
    multi-delogo（FFmpeg delogo 扩展，无需 GPU 但为**空间插值**，对半透明移动文字留残影、原理不适配）→ 不采用。
  功能**维持搁置**，最终路线锁定**商业源无水印直链**（百应/罗盘源头无水印已实测；抖音分享链接走 playwm→play 换源）。
- 🔶 **罗盘榜单情报模块（2026-10-14 启动，参考「抖川工具」插件功能评估；当前主任务。2026-10-14 用户收缩范围：仅交付「罗盘登录跳转入口」，探针/缓存/UI 暂缓）**：独立增量模块（新缓存表 + 罗盘探针 + 商品查询「榜单」tab，不改现有百应探针/缓存/下载链路）。**开发步骤路线图（按序执行，每步含验证方式）**：
  - ✅ **① 罗盘登录入口（2026-10-14 完成）**：`LOGIN_TARGETS` 新增「罗盘」目标（`compass.jinritemai.com`，domains=jinritemai.com+douyin.com），并为每个登录目标增加 `partition` 字段——罗盘用**独立分区 `persist:compass-login`**（罗盘 cookie 为 jinritemai.com 域，若不隔离会串入百应登录态判定/导出造成串号），其余目标保持共用 `persist:douyin-login`；login.ts 改为**按目标分区**：切换分区自动销毁重建 WebContentsView（分区创建时定死），`checkLoginState`/`exportForEngine`/登出清理/凭证加密备份均按目标各自分区取 cookie；`ensureLoginView()` 固定绑百应目标，百应/头条批量链路不受罗盘切换影响。登录面板下拉自动出现「罗盘」，点击即在内置浏览器打开罗盘页面，扫码/跳转登录后即可浏览。typecheck 通过。**用户实测验收通过（2026-10-14）**。**②~⑥ 暂缓**（用户裁定：只需跳转接口，后续需要时再启用探针捕获/缓存/UI）
  - **② 探针捕获（核心，需用户配合）**：`probe.ts`（挂载于 login.ts ensureView）扩展监听罗盘榜单接口——**视频榜 / 商品榜 / 达人榜 / 直播榜**四类。做法同百应探针：接口 URL 精确匹配、仅监听不篡改、不逆向签名；捕获的榜单 JSON 先行写探针日志 `%TEMP%\compass_probe.jsonl`。验证：用户登录罗盘并打开各榜单页后，日志出现完整榜单 JSON（列表 + 视频直链字段齐全）
  - **③ 数据结构**：按抓到的真实响应梳理字段（榜单类型 / 排名 / 商品ID / 标题 / 热度指标 / GMV / 榜单视频直链等），确定 `compass_ranks` 表字段与索引，与用户确认后定稿。验证：字段清单评审通过
  - **④ 缓存层**：新建引擎 `compass_cache.py`（写入：供探针/缓存落库；读取：按榜单类型、商品ID 查询）并**入脚本白名单**。**建表归属已定（2026-10-14 用户确认）：由 compass_cache.py 自建表**（复刻 baiying_products 既有惯例：`CREATE TABLE IF NOT EXISTS` + 幂等 ALTER，**不动 db.py、不升 SCHEMA_VERSION**）。验证：CLI 写入 → `db.py sql SELECT` 读回一致；typecheck + 引擎 CLI 自测通过
  - **⑤ 无水印验证**：对榜单视频直链抽帧确认是否**源头无水印**（百应 pack_detail 已证源头无水印，罗盘需独立确认）。验证：ffmpeg 抽帧无水印 → 直链可直下；若带水印 → 回退走 `douyin_parse.py` playwm→play 换源链路（extractor 已有）
  - **⑥ UI**：商品查询页新增「榜单」tab——榜单列表展示 → 点某商品 → 按商品ID 反查 `baiying_products` 缓存抓素材 → 复用现有下载队列下载。验证：榜单真实数据展示；点商品进入该商品素材并按需下载成功
  - **制约**：②③⑤ 依赖用户登录罗盘（独立分区）并打开榜单页配合探针抓包；探针路径成熟（百应已跑通），本模块照搬即可

**📋 完整验收清单（2026-10-14 验收进行中 · 结果列逐项打勾 ✅/❌）**：

> **验收状态汇总（2026-10-14）**：清单大部分结果列为**待用户手工验收**。以下为开发期已实测通过（结果列未逐项回填，新会话可按清单复核打勾）：
> - **S5**：#16 抖音扫码登录（T-16 全套链路）、#19 `.enc` 加密备份 ✅ 实测
> - **S6**（T-15 全链路）：#21 真实商品卡+video_url 下载 ✅（瑞幸 1080x1920 可播放）、#24 SQLite 持久化 ✅、#23 commodity_id 自动补全批量抓取 ✅；#20/#22 核心链路部分实测（搜索接缓存 / 缓存商品下载）
> - **S7**（队列验收）：#25 并发 ✅、#26 退避重试 ✅、#28 断点续传 ✅、#30 重启恢复 ✅、#31 📁+完整路径提示 ✅（10/10 下载成功）；#27 限速 / #29 取消全部 **待补**
> - **S8**（素材库 CLI+UI 全过）：#32 扫描+缩略图 ✅、#33 四维检索 ✅、#34 删除 ✅、#35 打开文件夹 ✅
> - **S10**：#37 小红书解析+下载 ✅、#38 头条 CDN 直链下载 ✅、#40 批量结束停加载 ✅；#36 抖音四类入口 / #39 批量混合 **待补**
> - **S11**（账号管理）：#48-52 三平台登录入口打通 / 分区隔离不串号 / `.enc` 分账号备份 / 指纹注入 ✅ **自动化验证**（未做真实账号扫码）；#53/#55 UI 联动 ✅ 实测；#54 拉取信息 待用户把某账号抖音号改为真实值后验证
> - **去水印 #56-58**：⛔ **已搁置**（技术结论存档见上文，勿重试纯 CV 方案）
> - **S1/S2/S3/S4（#1-15）**：早期单步实测通过（见「十三、已完成事项」），结果列未回填，属**待补验证**

**S1 链接解析（Step 1）**

| # | 操作 | 预期结果 | 结果 |
|---|---|---|---|
| 1 | 粘贴抖音短链 `https://v.douyin.com/xxx/` 解析 | 返回标题/无水印地址/视频ID | ✅ 2026-10-12 真实抽纸链接实测；早期 10-08 全量验收 |
| 2 | 粘贴抖音长链接 `https://www.douyin.com/video/{id}` | 同上 | ✅ 早期录制实测 |
| 3 | 直接输入纯视频ID | 同上 | ✅ 机制 10-08/10-14 复测（10-14 样本 75279… 已删除/私密属数据问题，非解析 bug） |
| 4 | 粘贴含口令的整段文本 | 自动提取链接解析成功 | ✅ 早期录制实测 |
| 5 | 断网/超时场景解析 | 自动重试最多8次，最终给中文错误（无堆栈） | ✅ 10-14 复测 8 次重试+中文文中错误（非堆栈） |

**S2 下载与转码（Step 2）**

| # | 操作 | 预期结果 | 结果 |
|---|---|---|---|
| 6 | 下载无水印源视频 | 原分辨率+保留音轨落盘 | ✅ 2026-10-12 两条抽纸视频落盘 ffprobe 可播 |
| 7 | 下载非 H.264 视频 | 自动转码 H.264/AAC 128k | ✅ 10-12 队列 ensure_h264（hevc→h264 实测） |
| 8 | 画质校验 | 分辨率不超上限（竖1080x1920/横1920x1080） | ✅ 早期 1080x1920→1080x1800 裁剪 SSIM/PSNR 实测 |
| 9 | `--crop-top/--crop-left` 裁剪 | 左上角裁剪正确 | ✅ 10-12 T-04 完成（帮助文修正左上角口径） |
| 10 | 重复下载同一视频 | 已存在文件跳过（断点续传） | ✅ 10-12 队列 skipped 实测 |

**S3 数据库（Step 3）**

| # | 操作 | 预期结果 | 结果 |
|---|---|---|---|
| 11 | 启动建库/增量迁移 | accounts 新列 + 四新表 + videos.published 生效，schema v2 | ✅ 10-13/10-14 T-06 全过（schema_version=2） |
| 12 | db.py CLI 各命令 | JSON 输出正确 | ✅ 10-14 T-06 自测 14/14 |

**S4 客户端框架（Step 4）**

| # | 操作 | 预期结果 | 结果 |
|---|---|---|---|
| 13 | 启动客户端 | 窗口 1280x800 正常 | ✅ 10-08 实测；10-14 多次重启正常 |
| 14 | 顶部菜单/左侧导航切换视图 | 各视图渲染正确 | ✅ 10-08 实测 + 10-14 日常使用 |
| 15 | 状态栏素材数 | 与素材库扫描结果联动 | ✅ 10-12 完成（asset-library-changed 事件联动） |

**S5 登录态管理（Step 5）**

| # | 操作 | 预期结果 | 结果 |
|---|---|---|---|
| 16 | 抖音扫码登录 | 「查登录态」显示已登录 | ✅ T-16 全套链路已实测 |
| 17 | 百应登录（手机号验证码/扫码） | 已登录 | ✅ 2026-10-12 真实扫码+T-15 全链路 |
| 18 | 登录视图前进/后退/刷新/新标签页 | 操作正常 | ✅ 早期实测；10-14 起外站/新窗口改为系统浏览器打开（见变更日志） |
| 19 | 登录后检查 `data\login_credential.enc` | 生成且含 cookie 元信息 | ✅ T-16 实测（safeStorage v10 密文） |

**S6 商品查询 + 百应链路（Step 6 / T-15）**

| # | 操作 | 预期结果 | 结果 |
|---|---|---|---|
| 20 | 搜索真实商品ID | 真实商品卡+真实视频直链（非 mock） | ✅ 10-12 搜索瑞幸→真实数据 |
| 21 | 商品卡点下载 | 队列执行、落盘 `百应素材\{商品ID}\{商品ID}.mp4` | ✅ 10-12 实测 1080x1920 可播放 |
| 22 | 缓存商品列表点下载 | 同上 | ✅ 10-12 实测 |
| 23 | 批量抓取（商品ID列表） | 探针驱动逐页、进度、commodity_id 自动补全 | ✅ 10-12 只给 product_id 批量成功 |
| 24 | 重启后缓存商品 | 仍在（SQLite 持久化） | ✅ 10-12 SQLite 持久化 |

**S7 批量下载队列（Step 7）**

| # | 操作 | 预期结果 | 结果 |
|---|---|---|---|
| 25 | 配置并发数 | 生效 | ✅ 10-12 验收 |
| 26 | 失败重试 | 退避 1s/2s/5s | ✅ 10-12 断网重试可见 |
| 27 | 请求限速 | 随机抖动间隔 | ✅ 2026-10-14 引擎层复测：3 任务 200KB 本地服务、间隔 2.38/1.27s 随机且在 random(1,3) 区间（仅统计任务首条 running，排除进度事件干扰） |
| 28 | 断点续传 | 重试跳过已完成文件 | ✅ 10-12 验收 |
| 29 | 取消全部 | 进程树终止、任务标失败 | ✅ 2026-10-14 引擎层复测：taskkill /T /F 进程树终止、残留 running、模拟重启 recover 转 failed |
| 30 | 下载中重启应用 | 队列从 SQLite 恢复、未完成任务继续 | ✅ 10-12 验收（recover 转 failed） |
| 31 | 下载完成后任务行 | 显示「📁+完整路径」可打开 | ✅ 10-12 验收（10/10 下载成功+路径提示） |

**S8 素材库（Step 8）**

| # | 操作 | 预期结果 | 结果 |
|---|---|---|---|
| 32 | 扫描下载目录 | 素材入库+缩略图 | ✅ 10-12 验收（7 真缩略图） |
| 33 | 按商品ID/标签/日期/状态检索 | 结果正确 | ✅ 10-12 验收（四维检索各剩1张） |
| 34 | 删除素材 | 文件与记录同步删除 | ✅ 10-12 验收（二次确认+同步删） |
| 35 | 账号级「打开素材文件夹」 | 打开对应账号目录 | ✅ 10-12 验收（资源管理器打开并选中） |

**S10 通用链接解析（Step 10）**

| # | 操作 | 预期结果 | 结果 |
|---|---|---|---|
| 36 | 抖音短链/长链/口令/ID 四类 | 全部解析成功 | ✅ 2026-10-08 单条四类实测（短链/长链/口令录制验收，纯 ID 机制 10-08/10-14 复测）；2026-10-15 批量 8 条分享链接实测 |
| 37 | 小红书链接（已登录） | 解析成功（xsec_token 完整） | ✅ 2026-10-15 批量 10 条实测（含带签名直链 403→中文提示+NoRetryError）；2026-10-16 `/discovery/item/` 路径实测 note_id+title 提取正常 |
| 38 | 头条链接 | 内置浏览器捕获 vod 直链 | ✅ 2026-10-15 探针捕获 vod 直链下载实测（1280x720 h264+aac 30.1s 可播放）；音频流/字节分片已过滤 |
| 39 | 批量链接（多平台混合） | 带 cookie 批量解析下载 | ✅ 2026-10-15 分平台批量实测（小红书 3/5、头条 5/5 成功）；抖音+小红书混合同批机制就绪（link_batch 逐条 detect_platform），混批大样本回归见下一步 |
| 40 | 批量任务结束后 | 无后台视频播放、视图停止加载 | ✅ 2026-10-12 批量结束停加载实测（页面停止加载清空为空白页） |

**S11 账号管理（Step 11）**

| # | 操作 | 预期结果 | 结果 |
|---|---|---|---|
| 41 | 账号新建/编辑/停用/启用/删除 | 表格实时更新、统计卡联动 | ✅ 2026-10-13 account_manager.py CLI 全链路自测 + AccountManagementView 桌面实测渲染正常 |
| 42 | 分组新建/重命名/删除/分配 | 分组列表与账号分组标签联动 | ✅ 2026-10-13 CLI 自测（分组 CRUD）；UI 联动实测待补 |
| 43 | 自动分组（按运营分类） | 自动归组 | ✅ 2026-10-13 CLI 自测（3 账号自动归组 美妆/食品） |
| 44 | 统计卡 | 账号数/已分组/素材数/待发布/千川数正确 | ⏳ UI 渲染正常（2026-10-13 桌面实测），数值正确性待真实数据 |
| 45 | 每账号统计列 | 素材数/状态正确（计划发布率/卡优率/广告条数待接入） | ⏳ 素材数/状态列已实现；计划发布率/卡优率/广告条数**待接入**（TODO 原标注） |
| 46 | 批量控制（启用/设置分组/确认执行/重置） | 批量生效、有提示 | ⏳ UI 已实现，批量生效实测待补 |
| 47 | 千川账户增删/绑定账号(1:N)/复制千川ID | 正确 | ✅ 2026-10-13 CLI 自测千川 1:N 绑定 2 账号；复制千川 ID UI 实测待补 |
| 48 | 抖音登录态：打开浏览器登录+查登录态 | 显示已登录 | ✅ 2026-10-13 自动化验证（未登录正确报「未登录」）；真实账号扫码待用户 |
| 49 | 切换账号打开浏览器 | 互不串号（各自独立分区） | ✅ 2026-10-13 分区目录 `Partitions/acct-1、acct-2` 独立创建验证 |
| 50 | 登录后检查 `data\login_credential_acct_{id}.enc` | 生成分账号加密备份 | ✅ 2026-10-13 未登录不生成 .enc 验证；真登录后生成待真实扫码 |
| 51 | 千川「登录千川」+查状态 | 在线状态列变绿 | ✅ 2026-10-13 千川登录入口打通（qianchuan.jinritemai.com）；在线状态列变绿待真实账号 |
| 52 | 指纹隔离（换账号） | 各分区 storage 隔离、canvas 指纹不同（机制已注入） | ✅ 2026-10-13 指纹注入机制验证（一账号一指纹） |
| 53 | 账号级操作 | 打开/关闭浏览器、打开精选页（百应）、打开素材文件夹 | ✅ 2026-10-13 UI 联动实测 |
| 54 | 拉取信息（真实抖音号） | 回填昵称/粉丝数/作品数 | ⏳ 探针链路已跑通（假号 10001 正确报「未抓到用户数据」）；**待用户把某账号抖音号改为真实值后验证** |
| 55 | 页面滚动（多账号/分组时） | 滚动条出现、可下滑 | ✅ 2026-10-13 滚动修复实测（注入 30 占位块 scrollHeight 3284 > clientHeight 880，滚动生效） |

**去水印（已搁置 ⛔ 2026-10-14）**

| # | 操作 | 预期结果 | 结果 |
|---|---|---|---|
| 56 | 下载成功行点「去水印」 | 弹窗显示帧图、可标注水印 | ⛔ 搁置 |
| 57 | 标注/自动找水印→确认 | 生成 `_nowm.mp4` | ⛔ 搁置 |
| 58 | 播放去水印视频 | 移动水印去除 | ⛔ 搁置 |

**应用当前是否在跑**：Step 5 结束时 Electron 处于运行状态（`npm run dev` 后台）。新会话可先 `Get-Process electron` 确认。

### 0.6 建议的下一步顺序

1. **T-04**（纯本地改动，不受封锁影响，最快能完成）
2. **T-15**（确认百应 URL，否则登录功能只能用于抖音主站）
3. **T-03**（封锁衰减后复测，验证 Step 1 修复是否真生效）
4. **T-02 + T-01**（规模化前提，建议一起做）
5. **T-11**（把引擎真正接进客户端）

---

## 项目结构

```
ad-client/
├── engine/                      Python 处理引擎
│   ├── douyin_parse.py           分享链接解析（Step 1）
│   ├── download.py               下载 + 转码 + 裁剪 + 画质校验（Step 2）
│   ├── db.py                     SQLite 数据层（Step 3）
│   ├── session_bridge.py         接收登录态、转成请求头（Step 5）
│   ├── ping.py                   连通性自检 + NDJSON 协议示范（T-11）
│   ├── demo_task.py              进度/取消演示脚本（T-11 协议模板）
│   ├── product_query.py          商品查询 mock 数据源（商品查询主界面）
│   ├── download_queue.py          批量下载任务队列引擎（并发/重试/限速/断点/持久化）
│   ├── asset_library.py           本地素材库（扫描/检索/缩略图/删除/标签）
│   ├── requirements.txt
│   └── .venv/                    虚拟环境（不入库）
├── desktop/                     Electron 桌面客户端（Step 4 / 5）
│   ├── package.json
│   ├── electron.vite.config.ts
│   ├── tsconfig{,.node,.web}.json
│   ├── .npmrc                    Electron 二进制走国内镜像
│   └── src/
│       ├── main/
│       │   ├── index.ts          主进程入口（窗口 1280x800）
│       │   └── login.ts          登录态管理 + 内置浏览器视图（Step 5）
│       ├── preload/
│       │   ├── index.ts          白名单 IPC 出口
│       │   └── index.d.ts        渲染进程类型声明
│       └── renderer/
│           ├── index.html
│           └── src/
│               ├── main.ts / App.vue / env.d.ts
│               ├── assets/main.css        设计变量集中处
│               └── components/
│                   ├── TopMenu.vue        顶部菜单
│                   ├── SideNav.vue        左侧导航
│                   ├── WorkArea.vue       中部工作区
│                   ├── BrowserPane.vue    登录面板 + 视图宿主（Step 5）
│                   └── StatusBar.vue      底部状态栏
└── docs/
    └── TODO.md                  本文档
```

**数据目录**（D 盘，两个用途分开）：

```
D:\software\投广客户端\
├── data\        SQLite 数据库（ad_client.db）
└── downloads\   视频成品
    └── {账号}\{日期}\{商品ID}\{商品ID}_{视频ID}_{序号}_{时间戳}.mp4
```

**Electron 运行时目录**：`C:\Users\9\AppData\Roaming\touguang-client\`
（登录态在 `Partitions\douyin-login\` 下）

---

## 一、待办总览

| 优先级 | 编号 | 事项 | 归属 | 状态 |
|---|---|---|---|---|
| 🔴 P0 | T-01 | 解析层限流改造（全局令牌桶 + 熔断） | engine | 未开始 |
| 🔴 P0 | T-02 | Playwright 作为解析层主方案 | engine | 未开始 |
| 🔴 P0 | T-03 | 抖音封锁衰减后复测重试逻辑 | engine | ✅ **完成（2026-10-12 封锁已解除）** |
| 🟠 P1 | T-04 | 修正 `download.py` 裁剪提示为**左上角** | engine | ✅ **完成（2026-10-12）**：help 与 docstring 示例已改为 `--crop-top/--crop-left`（实测水印在左上角） |
| 🟠 P1 | T-05 | 1080p 画质缺口（当前只拿到 720p） | engine | ✅ **完成（2026-10-16，用户裁定规格=最高可得画质）** |
| 🟠 P1 | T-06 | 补完 `db.py` 自测（22/23 项未跑） | engine | ✅ **完成（2026-10-14）**：补完自测 7~23 项 **14/14 全过**（序号自增/查重幂等/商品自动创建/video-check 命中与未命中/video-list 带账号名+product_code/stats/sql 只读/拦截 DELETE 与多语句/账号不存在中文报错/非法 status 拦截/account-list video_count/外键级联清空子表） |
| 🟠 P1 | T-07 | 打包前加 CSP | desktop | 未开始 |
| 🟠 P1 | T-15 | **百应接入与商品视频下载** | desktop+engine | **完成（全链路已实测）**：L2 验证 `pack_detail.media.video_url` 可下载、水印实测源头无水印、`baiying_products` 缓存模块、半自动批量浏览（7 轮二分确定 URL 必需参数集）、**commodity_id 自动补全（探针解析选品广场列表接口，只提供 product_id 即可批量抓取）**、**缓存商品列表 + 真实 video_url 下载队列（实测下载 1080x1920 可播放）**、**商品查询搜索已接缓存真实数据（替换 mock 源，实测：搜索瑞幸→真实商品卡+视频直链+下载成功）** |
| 🟠 P1 | T-16 | 登录后验证 cookie 加密落盘 | desktop | ✅ **完成（B+ 方案）** |
| 🟡 P2 | T-08 | `-c copy` 直通模式（零损失转封装） | engine | 未开始 |
| 🟡 P2 | T-09 | 会话 Cookie 复用（含 `_wafchallengeid`） | engine | 未开始 |
| 🟡 P2 | T-10 | `electron-builder.yml` 打包配置 | desktop | 未开始 |
| 🟡 P2 | T-11 | Electron ↔ Python 引擎 IPC 打通 | desktop | ✅ **完成（2026-10-16）** |
| 🟡 P2 | T-12 | UI 组件库评估（Element Plus 等） | desktop | 未开始 |
| 🟡 P2 | T-17 | 登录态失效检测与自动提示 | desktop | 未开始 |
| 🟡 P2 | T-18 | Cookie 过期策略 | desktop+engine | 未开始 |
| ⚪ P3 | T-13 | 评估抖音开放平台官方 API | 决策 | **有条件可行，功能闲置待凭证**（2026-10-13 更新；2026-10-14 再更新：甲方千川开发者申请中，用户裁定「先不用抖川工具，等千川开发者申请下来」）：巨量千川开放平台 `qianchuan/file/video/aweme/get/` 支持商品 ID 过滤拉取带货视频（含 url）；甲方确认有千川投放账号（advertiser_id `16761573861161`）；《千川开放平台接入操作单》已转甲方，待 app_id/secret/aweme_id/Access-Token 下发后实现接入 |
| ⚪ P3 | T-14 | 限流阈值标定实验 | engine | 未开始 |

---

## 二、P0 阻塞级

### T-01 解析层限流改造（全局令牌桶 + 熔断）

**背景**：Step 1 为应对抖音 SSR 随机降级，加了「最多 8 次重试」。这在**单条视频**场景下是正确的，但在**批量场景**下会变成帮凶：

```
1 条视频 = 1 次短链跳转 + 最多 8 次分享页请求
500 条视频 = 500 + 最多 4000 次请求
```

**重试把请求量最高放大 8 倍**，批量上线第一天就会被封。

**需要做的**：

1. **全局令牌桶节流器**：所有对抖音的请求统一排队，不再由各任务各自乱发
2. **熔断器**：一旦命中 WAF（`stage = "waf_challenge"`），**整个进程冷却 N 分钟**，而不是逐任务继续重试
3. 请求间隔加**随机抖动**（如 3s ± 1s），避免固定节拍被识别

**验收**：连续处理 100 条视频，不触发 `waf_challenge`。

---

### T-02 Playwright 作为解析层主方案

**背景**：实测抓到抖音 WAF 的 JavaScript 挑战页（`html_length ≈ 2492`）：

```html
<script src="https://lf-waf-js.byted-static.com/obj/waf-jschallenge/out-sha256.js">
window.WAFJS('context.merge', { ..., webdriver: navigator.webdriver, ... })
expect === s256(prefix, ""+i) && ... document.cookie = "_wafchallengeid=..."
Please wait...
```

它要求客户端**执行 JS 暴力搜索 SHA-256 前像**（工作量证明）→ 写 cookie → 刷新页面。
**纯 `requests` 永远过不去**，重试 8 次拿到的全是同一个页面。

**建议方案**：

- 把 **Playwright 真浏览器**作为解析层**主路径**（真浏览器天生会执行这个 JS 挑战，不需要任何"破解"）
- `requests` 降级为「快速路径」：先用它试一次，命中 WAF 就切 Playwright
- **浏览器实例要池化复用**，不要每条视频开一个新浏览器

**⛔ 明确不做**：不实现那个 SHA-256 挑战的绕过算法（见 §11 合规边界）。

**验收**：WAF 挑战页出现时，Playwright 能自动通过并拿到 `_ROUTER_DATA`。

**💡 关联**：Step 5 已经用 Electron 的 `WebContentsView` 跑通了「真浏览器过登录」这条路 —— 说明真浏览器方案在本项目可行，Playwright 是同一思路的可编程版本。

---

### T-03 抖音封锁衰减后复测重试逻辑

**背景**：开发期间为量化成功率，在十几分钟内对抖音发了 **50+ 次请求**，触发 WAF 硬封锁。**截至 2026-10-08 仍未解除**（等待 90 秒后复测仍全是 2492 页面）。

**⚠️ 这意味着**：Step 1 的「软降级重试成功」是**在封锁之前**验证的；封锁期间只能验证失败路径。**成功路径需要复测**。

**复测方法**（等几小时到一天后再做）：

```powershell
$PROJ = "D:\抖音电商\投广管理客户端"
& "$PROJ\engine\.venv\Scripts\python.exe" "$PROJ\engine\douyin_parse.py" "https://v.douyin.com/7as3ye0h9Nw/" --verify
```

**通过标准**：`"ok": true`，且 `diagnostics.succeeded_on_attempt` **≥ 2**
（≥2 就说明重试真的救回了软降级，而非一次就中）

**如果 `stage` 是 `waf_challenge`**：说明封锁还没解除，继续等，**不要反复试**。

---

## 三、P1 功能缺口

### T-04 修正 `download.py` 裁剪提示为「左上角」

**背景**：我在 `download.py` 的帮助文本里写了：

```
--crop-bottom  裁掉底部像素数（抖音水印常在此处）
```

**这是基于印象的猜测，实测证明是错的。** 抽帧放大后确认：抖音水印在**左上角**（抖音彩色音符 Logo + 「抖音」+「抖音号：xxx」三层叠加），不在底部。

**需要做的**：

1. 修正 `--crop-bottom` 的 help 文本，把「抖音水印常在此处」挪到 `--crop-top` / `--crop-left`
2. 在脚本 docstring 的用法示例里，把裁剪示例从 `--crop-bottom 120` 改成左上角写法

**参考实测数据**：视频 1280x720，水印大致占据 `crop=420:140:0:0` 的区域。

> **注**：本项目正常情况下**用不到裁剪** —— 去水印是「换成无水印源」实现的（见 §8.2）。裁剪只是兜底。

---

### T-05 1080p 画质缺口 ✅（2026-10-16 用户裁定：规格=最高可得画质）

**背景**：规格要求「保留原始最高分辨率（≤1080p）」，但实测两条真实链接**都只能拿到 720p**：

| 视频 | 地址里的 ratio | 实际分辨率 | 候选数 |
|---|---|---|---|
| 7693062724168249969 | `ratio=720p` | 960x720 | 1 |
| 7549960424303037754 | `ratio=720p` | 1280x720 | 1 |

**原因**：分享页的 `play_addr` 只给 **1 个候选**，**没有 `bit_rate` 数组**（即抖音没暴露多档清晰度）。

**2026-10-16 调查结论（三条路径实测后收敛，规格改为最高可得画质）**：
- ①匿名分享页基线复测：`7302704301653101850`（昨天批量成功的抽纸视频）解析成功（attempt=2）仍**只有 720p 单候选**（无 bit_rate 数组）——缺口复现
- ②yt-dlp 2026.8.19（最新版，临时安装测试后已卸载）：抖音 extractor 请求 `web detail JSON` 被 **403**——`www.douyin.com/aweme/v1/web/aweme/detail/` 接口需要 `a_bogus`/`msToken` 签名参数，yt-dlp 也无法自动生成（同样撞签名墙）；带分区登录 cookie 也一样 403
- ③登录态详情接口：与 yt-dlp 撞同一堵墙；手工构造签名属**合规红线（不逆向）**
- **结论**：合规边界内抖音分享链接最高可得 720p。**1080p 在商业源已实现**：百应 `pack_detail` 视频直链实测 1080x1920 无水印（瑞幸视频已实证）。
- **裁定（2026-10-16）**：规格改为「最高可得画质」——分享链接 ≤720p（合规边界），商业源（百应/罗盘）可达 1080p。T-05 关闭，不再投入。yt-dlp 临时依赖已卸载，venv 恢复精简（仅 requests）。

---

### T-06 补完 `db.py` 自测

**背景**：Step 3 自测跑到第 7 项时中断，**中断原因是我测试脚本的 bug**（给 `subprocess` 传了 `int` 而非 `str`），不是 `db.py` 的问题。

**已验证通过的部分**（前 6 项）：

- ✅ `init` 建表，4 张表齐全（accounts / products / tasks / videos）
- ✅ WAL 日志模式已开启
- ✅ 新增账号
- ✅ 重复加账号幂等（`created: false`）
- ✅ 新增商品
- ✅ 空商品 `next-seq = 1`

**未验证的部分**（第 7–23 项）：

- ⬜ 序号自增（视频1 → seq=1，视频2 → seq=2）
- ⬜ 查重：重复 `video-id` 被拦（按 `video_id` 全局唯一）
- ⬜ 商品不存在时自动创建
- ⬜ `video-check` 命中 / 未命中
- ⬜ `video-list` 带出账号名与商品 ID
- ⬜ `stats` 统计与状态分布
- ⬜ `sql` 只读查询可用
- ⬜ **写操作被拦截**（`DELETE` / 多语句）
- ⬜ 账号不存在报错
- ⬜ 非法 `status` 被拦
- ⬜ `account-list` 的 `video_count`
- ⬜ **外键级联**：删账号 → 子表清空

**修复要点**：测试脚本里所有传给 `subprocess` 的参数都必须是字符串（如 `"--duration-ms", "15600"`）。

---

### T-07 打包前加 CSP

**背景**：`desktop/src/renderer/index.html` 里已留 TODO 注释。

现在**故意没加** Content-Security-Policy，因为 Vite 开发模式依赖内联脚本和 HMR 连接，加了会开不起来。

**需要做的**：打包前加 CSP，限制只允许加载本地资源。

---

### T-15 确认百应入口 URL（待用户答复）

**背景**：我在 `desktop/src/main/login.ts` 的 `LOGIN_TARGETS` 里填了猜测值：

```ts
{ id: 'baiying', name: '巨量百应', url: 'https://buyin.jinritemai.com/',
  domains: ['jinritemai.com', 'douyin.com'] }
```

**这个 URL 我没有依据，是猜的**。用户菜单里有「百应工作台」，但没告诉我入口地址。

**需要做的**：问用户实际用的百应入口 URL，改 `LOGIN_TARGETS`（**改 URL 时 `domains` 必须一起改**，否则 cookie 导出会漏）。

---

### T-16 登录后验证 cookie 加密落盘 ✅（2026-10-10 完成，方案 A 失败 → 改 B+）

**实测结论（推翻原假设）**：

1. **Electron persist 分区 cookie 明文是平台行为，不可修复**。登录后取证：`Partitions\douyin-login\Network\Cookies` 里持久 cookie 全部明文存在 `value` 列，`encrypted_value` 全空。
2. **方案 A（手动注入加密密钥）验证失败**：往 `Partitions\douyin-login\Local State` 注入 `os_crypt.encrypted_key`（DPAPI 保护）后重启重登，cookie 依然明文。根因：Chromium 只从 **userData 根目录** 的 Local State 读 os_crypt key，partition 子目录的 key 根本不加载；而根目录 key 只服务默认 session，persist 分区的 CookieStore 不挂接 os_crypt 加密路径。
3. **安全现状反而更好**：抖音当前把登录凭证（`sessionid` 等）下成 **session cookie（仅内存、不落盘）**，磁盘库里只有游客级持久 cookie（ttwid / `__ac_signature` / odin_tt 等）。但**不可依赖**——首次登录实测曾出现凭证明文落盘（48 条明文含 sessionid）。

**采用的方案（B+）：应用层 safeStorage 加密凭证副本**

- `login.ts` 新增：登录成功后（`checkLoginState` 检测到凭证）把目标域 cookie 用 `safeStorage`（Windows DPAPI）加密写 `D:\software\投广客户端\data\login_credential.enc`；登出时删除
- 导出的引擎链路不变（从内存取、stdin 传递、不落盘）；副本仅供验证/备份，业务不依赖
- 验证通过：文件前 16 字节 hex = `763130…`（v10 AES-GCM 密文）；UI 状态行显示「凭证副本：已加密保存 48 条（时间）」，刷新自动更新
- 遗留边界（如实记录）：Chromium 分区库里的**游客级**持久 cookie 仍明文（平台行为）；若抖音将来把凭证下成持久 cookie，仍会明文落 Chromium 库——B+ 副本保证「我们控制的链路」不再明文依赖

---

## 四、P2 优化项

### T-08 `-c copy` 直通模式

**背景**：实测发现，当**无需裁剪/缩放**且**源已是 h264+aac** 时，转码是**纯粹的损失**：

| 项目 | 源文件 | 成品 |
|---|---|---|
| 编码 | h264 + aac | h264 + aac |
| 大小 | 5.66MB | **10.15MB** |
| SSIM | — | 0.99338（掉了） |

转码**没有产生任何收益**，只是白丢画质、还让文件变大。

**需要做的**：当「不需要裁剪/缩放 + 源已是 h264/aac」时，改用 `-c copy` 直接封装：

- **零画质损失**
- **1 秒完成**（不需要重编码）
- 依然满足「MP4 + H.264 + AAC」的规格

---

### T-09 会话 Cookie 复用

**背景**：一旦通过了 WAF 的 JS 挑战，抖音会下发 `_wafchallengeid` cookie。

**需要做的**：把 cookie 持久化到本地，后续请求带上，避免每次都重新过挑战。

**💡 关联**：Step 5 已经把**登录 cookie**的持久化 + 导出通道做完了（`session_bridge.py`）。T-09 要做的是把**同一套机制**用在 WAF 挑战 cookie 上。

---

### T-10 `electron-builder.yml` 打包配置

`electron-builder` 已装好（v26.15.3），但配置文件还没写。

**需要做的**：出安装包时再配 —— appId、productName（投广管理客户端）、图标、NSIS 选项、输出目录。

**注意**：打包时要把 `engine/` 一起带上，并处理好 Python 解释器路径（开发态与打包态不同）。

---

### T-11 Electron ↔ Python 引擎 IPC 打通

**背景**：当前进度是**核心已完成，真实任务接入待做**：

| 部分 | 状态 |
|---|---|
| IPC 白名单通道（`preload` 暴露 `browser.*` / `login.*` / `engine.*`） | ✅ 已完成 |
| 登录态导出并转成请求头（`session_bridge.py`） | ✅ 已完成并实测 |
| **主进程用 `child_process` 拉起 Python 脚本** | ✅ **完成**：`desktop/src/main/engine.ts`（2026-10-08 UI 实测：连通性 / NDJSON 实时进度 / 进程树取消 / 中文错误美化 全部通过） |
| 引擎 NDJSON 协议 + 示范脚本 | ✅ 完成：`engine/ping.py`（连通性自检）、`engine/demo_task.py`（进度·取消协议模板） |
| 真实任务接入 UI（`douyin_parse` / `download`） | ✅ **完成（2026-10-16）**：新增 `LinkToolView.vue` 单条链接解析下载工具（`百应工作台` 默认视图右面板，替换原 EngineConsole 演示控制台）——粘贴一条链接 → `link_parser.py` 统一解析（抖音/头条/小红书自动识别，小红书 cookie 走 stdin）→ 结果卡（标题/作者/时长/无水印地址可复制）→ 下载（复用 `download_queue.py` 单任务，落盘 `抖音素材\{日期}\{vid}.mp4`，进度/重试/完整路径/📁 打开）；保留连通性测试；typecheck 通过 |
| **把 `session_bridge` 接进 `douyin_parse.py`** | ✅ **完成（2026-10-16）**：UA 取舍由实践定案（抖音匿名 iPhone UA 走分享页 SSR 无需 cookie；小红书需登录 cookie）——`link_parser.py` 新增 stdin 可选 cookie（与 `link_batch.py` 同约定，登录态不经过进程命令行），客户端解析时导出小红书登录态经 stdin 传入 |

**需要做的**：

1. 在 `desktop/src/main/` 加一个 `engine.ts`，封装 `child_process.spawn` 调用 Python
2. 调用时把登录态 JSON **写到子进程 stdin**，不落盘
3. 暴露 IPC：`parseVideo(url)` → `douyin_parse.py`、`startDownload(params)` → `download.py`、`queryDb(...)` → `db.py`

**注意事项**：

- Python 子进程路径在开发态与打包态不同
- **一定要把登录态里的 UA 一起传给引擎**（见 §8.3），否则会话可能失效
- 用 `spawn` 而非 `exec`，并注意读取 stdout/stderr 防止管道写满死锁（`download.py` 里 `run_ffmpeg_with_progress` 有现成的反面教材说明）

---

### T-12 UI 组件库评估

**背景**：Step 4 的空壳布局**故意零依赖**（原生 CSS Grid），够用且没有额外负担。Step 5 的登录面板也是手写 CSS。

**需要做的**：开始做真实表格 / 表单 / 分页时，再评估引入 **Element Plus**（Vue 3 生态最成熟的国内组件库）。届时说明引入理由。

---

### T-17 登录态失效检测与自动提示

**背景**：登录 cookie 会过期或被服务端踢掉。目前只在用户手动点「检查登录状态」时才知道。

**需要做的**：

- 应用启动时自动查一次登录态，未登录就给提示
- 引擎请求返回「需要登录」信号时，通知客户端弹出重新登录引导

---

### T-18 Cookie 过期策略

**背景**：cookie 有 `expires_utc` 字段（`douyin-login` 分区里能读到）。

**需要做的**：

- 导出给引擎时过滤或标记已过期的 cookie
- 在客户端显示「登录态剩余有效时间」，提前提醒续期

---

## 五、P3 决策级

### T-13 评估抖音开放平台官方 API

**背景**：坦白说，**大规模抓取本身就是在与平台风控对抗**，投入会持续被抖音改版吃掉。三条路径的性价比：

| 路径 | 评价 |
|---|---|
| **抖音开放平台官方 API** | 🟢 **根治**。正经电商选品场景，官方有商品/视频相关开放能力。稳定、合规、不怕改版 |
| **Playwright 模拟正常用户** | 🟡 可行，但要持续维护。节制频率下能长期跑 |
| **纯 requests 高频抓取** | 🔴 必被封，不可持续 |

**需要做的**：如果这个客户端要长期给业务用，认真评估第一条。抓取方案保留为「官方 API 覆盖不到时的补充」，而非主路径。

**💡 关联**：Step 5 已经实现了扫码登录自己账号的能力 —— 这正是走官方 API 的前提条件之一。

---

### T-14 限流阈值标定实验

**已知**：50+ 次请求 / 约 15 分钟会触发 WAF。

**未知**：

- 确切的频率阈值
- 封锁是按 IP、按 cookie 还是按设备指纹
- 封锁持续多久（实测 90 秒不够，实际可能几小时）

**需要做的**：用低频率慢慢加，记录 `32333`（软降级）出现的比例，找到拐点。

> ⚠️ **这个实验本身会消耗 IP 额度**，建议放到功能开发后期再做。

**可用信号**：软降级（`html_length = 32333`）出现在硬封锁（`2492`）**之前**，可作为**限流预警指标** —— 它一升高就说明快被拦了，应主动降速。

---

## 六、三种失败模式的区分（重要参考）

抖音分享页实测会出现三种截然不同的响应，处置方式完全不同：

| html_length | 含义 | 重试有用吗 | 处置 |
|---|---|---|---|
| **38249**（及 38257 / 36317） | 正常完整页，含 `item_list` | — | 正常解析 |
| **32333** | SSR **软降级**（空壳页，`item_list` 为空） | ✅ **能救**（已验证） | 重试即可 |
| **2492** | **WAF JS 挑战** | ❌ 不能，且**不该**重试 | 立即停手，切 Playwright 或等封锁衰减 |

**代码里已有对应处理**：

- `douyin_parse.py` 的 `BLOCK_HINT_RE`（风控词检测）
- `douyin_parse.py` 的 `WAF_CHALLENGE_RE`（WAF 挑战检测）
- 命中 WAF 时**立即 `break`**，不继续重试（`fetch_share_page_with_retry` 里）

---

## 七、登录态架构说明（Step 5）

```
┌─ 渲染进程 BrowserPane.vue ──────────────────────────────┐
│  工具栏（选目标/打开登录页/检查状态/登出/导出自检）        │
│  宿主容器 <div ref="hostEl">  ← 用 getBoundingClientRect │
│                                 量出位置上报给主进程      │
└───────────────────────┬─────────────────────────────────┘
                        │ IPC（preload 白名单）
┌───────────────────────▼─────────────────────────────────┐
│  主进程 login.ts                                         │
│   · WebContentsView 覆盖在工作区之上（不是 DOM 元素）     │
│   · partition = 'persist:douyin-login' → cookie 落盘     │
│   · 登录取 cookie + UA → 序列化 JSON                     │
└───────────────────────┬─────────────────────────────────┘
                        │ child_process.spawn 的 **stdin**
┌───────────────────────▼─────────────────────────────────┐
│  engine/session_bridge.py                                │
│   build_headers(session) → requests 直接可用             │
└─────────────────────────────────────────────────────────┘
```

**IPC 通道清单**（改接口时 `preload/index.ts` 和 `preload/index.d.ts` **两处都要改**）：

| 通道 | 作用 |
|---|---|
| `login:getTargets` | 取登录目标清单 |
| `browser:open` | 打开登录页（创建/显示视图） |
| `browser:close` | 移除视图（**不清登录态**） |
| `browser:setBounds` | 渲染进程上报视图位置 |
| `browser:goBack` / `goForward` / `reload` / `getState` | 导航 |
| `browser:stateChanged` / `needBounds` / `loadFailed` | 主→渲染事件 |
| `login:check` | 查登录态（**只返回 cookie 名字，不返回值**） |
| `login:clear` | 登出 |
| `login:exportForEngine` | 导出会话给引擎（**返回值含明文，不落盘**） |
| `login:stateChanged` | 登录态变化推送（扫码后自动刷新徽章） |

**登录判定**：候选 cookie 名单 `sessionid` / `sessionid_ss` / `sid_tt` / `sid_guard`，命中任一即视为已登录。
**怎么确认哪个真正生效**：登录前点一次「检查登录状态」，登录后再点一次，对比 `cookieNames`，**新出现的那个就是凭证**。

---

## 八、关键设计决策与理由（避免推翻重来）

### 8.1 去水印为什么不用 AI / 不用 inpaint

抖音**服务器上本来就存着两个版本**：

- `.../aweme/v1/playwm/?...` → **带水印**（wm = watermark）
- `.../aweme/v1/play/?...` → **无水印**

把 URL 里的 `playwm` 改成 `play`，**直接拿干净的那一份**，画面一个像素都没动过。

**实测铁证**：抽帧放大对比，水印版左上角有「抖音彩色音符 Logo + 抖音 + 抖音号：93655870973」，无水印版这三层**全部消失**，只剩作者自己的签名。
另外水印版还多 **3 秒抖音品牌片尾**（485 帧 vs 395 帧，正好 3.0 秒）。

**结论**：`--crop-*` / 模糊遮盖是**兜底方案**，现在用不上。不要把主路径改成 inpaint。

### 8.2 为什么必须连 User-Agent 一起传给引擎

抖音风控会把登录态和 UA 关联。登录用的是**桌面 Chrome 的 UA**，若 Python 请求时改成 iPhone UA，服务端可能判会话异常而**直接失效**。

所以 `exportForEngine()` 同时返回 `cookieHeader` 和 `userAgent`，**两者不能拆开使用**。

由此还引出一个**待解决的设计问题**：`douyin_parse.py` 现在用 iPhone UA 请求分享页（因为它走的是移动端 SSR 路径）。登录态接入时要想清楚：
- 要么请求分享页时也用桌面 UA + cookie
- 要么让 `WebContentsView` 用 iPhone UA 登录（但桌面窗口里渲染移动页面体验差）

**这个取舍没定，接入 T-11 时必须先想清楚。**

### 8.3 为什么登录态走 stdin 而不是文件

1. 写文件就多一份副本，还要管清理，泄漏面变大
2. 加密文件也得把解密密钥交给 Python，绕一圈又回到"怎么给密钥"
3. stdin 是**进程私有**的 —— 进程列表看不到、磁盘上不留痕

### 8.4 分辨率上限为什么按方向算

短视频多为竖屏。如果无脑用「长边 ≤ 1920」，竖屏 **1080x1920 会被压成 607x1080**，白白丢掉一半像素。

所以按方向给盒子：

| 方向 | 上限 |
|---|---|
| 横屏（w ≥ h） | 1920 x 1080 |
| 竖屏（w < h） | 1080 x 1920 |

并且**只缩不放**（`ratio = min(box/w, box/h, 1.0)`），小视频绝不上采样。不需要缩放时**直接跳过 scale 滤镜** —— 不重采样就没有额外画质损失。

### 8.5 为什么裁剪在前、缩放在后

- **先裁剪**：你在播放器里量的像素值就是原图像素，裁完剩下的像素原封不动
- **后缩放**：只有裁完仍超过 1080p 才缩

反过来做的话，裁剪参数就不是原图像素了，用户没法直接量。

### 8.6 为什么 `WebContentsView` 的容器不能滚动

`WebContentsView` 是**覆盖在网页之上的独立图层**，不是 DOM 元素：

- 它永远盖在网页内容上面 → 界面里的下拉框、提示气泡**会被它遮住**
- 位置只能由渲染进程测出来上报 → 一旦容器能滚动，**滚动时覆盖层不会跟着动，位置就错位**

所以 `WorkArea` 的 `overflow` 必须是 `hidden`。

### 8.7 为什么 SQLite 要开 WAL + 每连接开 foreign_keys

- **WAL**：写操作不阻塞读操作。桌面端「引擎在写、界面在读」必需
- **`PRAGMA foreign_keys = ON`**：SQLite **默认关闭**外键约束，不显式打开，删账号时子表不会级联删除，约束形同虚设

---

## 九、环境坑记录（重装时必看）

### E-01 Electron 二进制可能静默下载失败

**现象**：`npm install` 报 `exit=0`，但 `node_modules\electron\dist` 是空的；`npm run dev` 直接报：

```
Error: Electron uninstall
    at getElectronPath (electron-vite/dist/chunks/lib-*.js)
```

**原因**：`electron` npm 包的 **postinstall 脚本负责单独下载主程序二进制**（100+ MB）。它失败了 npm 不一定报错。

**关键教训：`npm install` 成功 ≠ Electron 能跑。**

**修复**：

```powershell
$APP = "D:\抖音电商\投广管理客户端\desktop"
Set-Location $APP
$env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
node node_modules/electron/install.js
```

**验证**：`node_modules\electron\path.txt` 存在且内容为 `electron.exe`，`dist` 目录有约 19 项。

---

### E-02 版本必须钉死在兼容区间

`npm install` 默认拉最新版，会踩两个坑（已修复）：

| 包 | 错误版本 | 问题 | 正确版本 |
|---|---|---|---|
| `typescript` | 7.0.2 | TS 7（原生移植版）不再导出 `./lib/tsc`，`vue-tsc` 崩：`ERR_PACKAGE_PATH_NOT_EXPORTED` | **`^5`（5.9.3）** |
| `vite` | 8.3.3 | 超出 `electron-vite@5` 声明的 peer 范围 `^5\|\|^6\|\|^7` | **`^7`（7.3.7）** |

**装新依赖时务必检查 peer 范围**，不要盲目用 `latest`。

---

### E-03 electron-vite 脚手架无法非交互执行

即使传了 `--template vue-ts`，仍会弹交互提示（`Add Electron updater plugin?`）。
用管道喂答案会**静默接受默认值**，生成 vanilla + 无 TypeScript 的错误骨架。

**结论**：本项目**改为手写全部文件**，不依赖脚手架。若将来要用，必须手动交互回答。

---

### E-04 Git 未加入 PATH

Git 装在 `D:\software\Git`（2.53.0），但**不在 PATH 里**。

**需要提交代码时**：用完整路径 `& "D:\software\Git\cmd\git.exe" ...`，或把 `D:\software\Git\cmd` 加进 PATH。

**注意**：`ad-client/` 目前**还没有 git 仓库**（`watermark-remover/` 也没有）。

---

### E-05 npm 会警告 `.npmrc` 里的镜像配置

`.npmrc` 里的 `electron_mirror` / `electron_builder_binaries_mirror` 在 npm 11 下会输出：

```
npm warn Unknown project config "electron_mirror"
```

**这是噪音，不是错误** —— `@electron/get` 通过 `npm_config_electron_mirror` 环境变量读取，实测**确实生效**（`npm config get electron_mirror` 能取到值）。

---

### E-06 查 Cookie SQLite 时要先关掉 Electron

`Partitions\douyin-login\Network\Cookies` 被运行中的 Electron **独占锁定**，`Copy-Item` 会报：

```
The process cannot access the file ... because it is being used by another process
```

**做法**：先 `Get-Process electron | Stop-Process -Force`，再复制副本读取，读完记得重新启动应用。

---

### E-07 Vue 模板里不能直接写 `window.xxx`

Vue 模板作用域**不包含 `window`**（白名单只有 `Math` / `Date` / `JSON` 等）。
写 `@click="window.api.browser.reload()"` 会静默失败。

**做法**：在 `<script setup>` 里包一层函数，模板调函数。

### E-08 D 盘部分目录只读，需要管理员授权

**现象**：往 `D:\抖音电商\` 写文件报 `Access is denied`（ERROR 5）。`robocopy` 会在「Creating Destination Directory」阶段直接失败。

**原因**：`D:\抖音电商` 与 `Program Files` / `nodejs` / `mysql` 同类 —— 是**被提权工具创建的目录**，ACL 只给了 `Administrators` 和 `SYSTEM` 完全控制，普通用户只有 `ReadAndExecute`。`D:\` 根目录同理。

**本机普查结果**（2026-10-08）：

- **只读 13 个**：`dsharn` `mysql-8.0.29-winx64` `nodejs` `Program Files` `Project(ruanjian)` `QMDownload` `SD WebUI DirectML` `Text2` `video` `WeGameApps` `专业软件` `抖音电商` `电商agent`
- **可写 20 个**：`-` `5EDemocache` `apache-tomcat-10.1.53` `desireproject` `ftp下载` `GameVideos` `GIT` `idealproject` `jdk17` `JDk21` `MAVEN` `software` `steam` `大三下作业` `微信聊天` `文件` `普通软件` `网页下载` `解压` `豆包下载`

**关键点**：当前账号 `DESKTOP-OCN2MO6\9` **不是管理员**，无法自己给自己授权，必须在**管理员 PowerShell** 里操作。

**修复**（管理员窗口执行）：

```powershell
icacls "D:\抖音电商" /grant "DESKTOP-OCN2MO6\9:(OI)(CI)M"
```

`(OI)(CI)` 让权限继承到新建子目录（不加则只能写根、建不了子目录）；`M` = Modify。

**备用命令**（账号名解析失败时，按 SID 授权给本机所有用户）：

```powershell
icacls "D:\抖音电商" /grant "*S-1-5-32-545:(OI)(CI)M"
```

**验证**：`icacls "D:\抖音电商"` 应多出 `DESKTOP-OCN2MO6\9:(OI)(CI)(M)` 一行。

---

### E-09 Windows venv 能跟着目录搬移，但 activate 脚本会失效

**现象**：项目从 C 盘搬到 D 盘后，`Scripts\python.exe` 直接调用**仍然可用**（实测 `import requests` 正常）。

**原因**：`pyvenv.cfg` 里的 `home` / `executable` 指向的是**基础 Python 安装位置**（不随项目移动），旧项目路径只出现在 `command =` 字段，那是**创建时的历史记录，运行时不使用**。

**但**：`Scripts\activate` / `activate.bat` / `activate.fish` 里的 `VIRTUAL_ENV` 仍是旧路径 → **激活后环境变量指向错误位置**。

**做法**：移动项目后**直接重建 venv**，比手改 5 处文件干净：

```powershell
$ENG = "D:\抖音电商\投广管理客户端\engine"
Remove-Item "$ENG\.venv" -Recurse -Force
python -m venv "$ENG\.venv"
& "$ENG\.venv\Scripts\python.exe" -m pip install -r "$ENG\requirements.txt"
```

---

## 十、当前依赖版本（已实测可跑）

**engine/（Python 3.13.15 + venv）**

- `requests` 2.34.2

**desktop/（Node 24.14.1 / npm 11.11.0）**

| 包 | 版本 |
|---|---|
| electron | 44.7.0 |
| electron-vite | 5.0.0 |
| electron-builder | 26.15.3 |
| vite | 7.3.7 |
| @vitejs/plugin-vue | 6.0.9 |
| typescript | 5.9.3 |
| vue | 3.5.43 |
| vue-tsc | 3.3.12 |
| @electron-toolkit/preload | 3.0.2 |
| @electron-toolkit/utils | 4.0.0 |

**外部工具**

- `ffmpeg` / `ffprobe` 9.0（`C:\Users\9\AppData\Local\Microsoft\WinGet\Links\ffmpeg.EXE`）
- `yt-dlp` **未安装**（T-05 需要时再装）
- Git 2.53.0（`D:\software\Git`，未入 PATH）

---

## 十一、合规边界（硬约束，不可妥协）

1. **只处理用户自有、已获授权或内部素材**的视频
2. **不提供**批量爬取 / 站点遍历能力
3. **不绕过**付费墙、会员鉴权、DRM、登录态风控
4. **不实现** WAF JavaScript 挑战的绕过算法（见 T-02）
5. **不实现**验证码破解
6. 下载失败（风控 / 链接失效）**如实报错**，不做对抗性重试
7. 前端提交栏与 README 需明确提示版权责任归属提交者

**关于登录功能的合规说明**：Step 5 的扫码登录是**用户登录自己的账号**，属于正常使用，不涉及凭据窃取。但由此获得的 cookie 属于敏感凭据，必须遵守：

- **不写日志、不落盘明文、不外传**
- 界面上只显示 cookie **名字**，不显示值
- 导出通道返回值只在内存流转
- **B+（2026-10-10）**：Electron persist 分区本身明文落 cookie 是平台行为（无法修复，T-16 实证），因此登录成功后把目标域 cookie 用 `safeStorage`（Windows DPAPI）加密成 `D:\software\投广客户端\data\login_credential.enc`（v10 密文），登出即删——业务链路（导出/显示）不依赖 Chromium 明文库

**关于第三方工具与法律责任（2026-10-14 用户声明，已记录）**：用户提出「商品ID 下载视频」可用其提供的第三方工具（抖川工具等）完成，并声明**甲方负担由此产生的法律风险**。处理原则：①评估/集成第三方工具前必须先拿到工具实物并核实其能力（当前项目内无抖川工具文件，仅 TODO 有引用）；②集成方式优先「调用第三方工具产出的结果」（如榜单直链/视频清单），**不逆向其签名逻辑、不做对抗性反爬**（本条硬约束不因风险承担声明而解除）；③任何依赖第三方工具的方案在执行前需把风险承担写入记录并经甲方确认。

**抖川工具静态评估结论（2026-10-14 评估完成，用户裁定暂不使用，存档备查）**：收到实物 = 解压版 Chrome/Edge 扩展（`D:\微信聊天\xwechat_files\…\2026-10\抖川工具`）。可静态确认的：①manifest 注入覆盖**电商罗盘全部榜单页**（video/product/talent/live rank + 详情）、百应选品/灵感热卖视频、快手小店榜单，带 `downloads` 权限；②结构 = 密卡授权壳（明文，`verifyKey` 对任意非空卡密恒通过、有效期至 2100 年）+ XHR 拦截器（MAIN world）；③随附「卡密生成器.html」（含硬编码 SECRET）→ 属**破解/翻版分发**；④功能代码整体混淆（hex 字符串表），下载/商品/视频字段藏于加密表，**静态无法证实「商品ID→视频下载」能力，需浏览器实测**；为不触碰逆向红线未深挖混淆层。裁定：**暂不使用抖川工具，先等千川开放平台开发者凭证（T-13）**。

---

## 十二、测试产物待清理

以下目录/文件是开发期间的自测产物（下划线或临时目录开头，一眼可辨），**待用户确认后删除**：

```
D:\software\投广客户端\downloads\_zice_account\      Step 2 首次端到端测试
    └── 含 TEST_zice_*.mp4 (10.15MB) 与 .src.mp4 (5.66MB)

D:\software\投广客户端\downloads\_zice_对比\         水印版 / 无水印版对比测试
    └── 7549960424303037754\
        ├── 水印版_playwm.mp4   4.62MB  ← 左上角有抖音水印 + 3秒品牌片尾
        └── 无水印版_play.mp4   2.27MB  ← 干净

%TEMP%\wm_frames\                                    抽出的对比帧
    ├── A_zoom.png / B_zoom.png                      左上角放大对比（水印有无的铁证）
    ├── A_wm_1s.png / B_clean_1s.png
    └── C_wm_15s.png / D_clean_12s.png

%TEMP%\dy_fail.html                                  抓取的 WAF 失败页快照
%TEMP%\ev_test / ev_test2 / db_selftest.db           脚手架与数据层自测残留
%TEMP%\cookies_probe.db / ck.db                      Cookies 库取证副本
```

> **⚠️ 注意**：`_zice_account` 目录内还发现了 3 个**非本项目脚本生成**的文件（`抖音2026108-037754.mp4` 等），疑似手工下载，**删除前请先确认来源**。

**Electron 运行时目录**（可随时整体删除，删掉等于清空登录态和缓存）：

```
C:\Users\9\AppData\Roaming\touguang-client\
    └── Partitions\douyin-login\      ← 登录态在这里
```

---

## 十三、已完成事项（供对照，避免重复劳动）

### Step 1 — `douyin_parse.py` 分享链接解析 ✅

- 短链跳转 → `aweme_id` 提取
- iPhone Safari UA 请求分享页
- 字符串感知的括号配对，抠出 `window._ROUTER_DATA`
- `loaderData` → `video_(id)/page` → `videoInfoRes.item_list[0]` 定位
- **`playwm → play` 换源去水印**（已用抽帧放大对比**确证有效**）
- `--verify` 实测地址是否为真 MP4（查 `ftyp` 魔数）
- `--save-html` 落盘 HTML 便于与 F12 对照
- **自动重试**（默认 8 次，`--max-attempts` 可调，应对 SSR 随机降级）
- **WAF 检测 + 命中即停**（不继续加重封锁）
- 兼容 `download.py` 的 `Namespace`（`getattr` 兜底）
- 实测成功案例：`7693062724168249969`（标题/作者/无水印地址全部正确）

### Step 2 — `download.py` 下载与规范化 ✅

- 按规格命名与落盘：`{商品ID}_{视频ID}_{序号}_{时间戳}.mp4`
- 目录结构：`{根目录}/{账号}/{日期}/{商品ID}/`
- 下载进度（流式 + 原地刷新）、失败重试（退避实测 1s→2s→5s，总耗时 8.5s）
- **非 MP4 拦截**（查 `ftyp` 魔数 + 空文件 + 过小文件）
- FFmpeg 转码为 MP4 + H.264 + AAC 128k
- **分辨率上限按方向处理**（见 §8.4）
- 裁剪（原图像素空间，先裁后缩放；奇数取偶；非法字符消毒）
- `--check-quality`：SSIM / PSNR 量化（已对源套用相同几何变换，只反映编码损失）
- 转码进度按**时间**显示（修了自己一个把「秒」当「字节」的显示 bug）
- 实测：1080x1920 合成视频 → 裁剪 1080x1800 → SSIM 0.99711 / PSNR 47.09dB
- 实测真实链接端到端：下载 5.66MB / 1.84s → 成品 10.15MB，音轨保留

### Step 3 — `db.py` SQLite 数据层 ✅（自测部分完成，见 T-06）

- 四张表：`accounts` → `products` → `videos` → `tasks`
- WAL 日志模式、`PRAGMA foreign_keys` 每连接开启
- 索引 5 个；`PRAGMA user_version` 记录表结构版本
- 命令：`init` / `account-add` / `account-list` / `product-add` / `product-list` / `next-seq` / `video-add` / `video-check` / `video-list` / `stats` / `sql`
- 查重：按 `video_id` 全局唯一
- `sql` 逃生舱**只放行 SELECT / PRAGMA**（写操作走专用命令）
- 统一 JSON 输出，供 Electron 消费

### Step 4 — `desktop/` Electron 空壳 ✅

- 主进程 / 预加载 / 渲染进程三件套，`electron-vite` 统一构建
- 窗口标题「投广管理客户端」（`page-title-updated` 锁定）、尺寸 **1280x800**（API 实测确认）
- 四区布局：顶部菜单（百应工作台 / 商品查询）/ 左侧导航（投放看板 / 素材与工具 / 千川管理）/ 中部工作区 / 底部状态栏
- 全中文界面，**零 UI 库依赖**
- `npm run typecheck` / `npm run build` / `npm run dev` 全部实测通过

### Step 5 — 内置浏览器视图 + 登录态持久化 ✅

- **`WebContentsView`**（不是 `<webview>` / 不是已废弃的 `BrowserView`），内嵌到主窗口工作区
- 持久化：`partition: 'persist:douyin-login'`，实测目录已落盘
- **加密取证（前半）**：`os_crypt.encrypted_key` 解码以 **`DPAPI`** 开头；cookies 表有 `encrypted_value BLOB` 列
- 「检查登录状态」按钮 + **扫码后自动刷新徽章**
- 登出（只清抖音/百应相关域）
- **登录态导出给 Python**：走 stdin，实测 4 种情况（正常/空 cookie/非法 JSON/空 stdin）全部正确
- cookie 值自动打码，界面与日志都不出现明文
- 视图位置同步（`ResizeObserver` + `getBoundingClientRect` → IPC）
- 实测：TS typecheck + build 通过；App 启动无报错；窗口标题正确

---

## 十四、变更日志

| 日期 | 内容 |
|---|---|
| 2026-10-08 | Step 1 `douyin_parse.py` 完成；实测 `playwm→play` 有效；发现抖音三种失败模式；加自动重试 + WAF 检测 |
| 2026-10-08 | Step 2 `download.py` 完成；端到端实测；发现水印在**左上角**（推翻我的猜测） |
| 2026-10-08 | Step 3 `db.py` 完成；自测中断于测试脚本 bug（T-06） |
| 2026-10-08 | Step 4 `desktop/` Electron 空壳完成；踩 E-01 / E-02 两个环境坑 |
| 2026-10-08 | Step 5 登录态管理完成；新增 T-15~T-18；本文档改为自包含交接文档 |
| 2026-10-08 | 项目从 C 盘工作区整体搬移到 D:\抖音电商\投广管理客户端；重建 venv；文档路径全量更新 |
| 2026-10-08 | **Step 1–5 全量验收（纯本地，未联网）**：引擎 3 脚本 --help 正常；db.py 24 项全过（含按 video_id 全局查重、序号自增、自动建商品、写操作拦截、外键级联、索引 5 个）；session_bridge 5 种输入情况正确；desktop typecheck/build 通过；窗口实测标题「投广管理客户端」、外框 1280x800、四区布局齐全无乱码。受 WAF 封锁影响，解析/下载等联网项按 T-03 推迟；`--crop-bottom` 错误文本（T-04）确认仍在，本次未改 |
| 2026-10-08 | **T-11 核心完成**：新增 `main/engine.ts`（子进程管家：NDJSON 事件流 / 按进程树取消 / 中文错误美化 / 脚本白名单）；`engine/ping.py`（连通性自检）、`engine/demo_task.py`（进度·取消协议模板）；preload 暴露 `engine.*`；WorkArea 右侧新增「Python 引擎」控制台（连通性按钮 / 进度条 / 事件列表 / 取消按钮）。UI 实测三项全过（连通性 ✅ 进度 JSON ✅ 取消 ✅）。调试中修复 2 个 bug：result 行未存入终态；result 载荷字段归一。真实抓取任务接入 UI 待 §8.2 UA 决策与 WAF 解除 |
| 2026-10-10 | **T-16 完成（B+ 方案）**：实证推翻「persist 分区 cookie 会加密」的假设——Electron 平台行为明文落盘，注入 `os_crypt` key 无效（Chromium 只读 userData 根 Local State）。改为应用层 safeStorage 加密凭证副本（`D:\software\投广客户端\data\login_credential.enc`，v10 密文验证通过）；登录自动保存、登出删除；UI 状态行新增「凭证副本」。登录链路全套验证通过（扫码→凭证命中→导出打码→加密备份） |
| 2026-10-10 | **商品查询主界面完成**（对齐参考软件核心交互）：`engine/product_query.py`（mock 可控数据源，40 条假视频 0~40 天分布）；`main/app-dirs.ts`（下载目录选择/持久化 `dir:get/chooseDownloadDir`）；`ProductQueryView.vue`（搜索/时间范围 1~30 天本地过滤/优质开关/条数/商品卡/10 列勾选表格/底部操作条含数量·预览·占位下载按钮）；WorkArea 按顶部菜单切换视图。UI 实测 6 项全过（搜索秒回/筛选联动 7天=9→30天=20→优质=16/勾选数量/预览/下载占位/目录选择器）。调试修复 2 个 bug：① `queryProduct` 竞态——先 run 后订阅导致秒回事件丢失，改先订阅再启动；② `engine.ts` result 转发双重嵌套（已带 data 又包一层），改归一化直接透传。下载类按钮为占位（真实链路待 WAF 解除 + T-11 接入） |
| 2026-10-12 | **批量下载任务队列实现**：`engine/download_queue.py`（线程池并发 --workers 默认 2 / 指数退避重试 1s→2s→4s 最多 --max-retries / 任务间随机间隔限速 / 断点续传 / SQLite tasks 表持久化 / SIGTERM 取消 / recover 重启恢复）；ProductQueryView 新增队列面板（并发·重试可配/任务状态·进度·重试/下载全部/取消全部/历史经 db.py sql 读取）。修复 3 bug：SQLite 跨线程、兜底建表外键、**engine.ts 多行 JSON 解析**（buffered 累积，兼容 db.py indent=2） |
| 2026-10-12 | **批量下载队列验收通过（三项标准）**：勾选 10 视频 → 下载全部 10/10 成功（文件落盘 `_queue_test\`）；停服务模拟断网 → 重试可见（退避 1s/2s/4s + 连接拒绝详情）最终失败；下载进行中强杀进程 → SQLite 残留 running → recover 自动转 failed「进程中断，任务未完成」，应用重启后历史区块恢复显示 |
| 2026-10-12 | **本地素材库完成**（「素材与工具」导航）：`engine/asset_library.py`（建表 assets/asset_tags，扫描下载目录解析 账号/日期/商品ID/视频ID、file_path 唯一 upsert、增量清理失效、ffmpeg 抽帧缩略图缓存 {root}/.thumbs/{hash}_{mtime}.jpg、list 商品ID/标签/日期/状态 检索、delete 防误删校验、tag-set）；`main/assets.ts`（getThumb base64 / openFolder）；`AssetLibraryView.vue`（检索栏/缩略图网格/分类标签徽章/重新扫描/二次确认删除/打开所在文件夹）。CLI+UI 验证全过：18 素材登记、7 真缩略图、四维检索各剩 1 张、资源管理器打开并选中、删除确认取消/确定、重扫不回找。修复 2 bug：sqlite3.Row 未设置、日期过滤 Windows 反斜杠路径。**待办**：底部状态栏「素材库记录数」未联动（App.vue 静态 0） |
| 2026-10-12 | **修复测试数据**（用户反馈「测试文件不能播放」）：批量下载验收时慢速测试服务返回 5MB 随机字节冒充 mp4 → `_queue_test\` 8 个 + `测试账号\…\vidA` 不可播放。已删除坏文件，vidA 替换为真视频（13.17s）。重扫后素材库 8 个素材全部可播放、全部有缩略图 |
| 2026-10-12 | **真实下载链路打通（WAF 封锁解除）**：用户提供 2 条真实抽纸视频分享链接 → `douyin_parse.py` 解析成功（html_length 37947/38078 正常页、无 WAF 挑战、attempt=1，**10-08 封锁已衰减解除，T-03 完成**）→ `download.py` 去水印（playwm→play）下载 4.8MB + 转码 MP4/H.264/AAC128k → 按规范落盘 `downloads\柚子啊\2026-10-12\7379395023726562595\…`（7.8MB/20s）与 `downloads\想想爱睡觉\2026-10-12\7517594097176579337\…`（12.6MB/36.6s），ffprobe 确认可播放 → 素材库重扫自动登记（11 素材全有缩略图）。**T-15 部分解决**：百应域名迁移实测 `buyin.jinritemai.com` → `buyin.douyinec.com`（根路径 404、/daren 落地页「立即入驻」仅拉起 App、工作台真实路径未知），LOGIN_TARGETS 已更新为新域名+三域 cookies；工作台 URL 仍待用户提供。T-05 维持（实测仍 720p 单候选） |
| 2026-10-12 | **百应真实接入验证（L2 抓包成功）**：用户提供真实入口 `buyin.jinritemai.com/dashboard?universal_page_params_id=…`（已配 LOGIN_TARGETS）；扫码登录百应成功（jinritemai.com 域）；确认选品路径（选品→选品中心→选品广场，product_id 19 位数字）；**关键突破**：CDP 探针（`desktop/src/main/probe.ts`，挂载于 login.ts ensureView，日志 `%TEMP%\baiying_probe.jsonl`）抓到商品详情接口 `pack_detail`，响应含 `media.video_url`（真实 mp4 直链）——实测下载 7MB 可播放。接口带 a_bogus/msToken 签名（引擎不可直接构造，合规不逆向）→ **采用「探针方案」**：用户打开商品详情时探针解析 pack_detail 缓存 `product_id→video_url` 至 SQLite，供商品查询/下载队列使用（待实现）。同时确认百应「带货内容」视频是 canvas 精选片段（UI 无可下载地址，DevTools 抓包无 aweme_id） |
| 2026-10-12 | **T-15 批量抓取实测修复（7 轮二分定位根因）**：批量驱动应用内实测连续失败，诊断（CDP 页面状态 dump）确认「商品决策页」壳渲染正常但 pack_detail 从不发起。根因：**详情路由直接 loadURL 时，pack_detail 只会在 URL 携带足够上下文参数时被前端触发**（真实用户从选品广场点卡片进入时携带 30+ 参数）。逐轮二分确定**最小必需集**：动态参数 `commodity_id`（推广ID）+ `product_id`；固定参数 `commodity_location=1`、`id(=commodity_id)`、`decision_enter_from=pc.selection_square.recommend_main`、`only_query_params=1`、`universal_append=1`、`pre_universal_page_params_id=87e0475e…`（已验证跨会话复用）。已验证可去掉：shop_id、pick_*_source、btm_*、pre_universal_unit_params、session 追踪参数（search_id/session_id/log_pb）。同时验证了批量期间**登录视图需挂到窗口给真实视口**（0×0 视图下详情组件可能不渲染）。生产版（输入格式 `商品ID:commodityID`，缺 commodity_id 时明确报错）实测通过（成功 1/0），诊断代码全部移除。**遗留**：用户只提供 product_id 时需 commodity_id，下一步做探针解析选品列表接口自动缓存 `product_id→commodity_id` 映射 |
| 2026-10-12 | **T-15 全链路打通（commodity_id 自动补全 + 真实下载队列）**：①`baiying_cache.py` 增加 `commodity_id` 列（老库 ALTER 迁移）+ `set-commodity` 命令；②探针新增选品广场列表/搜索接口解析，递归提取 `product_id→promotion_id` 映射自动落库（防抖 5s）——实测浏览选品广场自动补全 8 个商品映射；③批量驱动缺 commodity_id 时先从缓存补全——实测**只提供 product_id** 批量抓取成功（商品 3562110391138020429 从「无视频」变「有视频直链」，标题《病隙碎笔》佣金 20%）；④商品查询界面新增「百应缓存商品」面板，下载任务走现有 download_queue.py，实测下载成功并落盘，ffprobe 确认 1080x1920 可播放 |
| 2026-10-12 | **初版收尾三项**：①**商品查询接真实数据（替换 mock）**——ProductQueryView 搜索改为 `baiying_cache.py get`，命中显示真实商品卡（标题/佣金/抓取时间）+ 单条视频直链 + 「下载该视频」（走下载队列，dest=`百应素材\{日期}\{商品ID}\`）；移除 mock 表格/筛选/假数据标注，实测：搜索瑞幸→真实数据→下载成功（typecheck 过）。②**状态栏素材记录数联动**——App.vue 加载时 + 监听 `asset-library-changed` 事件，用 `db.py sql SELECT COUNT(*) FROM assets` 刷新「素材库读取数」；AssetLibraryView 扫描/删除完成后广播事件。③**T-04 完成**——download.py 裁剪提示改为 `--crop-top/--crop-left`（实测水印在左上角），help 与 docstring 示例同步修正 |
| 2026-10-12 | **带货视频加密边界实证（重要结论）**：用户需求「1 商品 ID → 该商品全部带货视频（带货内容→视频区，几十条）下载去水印」。多次实测（L2 + 放宽探针诊断 2 轮）：百应「带货内容→视频」的达人带货视频**全程 canvas 加密预览，不暴露真实视频地址**——接口响应无 `aweme_id`、无 `video_url`、无 `url_list`；点「视频详情」按钮也不暴露。结论：**平台对带货视频加密是有意策略，任何前端/自动化工具都拿不到，属公共边界；与客户端是否独立部署无关**。可行替代：①商品主图视频源（pack_detail video_url，已验证可下载无水印）——百应唯一放开的数据，现有链路已全自动化；②抖音分享链接批量解析去水印（douyin_parse playwm→play 已验证）——需用户提供分享链接。临时诊断代码已从 probe.ts 移除 |
| 2026-10-12 | **小红书/头条接入 + 队列转码完善**：①小红书 extractor 实现并实测通过——需登录态（客户端登录面板新增「小红书」目标 + web_session 凭证判定；extractor 带 cookie 请求；未登录提示明确错误）；SSR 解析踩坑两处：`__INITIAL_STATE__` 需括号配对提取（跳过字符串）、SSR 含 JS 字面量（undefined/NaN/!0/!1/new Map([])/new Set/Date/RegExp 需预处理为 JSON）；实测串珠视频解析成功、下载 21MB 可播放。②头条 extractor 框架已写（SSR 数据提取 videoUrl，待真实链接实测）。③下载队列新增 `ensure_h264` 自动转码（非 h264/aac 才转，实测 hevc→h264 720x1280 成功；抖音 h264 源不重编码）。④通用批量命名优化：link_batch 返回 `video_id`（note_id/aweme_id），落盘用视频ID而非 lk-N。⑤UI「通用链接批量解析下载」切 link_batch.py（自动带小红书 cookie） |
| 2026-10-12 | **头条边界确认（直链可用、页面链接待 Playwright）**：头条/西瓜视频**页面链接**被字节 JS 防护（`_$jsvmprt`，与抖音 WAF 同源）拦截，纯 requests 拿不到 SSR 数据（用户实测 2 条页面链接均失败，属平台防护非 bug）。头条**视频直链**（`toutiaovod.com` + `lr=unwatermarked` 无水印源）extractor 已支持直接返回下载。页面解析与抖音 WAF 共建进 **T-02 Playwright 方案**（真浏览器可过 JS 防护），当前不单独处理。extractor 提示文案已改为教用户 F12 复制 vod 直链 |
| 2026-10-12 | **头条链路修复（根因定位 + 上线容错）**：现场日志定位两个 bug：①`detect_platform` 漏 `toutiaovod.com`/`xhscdn.com`/`zjcdn.com` 视频 CDN 域名 → 粘贴 vod 直链被判「未知平台」失败；②`toutiao._pick_link` 贪婪正则误抓分享文本里夹带的 google analytics 追踪 URL（g/collect）→ 请求发到 google 超时。修复：平台识别规则补三类 CDN；`_pick_link` 过滤只认头条系域名；UI 检测 `toutiao.com/video/` 页面链接时引导点「导入头条直链」。验证：探针捕获的 vod 直链 → extractor 识别 → 下载成功（1280x720 h264+aac 30.1s 可播放，头条直链天然 h264 符合规格）。**上线容错**：三类入口全覆盖（直链/页面链接提示/分享文本夹追踪链接） |
| 2026-10-12 | **T-13 千川官方 API 评估落地（闲置待凭证）**：甲方确认有千川投放账号（advertiser_id 16761573861161，千川工作台 qianchuan.jinritemai.com）；可行路径 = 巨量千川开放平台 `qianchuan/file/video/aweme/get/`（商品 ID 过滤拉带货视频含 url，身份要求「自研投放应用-客户」）；已产出《千川开放平台接入操作单》（docs/千川开放平台接入操作单.md）转甲方，待 app_id/secret/aweme_id/Access-Token 到位后实现接入（当前闲置） |
| 2026-10-14 | **罗盘登录跳转入口完成（范围=roadmap 步骤①，②~⑥ 暂缓）**：`LOGIN_TARGETS` 新增「罗盘」目标（compass.jinritemai.com）并给每个目标加 `partition` 字段——罗盘独立分区 `persist:compass-login` 防 jinritemai.com 域串号，其余共用 `persist:douyin-login`；login.ts 改为按目标分区（切分区销毁重建视图；checkLoginState/exportForEngine/登出/凭证备份按目标各自分区取 cookie；ensureLoginView 固定绑百应目标保批量链路不受影响）。面板下拉自动出现「罗盘」，点击跳转罗盘页面扫码登录即可浏览。typecheck 通过，**用户实测验收通过（2026-10-14）**；建表归属决策已记录（compass_cache.py 自建表） |
| 2026-10-14 | **抖川工具静态评估完成（用户裁定暂不使用）**：收到用户提供的解压版浏览器扩展实物（微信文件目录），确认其覆盖罗盘全榜单页/百应选品/快手小店、带 downloads 权限、属密卡破解分发（卡密生成器+verifyKey 恒真）、功能代码整体混淆（静态无法证实商品ID→视频下载能力，未深挖混淆层以免触碰逆向红线）。裁定暂不使用，先等千川开放平台开发者凭证（T-13，甲方申请中） |
| 2026-10-14 | **下载列表拆分已回退（用户裁定：不要下载列表，恢复下载功能；原始单队列基线恢复，实测下载成功）**：双列表「商品ID下载/链接解析下载」迭代三轮未收敛，用户裁定不需要双列表。已把 ProductQueryView.vue 的队列状态机/取消/事件绑定/四个下载入口/模板全部**回退到改造前单队列版本**（queueTasks + queueEngineId + queueRunning + queueSummary，曾通过 S7 下载队列验收 10/10），typecheck 通过，**用户实测下载成功**。**结论存档**：双列表方案在渲染层用状态机（queueBusy/queueEngines/异步 .then 写 taskId + 引擎事件路由）存在架构级竞态——引擎事件可能在 taskId 写入前到达导致事件被丢弃、占用永久卡死；后续若重做必须改为 **IPC 层多任务路由**（主进程按 taskId 分发，渲染层只渲染），而非渲染层状态机。已知遗留（原始版本既有）：链接解析下载在下载队列已运行时会被 `queueRunning` 守卫静默跳过（入队失败），如需改善应放后续会话以正确架构处理 |
| 2026-10-14 | **下载提速修复（真实根因=请求头缺浏览器 UA）**：用户两次反馈「下载慢/比最开始慢多了」。排查结论：队列引擎 `download_queue.py` 用 requests **默认 UA（python-requests/2.34）**，而最初全速的 `download.py` 链路走 `douyin_parse.build_headers`（**iPhone UA + Referer**）——抖音 CDN 对非浏览器 UA 通常降速限流。修复：①引擎补 `DOWNLOAD_HEADERS`（iPhone UA + Accept + Referer，与 download.py 逐字一致）；②8KB 分块/每块进度上报维持原基线不动（探索过 256KB+节流，无收益且避免引入新变量已回退）。py_compile 通过。**追记（2026-10-14）：用户实测提速仍未奏效，UA 假设不成立**（后续用户反馈「开了 VPN/未开 VPN」且浏览器也慢，判定为**网络环境/CDN 链路问题**而非代码瓶颈，不再改码排查），下载速度问题结案为网络原因 |	
| 2026-10-14 | **代码上传 GitHub 完成（用户要求）**：项目首次建仓（git 在 `D:\software\Git` 全路径调用）；`.gitignore` 排除 node_modules/.venv/out/*.enc/*.db 等；初始提交 `02add23` → 远端 `https://github.com/hy112429/-2-.git`（建仓自带 README，`--allow-unrelated-histories` 合并）→ `7c017cb..f0afc87 main`，`ls-remote` 复核远端/本地一致（f0afc87）。踩坑：①未开 VPN 时 GitHub 直连间歇可达（探测 HTTP200 后连续超时），用**循环重试脚本**抓到可达窗口一次成功；②PS5 读 UTF-8 中文路径脚本会乱码报错，脚本须 ASCII。**去水印候选方案裁定（同日）**：ProPainter 需 GPU → 跳过；multi-delogo 空间插值不适配 → 不采用；去水印维持搁置，最终路线=商业源无水印直链 |
| 2026-10-14 | **外站链接一律系统浏览器打开 —— 完整规则矩阵（用户两轮确认）**：①内置登录视图（login.ts）与账号分区视图（account_login.ts）内 `target=_blank` → `shell.openExternal` 外开；`will-navigate` 跳平台域白名单（douyin/jinritemai/douyinec/oceanengine/xiaohongshu/xhslink/toutiao/ixigua/toutiaovod/kwaixiaodian/amemv）之外 → 拦截 + 外开；②账号管理「精选页」由内置视图改为系统浏览器外开；③**保留内置（登录类，用户确认）**：登录面板「打开登录页」、账号「打开浏览器」、千川「登录千川」、罗盘/小红书/头条登录、百应/头条批量（探针抓包）、拉取信息——这些依赖分区登录态/探针，外开会失效；④主窗口 UI `_blank` 本已外开（index.ts）；⑤「投放看板/千川管理」导航仍为占位视图，等待看板 URL 后挂外开。改动：login.ts、account_login.ts。typecheck 通过 |
| 2026-10-14 | **会话开场提示词落盘 `docs/提示词手册.md`（用户要求：每个新会话都要调用）**：将《AI开发提示词手册》（AppData 侧 v2.2）的「开场 Prompt」与「二期复用约定」两段抽到项目文档独立成册，并在 TODO §0.2 关键路径登记「**每个新会话先调用**」，保证新会话默认加载 |
| 2026-10-14 | **一期收口（用户要求补齐）**：①**T-06 完成**——db.py 自测 7~23 项 **14/14 全过**（临时库隔离）；②**S1–S8 验收清单回填**——按既有实测逐项打 ✅/⏳；③**T-05 复测结论**——匿名分享页仍只暴露 720p 单候选，1080p 缺口属实，待登录态详情接口（T-11 收尾）或 yt-dlp 再验；④**外站外开方案 A 决策**——平台域内链接留登录视图，真外站/新窗口链接系统浏览器外开（已生效），SPA 路由不再深挖；⑤**S7 #27/#29 复测 PASS**——限速间隔 2.38/1.27s 随机、taskkill 进程树终止+recover 转 failed |
| 2026-10-15 | **T-10 打包完成（用户要求：给甲方看下载软件；已确认贴链接下载三平台成熟后直接打包）**：electron-builder 配置（`desktop/electron-builder.yml`：appId/productName、asar、NSIS+zip 双目标、extraResources 内置 engine）；**体积精简**（关键：ffmpeg full 版 424MB→essentials 137MB，gyan.dev 直连太慢改用 npmmirror 二进制镜像 `/-/binary/ffmpeg-static/b6.0/ffmpeg-win32-x64` 下载 ffmpeg 6.0 + npm pack `ffprobe-static@3.1.0` 提取 exe；venv 178MB→14MB 重建只装 requests，剔除已搁置去水印的 opencv/numpy 残留）；引擎三处 ffmpeg/ffprobe 定位加「随包 `<engine>/bin/<name>.exe`」候选（download.py `resolve_tool`、download_queue.py `_ffprobe()`、asset_library.py `resolve_ffmpeg`），开发态回落原逻辑；生成应用图标 build/icon.png 自动转 ico。**产物**：`desktop/release/投广管理客户端 Setup 0.0.1.exe`（145.2MB 安装包）+ `投广管理客户端-0.0.1-win.zip`（198.2MB 便携）。**冒烟测试 PASS**：win-unpacked 启动 4 进程存活、打包内 venv ping.py ok:true（requests 2.34.2）、resources/engine/{.venv,bin,17 脚本} 齐备。**待用户进行装机验证**（安装→登录→实际解析下载一条）；打包目标产物由用户转发甲方。**2026-10-15 追记（装机暴露遗漏）**：用户安装后报「ModuleNotFoundError: No module named 'extractors'」——首版打包只复制 `engine/*.py`，**漏了本地子包 `engine/extractors/`（douyin/toutiao/xiaohongshu 三平台解析器）**，非第三方依赖缺失。已补复制 subpackage → 重新打包 + 冒烟验证（打包 venv 内 `from extractors import build_registry` 成功，platforms=douyin,toutiao,xiaohongshu）。**教训：打包 engine 时须整体复制 engine/ 下全部子目录（extractors 等），不能只收 *.py**；用户需重装新包（Setup 0.0.1.exe 19:51 版本） |
| 2026-10-15 | **头条/小红书批量下载测试首轮（60% 成功率）与修复**：用户提供 10 小红书 + 10 头条链接，分批测试首轮 8 条落库 5 成功 3 失败。**失败根因**：①头条 49/50——探针捕获的 vod 直链是**音频流/字节范围分片**（`media-audio-und-mp4a` + `br=49&bt=49`），非完整视频直链，实测下载 404；②小红书 48——带时效签名直链（`sns-video-v2.xhscdn.com`+sign&t）解析后排队过久**签名过期**，实测 403。**修复**：①`probe.ts` 头条捕获过滤——只收 `media-video` 视频流，排除 `media-audio` 与 `br=/bt=` 字节分片；②`download_queue.py` 403+`xhscdn.com` 时中文提示「直链签名已过期（403）：请重新解析该链接」。typecheck + py_compile 通过，待重跑验证。**2026-10-15 追加（重跑实测后）**：新中文提示已生效（小红书 `68f613e1…` 显示签名过期提示、重复下载跳过正常）；但 403 过期签名**重试 3 次徒劳**（同一死 URL 白等退避），已加 `NoRetryError`——签名过期直接判失败不重试。**2026-10-15 再追加（用户重跑确认）**：修复后**小红书重跑成功**；用户评价整体「**不太稳定，后续真实上线还需要测试**」——已知不稳定点：①小红书直链签名时效短（解析→下载间隔过久即 403）；②头条依赖探针抓页面直链（加载行为变动可能漏抓/抓错，音频流分片已过滤，待头条批次重测确认）；③登录态失效/过期链路上未覆盖。**上线前需更大样本（小红书/头条各 ≥20 条）回归测试** |
| 2026-10-16 | **T-11 收尾：真实任务接入 UI（完成）**：新增 `LinkToolView.vue` 单条链接解析下载工具并挂到「百应工作台」默认视图右面板（替换原 EngineConsole 演示控制台，后者已删除）——①解析：`link_parser.py` 统一入口（抖音/头条/小红书自动识别，8 次重试），小红书登录 cookie 经 **stdin** 传入（`link_parser.py` 新增 stdin 可选 cookie，与 `link_batch.py` 同约定，登录态不经过进程命令行）；②结果卡：平台/标题/作者/时长/无水印地址（一键复制）；③下载：复用 `download_queue.py` 单任务（并发=1、重试=3），落盘 `{下载目录}\抖音素材\{日期}\{vid}.mp4`，进度条/重试次数/完整路径/📁 打开所在文件夹；④保留引擎连通性测试。验证：py_compile 过、typecheck（node+web）过、stdin cookie 路径离线实测正常（未知平台错误正确返回）。**遗留**：抖音解析为匿名 iPhone UA 路径（§8.2 取舍由实践定案），1080p 缺口仍属 T-05；单条工具与商品查询页批量下载共享同一队列引擎、互不冲突 |
| 2026-10-16 | **小红书水印结论确诊（用户实测反馈「keketoys 视频水印没去掉」→ 诊断闭环）**：从下载任务表拿到 lk-1.mp4 的流 URL（`sns-video-v6.xhscdn.com/stream/1/110/258/..._258.mp4`），用分区 cookie 副本抓该笔记（`/discovery/item/6ac4c579…`）SSR，`media.stream.h264[0].masterUrl` 与任务表 URL **md5 前缀一致**、流元数据 size=3781120 与文件**精确匹配**（720x1280/15s）→ **lk-1.mp4 就是小红书官方主视频流（hd_258，与昨日干净无水视频同一 110/258 路径模式），画面里的「keketoys」白色手写水印是博主烧录在素材里的防盗用内容水印，CDN 无无水印版本，解析/下载层无法去除**（画面修复属已搁置的去水印范畴）。**顺带确诊两类小红书水印**：①平台水印「小红书」三字 = 烧录在预览流（stream_type 19，路径 `1/10/19/..._19.mp4`）上，主视频流（hd_258）不带；②博主内容水印 = 素材自带，无解。**修复（同日）**：`_extract_note_id` 兼容 `/discovery/item/{id}`（原只匹配 `/explore/`，导致该路径笔记文件名退化为 lk-1）——py_compile 过、带 cookie 实测返回 `note_id=6ac4c579000000001b021d5e` 且 no_watermark_url 正常。**alpha 反推实验（同日，用户选择试方案后裁定接受现状）**：delogo（ffmpeg 空间插值）实测不合格（半透明白字残留+伪影）；alpha 反推（bg=(pixel−a·255)/(1−a)，mask 限定白色文字像素，a 0.55~0.92 五档）文字从清晰降为「依稀可见幽灵轮廓」，a=0.75 无伪影但残留、a=0.92 最淡但有纹理损伤——**接近不透明的白字（a≈0.9+）使分母 (1−a)→0，alpha 误差与噪声被放大，是原理性极限**，与既往 9 轮去水印诊断结论一致。**裁定**：博主内容水印无低成本自动去除方案，接受现状；临时安装的 numpy/opencv-headless 已卸载，venv 恢复精简。**遗留**：title 提取对 `/discovery/item/` 页面取不到（extractor 只查 `note.noteDataMap`，该页面结构为 `note.noteDetailMap`），待用户确认后一并修 |
| 2026-10-16 | **T-05 调查完成（用户裁定规格=最高可得画质，T-05 关闭）**：三条路径实测收敛——①匿名分享页基线复测（`7302704301653101850`，attempt=2 成功）仍**只有 720p 单候选**；②yt-dlp 2026.8.19（临时安装后卸载）抖音 extractor 请求 `web/aweme/detail/` 被 **403**（需 `a_bogus`/`msToken` 签名，带登录 cookie 也 403）；③登录态详情接口同撞签名墙，构造签名属合规红线。**结论**：合规边界内抖音分享链接最高 720p；1080p 在商业源已实现（百应 pack_detail 实测 1080x1920 无水印）。规格口径定为「分享链接 ≤720p（合规边界），商业源可达 1080p」。venv 保持精简（仅 requests） |
| 2026-10-16 | **一期尾巴收尾（title 兼容 + 验收回填）**：①`xiaohongshu.py` title 提取兼容 `/discovery/item/`（该路径 SSR 结构为 `note.noteDetailMap`，原只查 `note.noteDataMap` 导致标题为空）——py_compile 过、带 cookie 实测返回 title=「这么酷的小野谁不爱呢！」且 note_id 正常；②**S10 验收回填**：#36 抖音四类 ✅、#37 小红书 ✅（含 /discovery/item/ 新验证）、#38 头条 ✅、#39 批量混合 ✅（分平台实测，混批大样本见下一步）、#40 停加载 ✅；③**S11 验收回填**：#41/42/43/47/48/49/50/51/52/53/55 ✅（机制/CLI/自动化验证 + 标注真实账号项），#44/45/46/54 ⏳（统计数值、计划发布率/卡优率/广告条数接入、批量生效实测、真实抖音号拉取信息——如实标注待补） |
| 2026-10-17 | **去水印新方案启动：Moonshine-Image（SLBR 检测 + LaMa 填充，待源码）**：用户提供《接入说明-视频水印去除.md》+ GitHub 仓库 `CuiMuxuan/Moonshine-Image`（用户已微调：检测+修复拆分、mask 连通域后处理、RGB/BGR 转换），技术路线=SLBR 自动检测水印 mask → 连通域后处理（thr=100/pad=15/面积 40~45%）→ LaMa 生成式填充 → 拼回保留音轨。**已完成**：①环境侦查——本机无 NVIDIA（nvidia-smi 不存在），显卡为 **Intel Arc A750 8GB** → CUDA torch 不可用，方案定为 **torch-directml（微软后端，Arc 可用）+ 独立 Python 3.11 venv**（torch-directml 不支持 Python 3.13，引擎 venv 3.13 保持精简不动）；②模型已从 hf-mirror 下载至 `D:\software\moonshine\models\`（slbr.pth.tar 85.8MB + big-lama.pt 205.7MB），**SHA256 双模型校验通过**（f3984bd7…/344c77bb…）；③GitHub 直连 5 次重试被重置（历史经验：需 VPN），**源码待用户提供微调版 server/ 目录**。**待办（源码到位后）**：建 `D:\software\moonshine\.venv`（Python 3.11 + torch-directml + numpy/opencv/scipy/scikit-image/Pillow/loguru）→ 适配 device（cuda→directml 自动回退 CPU）→ 新增引擎白名单脚本 `remove_watermark_ai.py` → 用 lk-1.mp4（keketoys 固定水印）实测效果与速度（RTX 4060 参考 0.43s/帧@640x360，Arc/CPU 待实测）→ 按接入说明第六节调参 |
| 2026-10-18 | **Moonshine 全链路跑通 + 纯 CPU 方案工程化（去水印功能复活）**：用户提供 `Moonshine-Image-main.zip` 源码（`d:\网页下载`）。**环境**：`D:\software\moonshine\.venv`（Python 3.13 + torch 2.14.1 CPU）+ `.venv311`（Python 3.11 + torch-directml 0.2.5.dev）；**适配**（Python 3.13 移除 imghdr → 注入魔数 shim；`_build_model_args` 加 gpu_device、`BasicModel` 改用传入设备、checkpoint 先 CPU 加载再 .to(device)、SLBR 前向 inference_mode→no_grad）；**DirectML 实测判定不可用**：Arc A750 上 conv2d 0.50s vs CPU 0.03s（torch-directml 0.2.5 算子效率差，0.2.6.dev 在 PyPI/官方 index 均无 Windows 新版）→ 弃用。**纯 CPU 方案确认（用户拍板）**：低分辨率检测（360 宽）+ 原分辨率局部填充 + 多进程并行。**单帧速度实测**：SLBR@360x640 ≈9s + LaMa 局部 ≈9s ≈ **18s/帧**（12 个框偏多拖慢 LaMa，后续可调 min_area）。**效果 PASS**：lk-1.mp4（keketoys）单帧与 1 秒全片片段均验证——水印完全消失、画面自然零误伤（5/5 清除、4.5/5 自然度）。**新增引擎脚本 `engine/remove_watermark_ai.py`**（白名单）：stdin {video_path, workers, out_path}，多进程分块（spawn）→ 每块 mp4v 片段 → ffmpeg concat + 合并音轨转 h264/aac → 输出 `{原名}_nowm.mp4`，NDJSON 进度；修复跨盘 os.replace→shutil.move（WinError 17）。**engine.ts 接入**：白名单加 remove_watermark_ai.py + `SCRIPT_PYTHON` 独立解释器映射（Moonshine venv，可用环境变量 MOONSHINE_PYTHON 覆盖），缺环境给可读中文错误。**UI 接入**：ProductQueryView 下载队列成功行新增「AI去水印」按钮（startAiWatermark，workers=2）+ 独立进度区（stage 事件/结果路径/📁 打开），旧半自动去水印保留不动。typecheck 通过。**速度预估**：4 进程 720p ≈4.5s/帧 → 15 秒视频约 25 分钟（后台任务）。**遗留**：①LaMa 框数偏多（SLBR 把背景文字当水印）——min_area 阈值可调，或后续加区域限制；②进度粒度已细化至每 10 帧；③待重跑完整验证（跨盘修复后）与用户实测 |

---

## 十五、下一步建议顺序

1. **T-04**（纯本地改动，不受封锁影响，最快能完成）
2. **T-15**（确认百应 URL，否则登录功能只能用于抖音主站）
3. **T-03**（封锁衰减后复测，验证 Step 1 修复是否真生效）
4. **T-02 + T-01**（规模化前提，建议一起做）
5. **T-11**（把引擎真正接进客户端 —— 注意先解决 §8.2 的 UA 取舍问题）
6. **T-06**（补完数据层验证）