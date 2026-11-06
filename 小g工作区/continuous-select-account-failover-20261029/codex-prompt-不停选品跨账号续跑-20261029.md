# Codex 开发卡：不停选品①跨账号续跑 · 2026-10-29 Asia/Shanghai

你在 Codex UI 可视化 thread 中执行本卡。进度只进本 thread，**勿塞 Trae**。禁 push、打包、删除无关文件、登录验证码、扫码、实机百应狂点。

## 项目与路径
- 项目根（`-C`）：`D:\抖音电商\投广管理客户端`
- 本卡目录：`D:\software\投广客户端\小g工作区\continuous-select-account-failover-20261029\`
- 证据目录（必须写出）：`D:\software\投广客户端\小g工作区\verify-continuous-select-account-failover-20261029\`
- CLAIM / ACK / 需求卡：见本卡目录与 `D:\software\投广客户端\小g工作区\需求卡-全链路不停选品-2026-10-29.md`
- 基线 `engine\browser_cdp.py` SHA256（开工前再核）：
  `A108D8E1E4E3FBA721CF640CADB434075C47CACBF613A26E71BD5E4354F66335`
- 本卡**预期主要改桌面批跑**；若未改引擎，REPORT 写明并记录当前 sha；若改了必须 ≠ 基线。

## 一句话目标
账号 A 因失败/暂停停下时：保留 A 的原因与 checkpoint/`needs_retry`；**立刻自动开跑后续账号 B…**；**不得**用 `stopReason` 把剩余账号标「未执行」。

## 根因（按此修，可核实后微调）
`desktop/src/renderer/src/utils/selection_batch.ts` → `runSequentialSelectionBatch`：
- 一旦设置 `stopReason`（含 11001、非「完成」且 `continueBatch !== true`、catch），后续账号全部 `未执行` +「已停止本批次剩余平台请求」。
- 这与 P-16① / 用户「前一个账号停下来，后一个也要继续选品」冲突。

## 范围（只做卡①）

### 必做
1. 改 `runSequentialSelectionBatch`（及必要的调用方接线）：
   - 账号 A 失败 / 部分完成 / 抛错 / 需暂停时：为 **A** 写入可读失败原因；状态可用 `失败` / 既有 recovery（`needs_retry`）等，**保留可查 checkpoint**。
   - **不要**再因 A 的失败设置会掐断整批的 `stopReason`；循环须继续调度 B、C…
   - 后续账号在无人点「继续选品」的情况下仍被 `runAccount` 调用（单测 mock 即可证明）。
2. 11001：本卡只要保证「A 遇 11001 后 B 仍开跑」。**不要**在本卡实现「冷却后同账号自动续」（那是卡②）。可暂时把 11001 的 A 标 `needs_retry`，但不得拖死 B。
3. UI 不出现原始 IPC/堆栈（勿回退 P-14 映射）。
4. 单测：扩展 `desktop/scripts/selection_batch.test.mjs`（或同风格新测）
   - 模拟 A 失败 → B 仍被调度且 status 不是因 stopReason 产生的「未执行」
   - A 的 outcome 仍带失败 message / 可恢复态
   - 回归：既有 selection_batch / selection_error_ux 相关测不要挂

### 明确不做
- 卡②：11001/「选品太多」闲置后同账号自动续
- 卡③：验证码/登录/断连触发与文案细化（本卡只保证 A 已停后 B 能开跑）
- 实机 / push / 打包 / 扫码 / 改 Trae / 开新 thread
- 不要大范围无关重构；尽量不动 `browser_cdp.py`（除非批调度必须）

## 关键入口
- `desktop\src\renderer\src\utils\selection_batch.ts`（`runSequentialSelectionBatch` ≈300+；`getSelectionErrorRecoveryState`）
- 调用方：`SelectionSettingsView.vue` 等（确认不要另有逻辑在 A 失败后直接 return 整批）
- 测：`desktop\scripts\selection_batch.test.mjs`
- 先例：`docs\决策先例.md` P-2026-10-29-16①

## 验证命令示例
```
node --test desktop/scripts/selection_batch.test.mjs
# 若有 error_ux 测一并跑：
node --test desktop/scripts/selection_batch.test.mjs desktop/scripts/selection_error_ux.test.mjs
```
在 desktop 或项目根按现有习惯执行；REPORT 写清 cwd。可选 `npm run typecheck`。禁 pack。
若动了引擎才跑 py_compile/unittest。

## 证据交付（缺一不可）
目录：`D:\software\投广客户端\小g工作区\verify-continuous-select-account-failover-20261029\`
1. `REPORT.md` — 根因；改动清单；browser_cdp sha（无论是否改）；做了/没做/风险；thread=`01a18082-51fe-7531-ac95-835d8b00a771` + turn id；完成时间 Asia/Shanghai
2. `DELIVER.md`
3. `files.sha256.txt`
4. 前端测试日志；改动/测试文件副本
5. CLAIM 指针

## 完成判定
- A 失败后 B 仍被调度（单测证明）
- 剩余账号不再因 `stopReason` 整批「未执行」
- A 失败原因可查；相关单测通过；REPORT 齐套
- 未做卡②③范围

立即开工。完成后本 thread 简短中文汇报：改动文件、sha、测例、证据路径。
