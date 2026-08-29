# YYHomeFNAsst

飞牛 fnOS 硬盘休眠与监控空间管理工具

[![Version](https://img.shields.io/badge/version-0.0.5-blue)](https://github.com/yangfty/YYHomeFNAsst)
[![Platform](https://img.shields.io/badge/platform-fnOS%20%3E%3D%200.9.27-green)](https://www.fnnas.com/)
[![License](https://img.shields.io/badge/license-MIT-orange)](#)

一键让硬盘休眠省电，空间不足自动清理旧监控录像。纯 Python 标准库实现，**零依赖、开箱即用**，不修改 fnOS 系统自带的休眠设置。

## 功能特性

### 硬盘状态一目了然
- 自动识别所有物理硬盘（SATA / USB / SAS），按序列号定位，不受 `/dev/sdX` 盘符变化影响
- 实时显示电源状态（hdparm -C）：运行中 / 空闲 / 已休眠 / 深度睡眠
- 显示型号、容量、可用空间、序列号、连接方式、挂载点

### 硬盘休眠
- **立即休眠**：一键向指定硬盘发送休眠指令（hdparm -y），执行后自动复查状态
- **定时休眠**（每块硬盘独立设置）：
  - `每天定时` —— 每天固定时间自动休眠（默认 02:00）
  - `空闲后自动` —— 硬盘持续非休眠超过 N 分钟后自动休眠（默认 30 分钟）

### 监控空间自动清理
- 每块硬盘可单独开启，自定义保留空间阈值（如剩 20 GB）、监控目录与**每天检查时间**（各硬盘独立设置）
- 剩余空间不足时，**递归扫描**监控目录下所有以日期命名的文件夹（如小米摄像头的 `2026062107`），**跨摄像头按日期从早到晚**依次删除，直到剩余空间高于阈值
- 每天定时检查（默认 00:00，可自定义），也支持手动「立即检查一次」
- 只删除纯数字日期命名的文件夹，其他文件和目录一律不动
- 清理结果（删除明细、释放空间）持久化保存，随时可在界面查看

## 下载

| 版本 | 下载 | 说明 |
|---|---|---|
| V0.0.5（最新） | [YYHomeFNAsst-0.0.5.fpk](https://github.com/yangfty/YYHomeFNAsst/releases/download/v0.0.5/YYHomeFNAsst-0.0.5.fpk) | 全新应用图标 + 界面配色统一 |
| V0.0.4 | [YYHomeFNAsst-0.0.4.fpk](https://github.com/yangfty/YYHomeFNAsst/releases/download/v0.0.4/YYHomeFNAsst-0.0.4.fpk) | 每盘独立检查时间 + UI 优化 |
| V0.0.3 | [YYHomeFNAsst-0.0.3.fpk](https://github.com/yangfty/YYHomeFNAsst/releases/download/v0.0.3/YYHomeFNAsst-0.0.3.fpk) | 定时休眠 + 监控空间清理 |
| V0.0.2 | [YYHomeFNAsst-0.0.2.fpk](https://github.com/yangfty/YYHomeFNAsst/releases/download/v0.0.2/YYHomeFNAsst-0.0.2.fpk) | 基础版：状态查询 + 一键休眠 |

更多历史版本请前往 [Releases 页面](https://github.com/yangfty/YYHomeFNAsst/releases)。

## 安装说明

### 环境要求
- 飞牛 fnOS **0.9.27** 及以上版本
- 硬盘需支持 ATA 休眠指令（绝大多数 SATA 机械硬盘支持；部分 USB 移动硬盘的桥接芯片不支持状态查询或休眠，属正常现象）

### 安装步骤
1. 从上方下载最新版 `YYHomeFNAsst-x.x.x.fpk`
2. 打开 fnOS **应用中心** → 右上角 **手动安装**（或"安装本地应用"）
3. 选择下载的 fpk 文件，确认安装
4. 安装完成后，桌面会出现 **YYHomeFNAsst** 图标；或直接访问 `http://NAS的IP:8327/`

### 升级 / 卸载
- **升级**：在应用中心对旧版本重新手动安装新版 fpk 即可，任务配置自动保留
- **卸载**：应用中心正常卸载；如需彻底清除任务配置，删除应用数据目录下的 `config.json`

## 使用说明

| 操作 | 入口 |
|---|---|
| 查看硬盘状态 | 打开应用，卡片实时显示电源状态与可用空间 |
| 立即休眠 | 硬盘卡片 →「立即休眠」 |
| 设置定时休眠 / 空间清理 / 每天检查时间 | 硬盘卡片 →「定时任务」（各硬盘独立设置） |
| 手动清理一次 | 定时任务弹窗 →「立即检查一次」 |

### 监控目录示例（小米摄像头）

```
/vol1/1000/监控-小米摄像2云台版/xiaomi_camera_videos/
├── 94F8275876BE/          ← 摄像头 SN
│   ├── 2026062107/        ← 按小时命名的录像文件夹（自动识别删除）
│   ├── 2026062108/
│   └── ...
└── AABBCCDDEEFF/
    ├── 2026062105/
    └── ...
```

监控目录直接填 `.../xiaomi_camera_videos` 即可，程序会递归扫描所有子目录中的日期文件夹，删除时按日期全局排序（先删最早的，无论属于哪台摄像头）。

## 注意事项

- 应用仅通过 `hdparm` 发送休眠指令和读取电源状态，**不会**修改 fnOS 系统自带的自动休眠设置
- 部分应用（如相册、下载、Docker 常驻容器）会周期性读写硬盘导致无法休眠，属系统层面行为
- 「空闲后自动」模式下，个别 USB 桥接芯片在状态查询时会被唤醒，此类硬盘建议改用「每天定时」模式
- **空间清理会永久删除监控目录下的日期文件夹**，请确认监控目录路径填写正确后再开启

## 更新日志

### V0.0.5
- 采用全新设计的应用图标：蓝底渐变 + 白色硬盘图案，填满整个图标空间，圆角外透明无白边
- 网页界面配色（页头 Logo、休眠按钮、主按钮、favicon）与新图标统一

### V0.0.4
- 空间清理检查时间改为**每块硬盘独立设置**（移除原右上角全局「定时设置」入口，旧配置自动迁移）
- 界面整体优化：硬盘卡片顶部状态色条、弹窗分区图标、移动端按钮布局，风格更统一
- 重新设计应用图标：填满整个图标空间，与 fnOS 风格更统一

### V0.0.3
- 新增定时休眠：每块硬盘可单独设置「每天定时」或「空闲后自动」休眠
- 新增监控空间清理：按硬盘设置剩余空间阈值与监控目录，每天定时检查，空间不足时自动删除日期最早的录像文件夹（兼容小米摄像头目录格式）
- 硬盘卡片新增「可用空间」显示
- 右上角「定时设置」可修改全局清理检查时间

### V0.0.2
- 界面优化：应用内显示版本号；硬盘卡片新增「上次休眠指令」记录；卡片加载动画更流畅

### V0.0.1
- 首个版本：硬盘识别、电源状态查询、一键立即休眠

## 开发

```bash
# 冒烟测试（67 项，模拟 fnOS 环境，Windows/Linux 均可运行）
python tools/test_mock.py

# 打包 fpk（需 fnOS 官方 fnpack 工具）
tools/fnpack.exe build -d YYHomeFNAsst
# 打包后把生成的 com.yyhome.fnasst.fpk 重命名为带版本号的文件名（本地仅保留最新版）：
# com.yyhome.fnasst.fpk → com.yyhome.fnasst-0.0.5.fpk

# 发布新版本（打 tag + 创建 Release + 上传 fpk 附件，历史版本由 Releases 管理）
python tools/release_github.py --token ghp_xxx --repo YYHomeFNAsst --version 0.0.5 --fpk com.yyhome.fnasst-0.0.5.fpk
```

## License

MIT
