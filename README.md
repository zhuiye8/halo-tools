# halobar — 花再 Halo PixelBar 歌词与自定义像素屏

Windows 桌面工具，把当前播放歌曲的歌词实时显示在「花再 Halo PixelBar」的 256×32 单色点阵屏上，并支持上传自定义图片/动画。

> 非官方项目，与漫步者 / 花再无隶属关系。所有写入均为设备公开协议，不刷固件。

## 当前状态

| 能力 | 状态 |
| --- | --- |
| USB HID 设备层（握手 / 版本 / 状态 / 文字 / 布局 / 快照恢复） | ✅ 实机验证（固件 1.1.1） |
| SMTC 媒体会话读取（标题 / 歌手 / 播放状态 / 时间轴） | ✅ 实机验证 |
| 6K Labs 本地进度源（`GET 127.0.0.1:9863/query`，需插件） | ✅ 实机验证（真实进度、歌曲 ID、时长、封面） |
| 网易云匿名取词（搜索 + LRC + 翻译 + 本地缓存） | ✅ 实机验证 |
| 歌词同步引擎（分段 / 时钟 / 暂停待机 / 退出恢复） | ✅ 实机跑通 |
| 自定义点阵上传（256×32，1–8 帧） | ✅ 实机验证（控制台上传，静态 1.2 s / 3 帧 3.2 s） |
| 网页控制台（状态 / 命令 / 设置 / 日志） | ✅ 实机验证（`127.0.0.1:8765`，token 保护） |
| 托盘（状态 / 暂停 / 恢复 / 打开控制台） | ✅ 可用（原生菜单，外观朴素） |
| 蓝牙控制 | ❌ 固件限制：切到蓝牙输入后 USB HID 消失，只能逆向杰理 SPP |

## 快速开始

```powershell
python -m pip install -e ".[media,dev]"   # winrt 投影 + pytest

python -m halobar run                      # 托盘 + 网页控制台（右键图标：暂停/恢复/退出）
python -m halobar run --no-tray --seconds 10   # 无界面跑 10 秒（冒烟测试）
python -m halobar run --no-console         # 只要托盘，不起网页控制台
python -m halobar probe                    # 读固件/状态/两个文字槽
python -m halobar lyrics --title 如愿 --artist 王菲   # 只取词，不写设备
python -m halobar demo --seconds 60        # 一次性同步（退出自动恢复屏幕）
python -m halobar watch --seconds 10       # 观察当前进度源
```

托盘菜单：当前曲目状态、暂停/继续同步、恢复屏幕（停止跟随）、打开控制台、退出。
`run` 启动时会保存屏幕快照，退出（含 Ctrl+C / 托盘退出）自动恢复。

网页控制台在启动时打印带 token 的地址（`http://127.0.0.1:8765/?token=…`），
也可以从托盘菜单「打开控制台」直接进入。控制台只监听 127.0.0.1。

## 数据与日志

程序目录可以只读，所有运行时数据都在 `%LOCALAPPDATA%\halobar`：

