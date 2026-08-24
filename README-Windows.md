# QQ Bot v3 — Windows 版使用说明（普通用户）

## 这是什么

QQ 群 AI 机器人 + 图形控制台，**免安装、免装 Python**。
解压整个 `qq-bot-v3-win` 文件夹到任意位置（路径不要有中文/空格最稳妥，
如 `D:\qq-bot-v3-win`），然后**双击 `start_gui.vbs`** 即可。

首次运行会自动在桌面创建「QQ Bot」快捷方式，以后直接点图标。

```
qq-bot-v3-win\
├── start_gui.vbs      ← 双击它启动（无黑窗口，后台运行）
├── stop_gui.vbs       ← 双击它停止（停 bot + 关界面）
├── start.bat          ← 调试用：带窗口启动，可看日志输出
├── make_shortcut.ps1  ← 建桌面快捷方式（start.bat 自动调用）
├── stop_bot.ps1       ← 停止逻辑（stop_gui.vbs 自动调用）
├── config.yaml        ← 配置文件（用记事本打开修改）
├── .env               ← API 密钥（首次使用需创建，见下）
├── assets\            图标
├── data\
│   ├── question_bank\ 题库（真心话大冒险）
│   └── napcat\         QQ 登录态（扫码一次后免扫）
└── python\            内置 Python 运行时（不要动）
```

## 首次使用三步

**1. 填 API 密钥**

新建文件 `qq-bot-v3-win\.env`（与 `config.yaml` 同目录，记事本新建即可）：

```
# 远程 LLM（必填，二选一）
REMOTE_API_KEY=你的API密钥

# 本地 ComfyUI 画图（可选，不用画图可留空）
COMFYUI_URL=http://你的机器IP:8188
```

`config.yaml` 里 `llm.remote_api` 填你的 API 地址（如 `https://api.deepseek.com/v1`），
`llm.remote_model` 填模型名（如 `deepseek-chat`）。其余参数都有默认值，
不懂可以不改。

**2. 登录 QQ**

启动后打开 GUI 总览页 → 点「启动 bot」→ NapCat 会显示**扫码二维码**，
用手机 QQ 扫一下登录你要用的机器人账号。
登录态存在 `data/napcat/`，以后重启不用再扫。

> 内置 NapCat Windows 运行环境（`data\napcat_win\`），**无需下载**，
> 解压即可用。若内置版本需升级：删除 `data\napcat_win\` 目录后点
> 「刷新二维码」，程序会从 GitHub 下载最新版
> （https://github.com/NapNeko/NapCatQQ/releases 的
> `NapCat.Shell.Windows.Node.zip`）。

**3. 完成**

bot 连上后开始收发消息。GUI 各标签页：总览（启动/停止/扫码）、配置、
消息管理（聊天记录/存档/转发查看）、人设、群组、游戏、画图、日志。

## 常见问题

**Q: 双击没反应？**
看 `data\bot.log`（记事本打开）——那是运行日志。
Windows 防火墙首次会弹「允许访问网络」，勾「专用网络」点允许。

**Q: 杀毒软件报毒/误删文件？**
内置 Python 和 PySide6 是官方组件。若被隔离，恢复文件并
把整个文件夹加入信任区。

**Q: 怎么改配置？**
记事本打开 `config.yaml`，改完**在 GUI 点「重载配置」或重启程序**生效。

**Q: 想彻底卸载？**
① 双击 `stop_gui.vbs` 停止 → ② 删除整个文件夹 → ③ 删桌面快捷方式。
聊天记录/登录态都在文件夹里（`data\`），删了就没有。
想保留记录就先备份 `data\` 目录。

**Q: 电脑重启后要重新扫码吗？**
不用。登录态持久化，重启电脑后双击启动即可（QQ 服务器偶尔会
失效登录态，那种情况重扫一次）。

**Q: 能装在 C 盘/移动硬盘吗？**
都行，文件夹在哪程序就在哪（绿色免安装）。移动硬盘注意：
换盘符后路径变了，登录态不受影响（数据跟着文件夹走）。

## 技术说明（给想折腾的人）

- 内置 Python 3.14.7（官方 embed 版 + PySide6 6.11 等依赖，全在 `python\`）
- 2026-08-24 起发行包已瘦身优化（zip 体积 164MB→63MB，解压后
  276MB→150MB）：PySide6 裁剪为仅保留 GUI 实际使用的 QtCore/QtGui/
  QtWidgets/QtMultimedia/QtMultimediaWidgets 及依赖链，其余 Qt 模块
  （WebEngine/QML/3D/图表/翻译文件等）全部移除。「NapCat 控制台」
  从内嵌网页窗口改为调用系统默认浏览器打开（无网页组件依赖，功能不变）。
- bot 主进程 = `python\python.exe gui_launcher.py --start`
  （GUI 内嵌控制 API 8697，bot WS 监听 8696）
- 运行日志：`data\bot.log`（bot 主日志）。调试模式（start.bat）
  的窗口内也会实时滚动全部输出
- 停止机制：`stop_bot.ps1` 先走控制 API 优雅停 bot，
  再按命令行特征清理进程
