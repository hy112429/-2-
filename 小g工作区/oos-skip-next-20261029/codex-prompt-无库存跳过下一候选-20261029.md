# Codex 开发卡：无库存跳过并继续下一候选 · 2026-10-29 Asia/Shanghai

你在 Codex UI 可视化 thread 中执行本卡。进度只进本 thread，**勿塞 Trae**。禁 push、打包、删除无关文件、登录验证码、扫码、实机百应狂点。

## 项目与路径
- 项目根（`-C`）：`D:\抖音电商\投广管理客户端`
- 工作区：`D:\software\投广客户端\小g工作区\`
- 本卡目录：`D:\software\投广客户端\小g工作区\oos-skip-next-20261029\`
- 证据目录（必须写出）：`D:\software\投广客户端\小g工作区\verify-oos-skip-next-20261029\`
- CLAIM：`D:\software\投广客户端\小g工作区\oos-skip-next-20261029\CLAIM.md`
- ACK：`D:\software\投广客户端\小g工作区\oos-skip-next-20261029\CLAIM-ACK-开发工程师.md`
- 基线 `engine\browser_cdp.py` SHA256（开工前再核，必须匹配）：
  `3C5218DE50C05FF8BF4C9D298B35682ABCCAFF7C7E5584A61A69C3533FBAC636`
- 若改了该文件，交付 sha **必须 ≠** 基线；若未改引擎，REPORT 写明并记录当前 sha。

## 一句话目标
核验百应商品 / 复制链接时，若遇「无库存」及同类不可加车态，或按商品 ID 查询后未能唯一识别目标行且证据为无库存/错品行 → **记 skip + 继续下一候选**，禁止把整任务/整批卡死为硬失败。日志必须写清商品 ID 与跳过原因。

## 用户证据锚点
- 任务#28；商品 ID `3589340486537708296`
- 现象：诊断/可见行样本含「无库存」及另一商品行；当前会卡死而非跳过
- 用户原话：「无库存就应该跳过选下一个 不应该直接卡死在这」
- 相关先例：P-2026-10-29-05（单品失败跳下一个）；本卡 P-2026-10-29-15

## 根因假设（请自行核实后修，可推翻）
1. `_locate_baiying_product_row` / 复制链接核验在「未识别到含目标 ID/标题的可见行」或「无法唯一匹配」时抛 `RuntimeError`。
2. `_selection_error_requires_pause` 把「身份无法唯一确认」「无法按商品 id 唯一定位」等一律标为暂停；分析/加车循环遇此类错误会 `raise` 安全停止整批。
3. 当页面可见行为「无库存」或错品行（ID/标题不匹配）时，应视为**该候选不可加车的业务 skip**，不是账号/限流/页面崩溃类 pause。

## 范围

### A. 引擎：无库存 / 错品 → skip（必做）
1. 在核验百应行 / 复制链接 / 定位商品行路径中识别：
   - 文案含「无库存」及同类不可加车（如缺货、已下架、暂不可带、售罄等——以页面真实文案为准，先覆盖「无库存」）
   - 查询后未能唯一识别目标行，且诊断样本/可见行证据指向无库存或错品（非目标 ID）
2. 对此类情况：
   - **不要**走 `_selection_error_requires_pause` → 整批暂停
   - 标记该候选 `skipped=True`，`cartAdded=False`，写入清晰 `status`/`thresholdReasons`：含 **商品 ID** + 跳过原因（例：无库存；无法唯一识别且样本为无库存/错品）
   - **continue** 下一候选；批次继续
3. 建议手法（任选核实后实现，勿大重构）：
   - 新增轻量异常类（如 `CandidateCartSkip`）或复用/扩展 `CandidateSourceRefreshSkip`，与 pause 路径分离
   - 或在 `_selection_error_requires_pause` / 分析循环的 pause 判定前，先检测 OOS/错品证据并转为 skip
4. 保留真正应 pause 的情形：11001/限流/滑块/登录/浏览器断开/真正身份冲突且**无**无库存/错品证据等。

### B. 日志与可观测（必做）
- 进度/候选结果/诊断中能看到：`productId` + 跳过原因
- 不要把整批 `stopReason` 设成因单个无库存而永久废掉

### C. 测试（必做）
1. 引擎单测：覆盖至少
   - 「无库存」→ skip 下一候选，不 raise 整批暂停
   - 无法唯一识别 + 样本含无库存/错品 → skip 非 hard pause
   - 真正需 pause 的身份异常（无 OOS 证据）仍 pause（防回归）
2. 建议落在 `engine/test_selection_recovery.py` 或新建窄测文件；用现有 unittest 风格
3. 验证命令：
   ```
   D:\抖音电商\投广管理客户端\engine\.venv\Scripts\python.exe -m py_compile engine\browser_cdp.py
   D:\抖音电商\投广管理客户端\engine\.venv\Scripts\python.exe -m unittest <你的测模块> -v
   ```
4. 若顺带改了 desktop 批次映射，补最小 node:test；否则可只跑引擎测。**禁止** pack。

### 不做（严禁跑偏）
- 挂车门槛、封面、错误文案卡范围（selection_error_ux）、push/打包/扫码
- 实机百应狂点 / 登录验证码
- 不要改 Trae；不要开新 thread
- 不要大范围无关重构

## 关键入口（开工前再读确认行号）
- `engine\browser_cdp.py`
  - `_locate_baiying_product_row`（无法唯一匹配 / 未识别可见行）
  - `_copy_baiying_product_link` / 核验复制链接
  - `_selection_error_requires_pause`（约 7639+）
  - 候选分析/加车循环中 `CandidateSourceRefreshSkip` 与 `must_pause` 分支（约 9242+、9466+）
- 既有测：`engine\test_selection_recovery.py`；`engine\test_browser_cdp_recovery.py`
- 先例：`docs\决策先例.md` P-2026-10-29-05 / P-2026-10-29-15

## 证据交付（缺一不可）
目录：`D:\software\投广客户端\小g工作区\verify-oos-skip-next-20261029\`

必须包含：
1. `REPORT.md` — 根因一句话；改动清单；`browser_cdp.py` sha（无论是否改）；做了/没做/风险；Codex thread id=`01a18082-51fe-7531-ac95-835d8b00a771` 与本 turn id；完成时间 Asia/Shanghai
2. `DELIVER.md` — 可转交摘要
3. `files.sha256.txt`；若改了 browser_cdp 另写 `browser_cdp.py.sha256`
4. `py_compile.log` + unittest 日志
5. 新增/改动测试文件副本
6. CLAIM 指针或副本

## 完成判定
- 无库存 / 错品证据 → skip + 下一项，整批不因该品卡死
- 日志含商品 ID + 原因
- 相关单测通过；REPORT 齐套
- 未触碰不做清单

立即开工。改完立刻跑验证并写证据目录。完成后在本 thread 用简短中文汇报：改动文件、sha、测例结果、证据路径。