| 路径 | 内容 |
| --- | --- |
| `settings.json` | 控制台 token、端口、对齐/动画/待机画面等偏好（重启保留） |
| `logs\halobar.log` | 滚动日志（5 MB × 3） |
| `logs\crash.log` | 未捕获异常（崩溃）记录 |
| `cache\lyrics\` | 歌词缓存 |
| `assets\` | 上传到音箱的原始素材 |

单实例运行：已经有一个实例时，再次双击只会把控制台打开，不会抢设备。
控制台地址固定（token 持久化），可以直接加书签。
桌面快捷方式：控制台顶部「创建桌面快捷方式」按钮，或托盘菜单同名项。

## 架构

```
halobar/
├── app.py             CLI（probe / lyrics / demo / watch）
├── device/
│   ├── protocol.py    帧格式、校验、命令码、UTF-8 安全截断
│   ├── transport.py   Win32 HID（ctypes，无第三方依赖）
│   └── screen.py      高层 API：状态、文字槽、布局、快照/恢复
├── media.py           Windows SMTC 会话读取（6K Labs 优先，SMTC 次之）
├── sixk.py            6K Labs 本地 HTTP 进度源（/query）
├── lyrics.py          LRC 解析、网易云匿名客户端、缓存
├── sync.py            分段、进度时钟、同步引擎
├── service.py         后台服务（单写者、重连、状态快照、退出恢复）
├── server.py          本地网页控制台（127.0.0.1，token 保护）
├── web/index.html     控制台页面（原生 HTML/JS，无框架）
└── tray.py            托盘（pystray，菜单与状态显示）
tests/                 协议 / 解析 / 引擎单元测试（无硬件）
probe/                 早期协议验证脚本（保留作参考）
```

设计约束（为了可维护）：

1. **单一数据源**：协议常量集中在 `device/protocol.py`，不再散落在脚本里。
2. **可替换传输层**：`Screen` 只依赖 `transport.write/read`，测试用假屏幕即可跑引擎。
3. **写入即校验**：文字写入有 `2F CC` 专用 ACK；布局/快照恢复回读状态。
4. **失败不重试**：设备状态不确定时宁可报错，也不重复写（避免把屏幕写坏）。
5. **无 OCR、无内存读取**：不依赖客户端版本，不需要改客户端。

## 已验证的协议事实

| 项 | 值 |
| --- | --- |
| USB | VID `2D99` / PID `A160`，usage page `0xFF14`，64 字节报告 |
| 握手 / 固件 | `0x65` → 协议 2；`0xC6` → `1.1.1` |
| 文字 | `0xE7` 读 / `0xE8` 写；载荷 `[slot, len, utf8...]`，单包 **≤50 字节**（官方规格 48） |
| 文字槽 | `0x00` 自创文字（max 16 字）、`0x0E` 歌词槽（报 max 80 字） |
| 布局 | `0xEF`：`[1,R,G,B,0,2,0,align,0xFF]` 静态 / `[1,R,G,B,0,2,1,dir,0xFF]` 滚动 |
| 歌词动画 | `0xEF`：`[1,R,G,B,0,0,1,0,preset]`，preset 0–4 |
| 场景 | `0xEF`：`[2,R,G,B,0,1,category,option,template]`（9 类内置场景） |
| 状态 | `0xEE` 返回 12 字节：`[0]=0, [1..3]=RGB, [4]=组合, [5]=模式, [6]=类别, [7]=选项, [8]=模板, [9..11]=音乐选项` |
| 点阵上传 | `0x17` 分包（设备给 50 字节/包），单帧 1064 字节 ≈1.2 s，8 帧 ≈9.2 s，切场景瞬时 |

## 精确进度模式（可选，需要装客户端插件）

网易云自己上报的 SMTC **没有时间轴**，所以默认使用「自建时钟」兜底。若想要播放器给出的真实进度（拖动、卡顿都准确），按下面三步给**官网安装版**网易云装上插件链：

| 步骤 | 做什么 | 链接 |
| --- | --- | --- |
| 0 | 换成官网安装版（MSIX/Store 版无法注入） | https://music.163.com/#/download |
| 1 | 装 BetterNCM（插件框架） | https://github.com/std-microblock/BetterNCM-Installer/releases/latest |
| 2 | 装 InfLink-rs（给网易云补 SMTC 进度与会话 API） | https://github.com/apoint123/inflink-rs/releases/latest |
| 3 | 装 6K Labs（本地 HTTP：`GET 127.0.0.1:9863/query`） | https://github.com/lgc2333/BetterNCM-6K-Labs/tree/dist |

验证：`python probe/query_9863.py 3`，能打印 `hasSong=True` 与递增的 `pos` 即成功。

装好后 `halobar` 自动优先使用 6K Labs（`media.read_playback` 内部顺序：6K Labs → SMTC → 时钟）。插件返回的 `track.id` 形如 `NCM-1888381008`，可用来精确取词、免去搜索匹配。

> 风险提示：DLL 注入会被部分杀软/反作弊误报；网易云大版本更新后插件可能失效需重装。不装插件不影响基础功能。

## 素材工作台（自定义像素屏）

控制台里的「自定义像素屏」卡片：

1. 选择 PNG / JPG / BMP / WebP / GIF（GIF 最多 8 帧，每帧 ≥100ms）；
2. 调整「适配」（完整显示 / 铺满裁切 / 拉伸）、阈值、反色、抖动；
3. 点「上传到音箱」，页面会显示进度与上传后的预览；
4. 勾选「设为待机画面」后，暂停/间奏时会自动切到这张图；「立即显示」可马上切过去。

限制与代价：

- 上传会**覆盖音箱里的自定义场景槽**（静态用 category 3，动画用 category 6），原始内容需回 TempoHub / App 重新发送；
- 静态图约 1.2 秒、8 帧动画约 9 秒（设备每包 50 字节，逐包确认）；
- 上传过程中歌词同步会暂停，完成后恢复。

## 显示行为

| 情形 | 行为 |
| --- | --- |
| 单句较窄（≤14 个半角格） | 静态显示，对齐方式可调 |
| 单句较宽但单包能装下（≤48 字节，约 16 汉字） | 固件原生右→左滚动 |
| 单句超过 48 字节 | 按行内时间比例切片轮显，切片切换借用设备淡入动画 |
| 前奏 / 间奏 4–10 秒 | 清空歌词文字（不留上一句） |
| 前奏 / 间奏 >10 秒 | 切回待机场景（默认内置 Waves），下一句自动切回歌词 |
| 暂停超过 20 秒 | 切回待机场景 |
| 歌词里的制作人 / 演唱者标注行 | 过滤（含 `作词 :`、`祝青：`、`G2er` 这类） |

## 已知限制

- **网易云没有播放进度**：它会上报 SMTC 会话（`cloudmusic.exe`），但 `TimelineProperties` 全为 0。当前回退为「自建时钟 + 播放/暂停采样」，拖动进度条后会漂移，可用 `--offset` 或后续的微调快捷键修正。
- **TempoHub 抢设备**：官方 PC 客户端会独占 HID 并写入歌词槽。同时运行会出现"两个程序抢屏"，建议二选一（我们正在做设备互斥检测）。
- **蓝牙不可控屏**：切换到蓝牙输入后 `USB\VID_2D99&PID_A160` 从系统消失，只剩音频通道。
- **点阵上传会覆盖自定义场景槽**，原始内容需回 TempoHub / App 重新发送。

## 开发

```powershell
python -m pytest -q          # 21 个单元测试，无需硬件
python -m halobar probe      # 需要设备
```

提交前请保证 `pytest` 全绿；协议相关改动必须补充对应的单元测试。

## 相关项目

- [XFEstudio/HaloPixelToolBox](https://github.com/XFEstudio/HaloPixelToolBox)（MIT）：协议与业务参考
- [trlanfeng/OpenHaloPixelBar](https://github.com/trlanfeng/OpenHaloPixelBar)：点阵上传协议实现
- [WXRIW/Lyricify-Lyrics-Helper](https://github.com/WXRIW/Lyricify-Lyrics-Helper)：取词与匹配策略参考