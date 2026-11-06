# 认领复述·不停选品①跨账号续跑 · 开发工程师 · 2026-10-29 Asia/Shanghai

## 已重读
- CLAIM：`continuous-select-account-failover-20261029/CLAIM.md`
- 需求卡：`需求卡-全链路不停选品-2026-10-29.md`（需求+方案闸门：拆三张之第①张）
- 先例：P-2026-10-29-16 ①；对照 P-14/P-15 已关勿复开
- 基线 `engine/browser_cdp.py` SHA256：`A108D8E1E4E3FBA721CF640CADB434075C47CACBF613A26E71BD5E4354F66335`（机上已核；本卡预期主要改桌面批跑）

## 一句话理解
账号 A 失败/暂停时保留原因与 checkpoint/`needs_retry`，立刻自动开跑后续账号 B…；禁止用 `stopReason` 把剩余账号标「未执行」。

## 范围确认
- 做：批调度跨账号续跑（`selection_batch.ts` / `runSequentialSelectionBatch`）
- 不做：卡② 11001/选品太多闲置同账号自动续；卡③ 验证码/登录/断连触发细化；实机/push/打包
- 进度进 Codex UI thread `01a18082-51fe-7531-ac95-835d8b00a771`，勿塞 Trae

## 状态
已认领，开工 Codex。
