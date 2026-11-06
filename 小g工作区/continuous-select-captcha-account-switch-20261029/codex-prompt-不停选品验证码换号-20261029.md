# Codex 开发卡：不停选品③验证码/登录/断连换号 · 2026-10-29 Asia/Shanghai

你在 Codex UI 可视化 thread 中执行本卡。进度只进本 thread，**勿塞 Trae**。禁 push、打包、删除无关文件、登录验证码绕过、扫码、实机百应狂点。

## 项目与路径
- 项目根：`D:\抖音电商\投广管理客户端`
- 本卡目录：`D:\software\投广客户端\小g工作区\continuous-select-captcha-account-switch-20261029\`
- 证据目录：`D:\software\投广客户端\小g工作区\verify-continuous-select-captcha-account-switch-20261029\`
- 基线 `engine\browser_cdp.py` SHA256：`A108D8E1E4E3FBA721CF640CADB434075C47CACBF613A26E71BD5E4354F66335`
- 若改引擎报新 sha；未改则 REPORT 写明仍为基线。

## 一句话目标
验证码 / 需登录 / 浏览器断开 → **停本账号**（保留原因与 checkpoint，UI 可读）并 **立刻换号继续选**；限流/11001/选品太多仍走卡②「闲置后同账号自动续」，**不要**把限流改成必须换号。勿回退①跨账号串行、②冷却自动续。

## 关键背景（核实后修）
1. 卡①：`runSequentialSelectionBatch` 已去掉跨账号 `stopReason`，A 失败后 B 仍调度。
2. 卡②：限流归类 + 冷却归零同账号自动续；`getSelectionErrorRecoveryState` 把 **滑块验证与 11001 一起标 `needs_retry`**。
3. 问题：滑块/验证码若继续与限流共用 `needs_retry`+自动续，会与闸门③「停本账号立刻换号」冲突；登录未确认/浏览器断开也需明确本账号停 + 换号，且文案可读（禁 IPC/堆栈）。

## 范围（只做卡③）

### A. 分类分流（必做）
1. 新增或明确分类（命名可自定，建议清晰）：
   - `ACCOUNT_SWITCH_CAPTCHA` / 滑块·验证码·安全验证
   - `ACCOUNT_SWITCH_LOGIN` / 需登录·登录态未确认（可复用/对齐 `LOGIN_OR_PAGE_STATE_UNCONFIRMED`）
   - `ACCOUNT_SWITCH_DISCONNECT` / 浏览器断开·页面关闭·context closed·websocket/cdp 断连等
2. **限流类保持卡②**：`BAIYING_RATE_LIMIT` / 11001 / 选品太多 / 请求过多 → 仍 `needs_retry` + 冷却同账号自动续；**不得**被本卡改成「必须换号」。
3. 验证码/登录/断连：
   - 本账号：停止继续对该账号发选品请求；持久化可读原因 + checkpoint（可用 `failed` 或独立态，但**不要**进入限流冷却自动续队列）
   - 批次：依赖①的串行，后续账号立刻继续；单测证明 A 遇上述三类后 B 仍被 `runAccount`
4. `toUserSelectionError` 增加上述三类可读中文（无 IPC/Trace）；诊断细节可折叠。

### B. 接线（必做）
- `selection_error_ux.ts` / `getSelectionErrorRecoveryState` / `SelectionSettingsView.vue`：
  - 限流 → 仍 arm cooldown + 自动续
  - captcha/login/disconnect → **不** arm 限流冷却自动续；标记本账号停；批次继续下一账号
- 引擎可选：若 `_selection_error_requires_pause` 或抛错文案需更易被桌面识别，可小改 classification/message；避免大重构。

### C. 测试（必做）
- A 抛滑块/验证码 → A 停且原因可查；B 仍调度；**不会**进入限流自动续 gate
- A 抛需登录 / 浏览器断开 → 同上
- A 抛 11001/选品太多 → 仍 `needs_retry`/限流路径（卡②回归）
- 卡①：A 普通失败后 B 仍执行（回归）
- 命令：
  ```
  node --test desktop/scripts/selection_batch.test.mjs desktop/scripts/selection_error_ux.test.mjs
  npm run typecheck --prefix desktop
  ```
- 若改引擎再跑 py_compile/相关 unittest。禁 pack。

### 明确不做
- 绕过验证码、扫码、自动填登录凭证
- 回退①②行为
- 实机 / push / 打包 / Trae / 开新 thread
- 大范围无关重构

## 关键入口
- `selection_error_ux.ts`、`selection_batch.ts`、`SelectionSettingsView.vue`
- 引擎：`browser_cdp.py` 中 `_selection_error_requires_pause` / 滑块·登录·断连相关抛错文案（仅必要时）
- 既有测：`selection_batch.test.mjs`、`selection_error_ux.test.mjs`
- 先例：P-16③；对照 P-14 文案

## 证据交付
`D:\software\投广客户端\小g工作区\verify-continuous-select-captcha-account-switch-20261029\`
含 REPORT（根因、改动、browser_cdp sha、做了/没做/风险、thread=`01a18082-51fe-7531-ac95-835d8b00a771`+turn id、Asia/Shanghai 时间）、DELIVER、files.sha256、测试/typecheck 日志、代码与测试副本、CLAIM 指针。

## 完成判定
- 验证码/登录/断连 → 停本账号 + B 立刻续；限流仍同账号自动续
- ①②相关测不回退；REPORT 齐套

立即开工。完成后本 thread 简短中文汇报。
