# 投广管理客户端

Windows x64 桌面客户端，用于管理抖音电商选品、素材和发布流程。

## 下载

最新普通版：**v0.0.10**。前往 [GitHub Releases](https://github.com/hy112429/-2-/releases/latest) 下载 Windows 安装器或便携 ZIP。

安装器提供安装目录选择。客户端数据目录和软件安装目录分开管理；修改安装位置不会自动移动账号数据或素材。

## 路径说明

- 安装目录：在安装向导中选择。
- 客户端数据目录：按当前 Windows 用户保存在 `%APPDATA%\touguang-client-client-data-v1\client-data`。
- 素材目录：在客户端「选品设置 → 通用下载目录」单独选择；账号设置的下载路径优先于通用目录。
- 恢复已有选品批次时，客户端会在确认后把所选未完成账号的有效下载目录更新为当前已保存设置；未下载的详情图及新生成视频使用新目录。已有详情图、生成视频和其他素材保留原位，不自动搬迁。

## 更新方式

客户端当前没有内置自动更新。安装已有版本的用户需要从 Releases 下载最新版并运行安装器。

当前公开版为 v0.0.10。每日定时选品功能仍在源码开发中，尚未包含于该安装包；源码功能启用后，安装版可选择随 Windows 登录启动，定时计划默认关闭。启用后，首次失败不会耗尽当天计划；非登录/验证类失败会保留检查点并持续自动续跑，未完成计划跨日继续，重试间隔从 30 秒逐步延长至最多 5 分钟；明确需要登录或人工验证时暂停。成功完成后等待下一天计划。触发、重试与处理结果记录在本机 `diagnostics/selection/daily-schedule.jsonl`。

源码开发版的「商品查询与下载」新增千川开放平台商品视频查询。需要在本机配置 App ID、App Secret，并使用开放平台生成的授权 URL 授权千川账户；授权与视频查询的操作步骤见 [千川开放平台接入操作单](docs/千川开放平台接入操作单.md)。此功能尚未包含于公开安装包，真实账户与视频下载待验收。

## 本地开发与打包

需要 Windows x64、Node.js/npm，以及 `desktop/electron-builder.yml` 中配置的 Python 和 FFmpeg staging 运行时。

```powershell
cd desktop
npm install
npm run audit:ui  # 检查各页面按钮绑定与自定义点击区域的键盘操作
npm run build
& ..\engine\.venv\Scripts\python.exe scripts\ui_visual_smoke.py  # 本地模拟 API 的界面烟测
npm run pack
```

版本记录见 [`docs/CHANGELOG.md`](docs/CHANGELOG.md)。
