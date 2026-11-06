# 认领复述·不停选品③验证码/登录/断连换号 · 开发工程师 · 2026-10-29 Asia/Shanghai

## 已重读
- CLAIM：`continuous-select-captcha-account-switch-20261029/CLAIM.md`
- 需求卡：`需求卡-全链路不停选品-2026-10-29.md`（拆三张之③；①②已关）
- 先例：P-2026-10-29-16 闸门③
- 基线 `engine/browser_cdp.py` SHA256：`A108D8E1E4E3FBA721CF640CADB434075C47CACBF613A26E71BD5E4354F66335`（①②交付后未改引擎）

## 一句话理解
验证码/需登录/浏览器断开 → 停本账号（原因+checkpoint 可查、文案可读）并立刻换号继续；限流/11001 仍走②同账号自动续，勿误判成换号；勿回退①②。

## 范围确认
- 做：captcha/login/disconnect 本账号停 + 后续账号继续；与限流路径分流；P-14 级可读文案
- 不做：绕过验证码/扫码/凭证外登录自动化；回退①②；实机/push/打包
- 进度进 Codex UI thread `01a18082-51fe-7531-ac95-835d8b00a771`，勿塞 Trae

## 状态
已认领，开工 Codex。
