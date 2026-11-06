# Codex 开发卡：不停选品②限流/11001/选品太多闲置后同账号自动续 · 2026-10-29 Asia/Shanghai

你在 Codex UI 可视化 thread 中执行本卡。进度只进本 thread，**勿塞 Trae**。禁 push、打包、删除无关文件、登录验证码、扫码、实机百应狂点。

## 项目与路径
- 项目根（`-C`）：`D:\抖音电商\投广管理客户端`
- 本卡目录：`D:\software\投广客户端\小g工作区\continuous-select-ratelimit-autoresume-20261029\`
- 证据目录：`D:\software\投广客户端\小g工作区\verify-continuous-select-ratelimit-autoresume-20261029\`
- CLAIM / ACK / 需求卡：见本卡目录与 `需求卡-全链路不停选品-2026-10-29.md`
- 基线 `engine\browser_cdp.py` SHA256：
  `A108D8E1E4E3FBA721CF640CADB434075C47CACBF613A26E71BD5E4354F66335`
- 若改引擎交付新 sha；未改则 REPORT 写明仍为基线。

## 一句话目标
识别「选品太多」/接口请求过多/11001 等限流频控，不当硬停；**闲置等待结束后同账号自动继续选品**（无人点「继续选品」也要恢复）。可保留按钮作手动入口。

## 现状（按此改，可核实）
1. P-14 已有：`toUserSelectionError` → `BAIYING_RATE_LIMIT`；`armSelectionCooldown`；冷却中禁用按钮；冷却结束文案仍是「点击继续选品…」——**缺自动触发**。
2. `getSelectionErrorRecoveryState` / classify 主要认 11001/`BAIYING_RATE_LIMIT`，**未必覆盖「选品太多」「请求过多」**。
3. 卡①已去掉跨账号 `stopReason`；本卡**不得回退**该行为。

## 范围（只做卡②）

### A. 识别（必做）
- 扩展限流分类，至少覆盖：`11001`、`BAIYING_RATE_LIMIT`、「选品太多」、「请求过多」、常见「频控/限流/冷却」文案（中英文合理子集）。
- 映射到同一可恢复态（建议继续用 `BAIYING_RATE_LIMIT` / `needs_retry`），用户文案可读，禁 IPC/Trace。
- `getSelectionErrorRecoveryState` 与 UX 分类保持一致。

### B. 闲置后同账号自动续（必做）
1. 冷却倒计时仍可见（复用 `armSelectionCooldown` / 进度区）。
2. **`selectionCooldownRemaining` 归零后**：自动从本机 checkpoint/`needs_retry` 触发同账号续跑（复用现有「继续选品」/recovery 入口，例如现有 resume 函数），**无需人工点击**。
3. 防双触发：自动续进行中时按钮可仍显示/禁用；手动按钮与自动续共用同一 flight（`createSingleFlight` 或等价），避免并发两次恢复。
4. 组件卸载/离页时清 timer，避免泄漏或幽灵续跑。
5. 冷却中：保持暂停对该账号的百应狂点；倒计时结束才自动续。

### C. 测试（必做）
- 扩展 `selection_error_ux.test.mjs`：选品太多/请求过多 → 限流分类
- 扩展 `selection_batch.test.mjs` 或新增窄测：冷却结束触发自动续的逻辑可测部分（纯函数/可抽函数优先；Vue 侧可用抽离的 scheduleAutoResume 测）
- 确认卡①相关断言仍在（A 失败后 B 仍调度）
- 命令示例：
  ```
  node --test desktop/scripts/selection_batch.test.mjs desktop/scripts/selection_error_ux.test.mjs
  npm run typecheck --prefix desktop
  ```
- 若改引擎：`py_compile` + 相关 unittest。禁 pack。

### 明确不做
- 卡③：验证码/登录/断连 → 停本账号立刻换号
- 回退卡①跨账号续跑
- 实机狂点 / push / 打包 / Trae / 开新 thread
- 不要大范围无关重构

## 关键入口
- `SelectionSettingsView.vue`：`armSelectionCooldown`（≈329+）、冷却 UI（≈3778+）、继续选品/`needs_retry` recovery（≈2796+）
- `selection_error_ux.ts`：classify / cooldownSeconds
- `selection_batch.ts`：`getSelectionErrorRecoveryState`（勿再引入跨账号 stopReason）
- 引擎可选：`browser_cdp.py` cooldown / `_mark_page_rate_limited` / 11001 分类——仅当桌面自动续需要更准的 remaining 秒数时再动
- 先例：P-16② + 补充④；P-14 对照

## 证据交付
目录：`D:\software\投广客户端\小g工作区\verify-continuous-select-ratelimit-autoresume-20261029\`
1. `REPORT.md`（根因、改动、browser_cdp sha、做了/没做/风险、thread=`01a18082-51fe-7531-ac95-835d8b00a771`+turn id、完成时间 Asia/Shanghai）
2. `DELIVER.md`；`files.sha256.txt`；前端测试/typecheck 日志；改动与测试副本；CLAIM 指针

## 完成判定
- 「选品太多」/请求过多/11001 → 限流可恢复，不当硬停
- 冷却归零后同账号自动续（单测或可测调度证明）
- 卡①行为未回退；相关测通过；REPORT 齐套

立即开工。完成后本 thread 简短中文汇报。
