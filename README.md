# 投广管理客户端

Windows x64 桌面客户端，用于管理抖音电商选品、素材和发布流程。

## 下载

最新普通版：**v0.0.10**。前往 [GitHub Releases](https://github.com/hy112429/-2-/releases/latest) 下载 Windows 安装器或便携 ZIP。

安装器提供安装目录选择。客户端数据目录和软件安装目录分开管理；修改安装位置不会自动移动账号数据或素材。

## 路径说明

- 安装目录：在安装向导中选择。
- 客户端数据目录：按当前 Windows 用户保存在 `%APPDATA%\touguang-client-client-data-v1\client-data`。
- 素材目录：在客户端「选品设置 → 通用下载目录」单独选择；账号设置的下载路径优先于通用目录。
- 恢复已有选品批次时沿用批次中保存的目录；新目录用于新建批次，已下载素材不会自动搬迁。

## 更新方式

客户端当前没有内置自动更新。安装已有版本的用户需要从 Releases 下载最新版并运行安装器。

## 本地开发与打包

需要 Windows x64、Node.js/npm，以及 `desktop/electron-builder.yml` 中配置的 Python 和 FFmpeg staging 运行时。

```powershell
cd desktop
npm install
npm run build
npm run pack
```

版本记录见 [`docs/CHANGELOG.md`](docs/CHANGELOG.md)。
