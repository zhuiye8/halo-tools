# 花再 Halo PixelBar 桌面工具：调研与实现方案

> 调研对象：[XFEstudio/HaloPixelToolBox](https://github.com/XFEstudio/HaloPixelToolBox)（WinUI3 + USB HID 的花再音响工具箱）
> 两个产品需求：**① 接入网易云、QQ 音乐等主流音乐平台并显示歌词；② 可以自定义像素屏内容（官方素材太少）。**
> 调研日期：2026-11-22 ｜ 测试设备：本机已连接的花再 Halo PixelBar（VID `2D99` / PID `A160`，固件 `1.1.1`）

---

## 0. 结论先行（TL;DR）

| 问题 | 结论 | 依据 |
| --- | --- | --- |
| 需求① USB 能否显示歌词 | **可以，已在本机实测打通** | 握手 `0x65`、版本 `0xC6`、写文字 `0xE8` + 回读 `0xE7` 全部成功，写一条文字往返 **2 ms** |
| 需求① 蓝牙能否控制屏幕 | **经典蓝牙只有音频通道，没有 BLE GATT；屏显控制走不了蓝牙** | 本机 SDP 记录只有 A2DP Sink / AVRCP / HFP / 杰理 `JL_SPP`；`BTHENUM` 下无 `BTHLE` 节点；官方手册写明 PC 端必须「USB 线 + 声卡输入（红灯）」 |
| 需求① 那"蓝牙也要可以"怎么满足 | **蓝牙放歌 + USB 控制的双连接**（音箱同时保持 USB 插入），或退一步用手机 App 的官方蓝牙歌词 | 本机 USB 复合设备与经典蓝牙配对记录可共存；待实测「蓝牙输入（白灯）时 HID 是否仍响应」 |
| 歌词从哪来 | **系统 SMTC（媒体会话）+ lrclib 歌词库**为主；网易云内存读行只作兜底 | HaloPixelToolBox 的 Spotify 分支已验证 SMTC + lrclib 路线 |
| 歌词怎么显示 | **优先用设备原生文字通道**（16 字 / 48 字节），长句按时间切片；点阵上传只做"待机画面" | 本机实测原生文字上限、布局、5 档进出动画、恢复快照 |
| 需求② 官方素材太少？ | **确实少**：9 个内置场景 + 5 种歌词进出动画 + 自创文字仅 16 字（静态），没有面向用户的自定义图片入口 | 官方手册 EDF286015；本机 `0xE7` 实测 16 字符上限 |
| 需求② 能否完全自定义？ | **能，且已在本机实测通过**：任意图片/GIF → 256×32 单色点阵 → 上传，静态 1.2 s、2 帧 2.4 s、8 帧 9.2 s；切场景瞬时 | 本机上传 "HALO 自定义点阵" 静态图与 8 帧动画均被设备确认并显示 |
| 两个需求怎么共存 | **一条"屏幕仲裁"状态机**：播放时走歌词（2 ms 级），暂停/空闲切回自制的待机动画（场景切换瞬时） | 两种通道互不冲突，均可回读校验 |

一句话：**HaloPixelToolBox 的"读内存歌词"是最脆弱的一环，建议改成"SMTC + 歌词库 + 原生文字通道"；自定义像素屏不做则已，要做就是"素材工作台 + 场景槽管理"，蓝牙按"音频走蓝牙、控制走 USB"来设计。**

---

## 1. HaloPixelToolBox 拆解

### 1.1 项目定位与结构

MIT 许可，C# / WinUI3，三个可执行端：

| 目录 | 作用 |
| --- | --- |
| `Client/` | 用户端：托盘 + 主界面 + 歌词同步（网易云、Spotify 两套） |
| `Backend/` | 管理端：维护各版本网易云的「内存指针解析表」和 IP 黑名单 |
| `Server/` | 服务端：下发解析表、鉴权、IP 封禁（默认 `http://localhost:3300/`） |
| `Core/` | 协议与设备：`HaloPixelDevice`、`HidPacketBuilder`、`CloudLyricsReader`、`CloudLyricsReader` 引用的 `MemoryEditor` |

关键文件（后续可对照）：

- 设备层：[HaloPixelDevice.cs](https://github.com/XFEstudio/HaloPixelToolBox/blob/master/Core/HaloPixelToolBox.Core/Utilities/HaloPixelDevice.cs)
- 协议层：[HidPacketBuilder.cs](https://github.com/XFEstudio/HaloPixelToolBox/blob/master/Core/HaloPixelToolBox.Core/Utilities/HidPacketBuilder.cs)
- 网易云取词：[CloudLyricsReader.cs](https://github.com/XFEstudio/HaloPixelToolBox/blob/master/Core/HaloPixelToolBox.Core/Utilities/CloudLyricsReader.cs) + [CloudMusicLyricsToolPageViewModel.cs](https://github.com/XFEstudio/HaloPixelToolBox/blob/master/Client/HaloPixelToolBox.Client/ViewModels/CloudMusicLyricsToolPageViewModel.cs)
- Spotify 取词：[SpotifyLyricsReader.cs](https://github.com/XFEstudio/HaloPixelToolBox/blob/master/Client/HaloPixelToolBox.Client/Utilities/SpotifyLyricsReader.cs)

### 1.2 两种歌词来源

**(a) 网易云：读桌面歌词窗口的内存**

- 找进程 `cloudmusic`，读 PE 版本号 → 用服务端下发的 `{ModuleName, BaseAddress, Offsets[]}` 解析指针链 → `ReadMemory(addr, 200)` → 取 UTF-16 到双零字节为止。
- 每 500 ms 重解析一次地址；服务端不可达时回退本地缓存。
- 优点：拿到的是网易云**已经对齐好当前时间**的那一行，不需要自己算时间轴。
- 缺点：**版本一变就失效**，需要维护一张按版本号索引的偏移表（这就是它必须带服务端+管理端的原因）；只能拿到"当前行"，拿不到时间轴/逐字。

**(b) Spotify：SMTC + lrclib**

1. `GlobalSystemMediaTransportControlsSessionManager` 选 `SourceAppUserModelId` 含 `Spotify` 的会话；
2. `TryGetMediaPropertiesAsync()` 取标题/艺人/封面，`GetTimelineProperties()` 取 `Position`/`LastUpdatedTime`；
3. 先查本地 `Lyrics` 缓存，再打 `https://lrclib.net/api/get`、`/api/get-cached`、`/api/search`（一轮三连，带 15 s 超时、宽松标题/艺人匹配）；
4. `position = timeline.Position + (now - LastUpdatedTime)`，在 LRC 行表里二分/线性取当前行；
5. 封面主色 → 同步像素屏颜色和氛围灯颜色。

这条路线是**通用的**：Windows 上 QQ 音乐、Apple Music、foobar2000、MusicBee、PotPlayer、浏览器都能上报 SMTC。

### 1.3 主循环（值得借鉴的部分）

- 50 ms 一拍；检测「歌词变了 / 换歌了 / 颜色变了 / 暂停超时」；
- 换歌或从暂停恢复时强制刷新，并把计时器归零；
- 暂停超过 `SwitchBackTimeout`（默认 60 s）→ 切回设备内置时钟/场景；
- 歌词长于 30 个显示单位 → 先发普通布局，500 ms 后切成 `ScrollRightToLeft` 滚动布局；
- 写屏顺序：氛围灯颜色 → 像素屏颜色 → 文本布局 → 文本。

### 1.4 项目里已经踩到的坑（我们不要重踩）

| 坑 | 说明 |
| --- | --- |
| 截断用的是字符数不是字节数 | `BuildText` 只判断 `text.Length > 50`，但固件限制是 **50 个 UTF-8 字节**。实测发 51 字节会**从多字节字符中间切开**，回读得到非法 UTF-8（`0xE4 0xB8` 结尾）。中文必须按字节切。 |
| 无时间轴 | 只跟随"桌面歌词窗口"的当前行，播放器如果没开桌面歌词就读不到；做不了间奏判断、逐字、拖动进度后重定位。 |
| 指针表维护成本 | 服务端+管理端+客户端缓存，本质是为一个易碎的内存地址续命。 |
| 无连接层抽象 | `HaloPixelDevice` 直接绑定 HidSharp，没有 transport 接口，想加 BLE/SPP 得重写。 |
| 设备互斥 | 会和 TempoHub 抢设备（同一 HID 接口只能一个进程打开）。 |

---

## 2. 协议事实（本机实测 + 社区交叉验证）

### 2.1 设备与帧格式

```
主机 → 设备: 2E AA EC <cmd> <len_hi> <len_lo> <payload...> <checksum> <00 填充到 64 字节>
设备 → 主机: 2F BB EC <cmd> <len_hi> <len_lo> <payload...> <checksum> <00 填充>
校验:       从 AA 开始逐字节求和 & 0xFF（E8 的 ACK 是 2F CC EC E8 00 01 01 <sum>，从 BB 起算）
```

| 项 | 本机实测值 |
| --- | --- |
| 设备路径 | `HID\VID_2D99&PID_A160&MI_04&COL02`（另有 `COL01` 是 62 字节的 FF13） |
| usage page / usage | `0xFF14` / `0x0001` |
| 报告长度 | 输入 64、输出 64 |
| `0x65` 握手 | `02 00 00 40` → 协议版本 2、加密标志 0、最大负载 64 |
| `0xC6` 固件 | `01 01 01` → **1.1.1** |
| `0xEE` 屏幕状态 | `00 F0 B4 C8 00 01 08 00 FF 01 00 02`（**12 字节**，A160/1.1.1 特有，比老版多 3 字节"歌词叠加/动画"） |

### 2.2 命令表

| 命令 | 方向 | 用途 | 载荷 |
| --- | --- | --- | --- |
| `0x65` | 查询 | 协议握手 | 空 |
| `0xC6` | 查询 | 固件版本 | 空 |
| `0x66` / `0x67` | 查/设 | 硬件音量（0–16，与 Windows 音量独立） | `<max> <cur>` / `<target>` |
| `0x6A` / `0x6B` | 查/设 | 氛围灯 | `13 <effect> <R> <G> <B> <bright> <speed>`；社区在 A106 变体上观察到需连发 3 次、间隔 30 ms 才稳定生效 |
| `0xE7` | 查询 | 读文字 | `[source]`，`source=0` 自定义文字、`0x0E` 歌词槽 |
| `0xE8` | 设置 | 写文字 | `[source, byte_len, utf8...]` |
| `0xEE` | 查询 | 读屏幕状态 | 空 |
| `0xEF` | 设置 | 切模式/布局/颜色/动画 | 9 或 12 字节，见下 |
| `0x17` / `0xEE` / `0xEF` | 传输 | 上传 256×32 点阵/动画 | 分包协议，见 2.5 |

### 2.3 文字容量（本机实测，重要）

对 `0xE7` 的两种 source 分别查询：

```
src=0x00 (自定义文字): max=16  pkg=50
src=0x0E (歌词槽)    : max=80  pkg=50
```

实测写入边界（`0xE8` 单包）：

| 输入 | 结果 |
| --- | --- |
| 16 个 ASCII | 正常存储 16 字节 |
| 40 / 50 个 ASCII | 正常存储 40 / 50 字节 |
| 16 个汉字（48 字节） | 正常 |
| 17 个汉字（51 字节） | **被截成 50 字节，末尾字节把最后一个汉字劈成非法 UTF-8** |
| 18 个汉字（54 字节） | 同上，回读末尾是 `` |
| 20 个汉字（60 字节） | 单包放不下（帧会到 69 字节 > 64），必须换策略 |

**结论：单次写的硬上限是 50 个 UTF-8 字节；官方建议 16 字符 / 48 字节。中文 ≈ 16 字/句。**

### 2.4 布局 / 模式 / 动画（`0xEF`）

| 目标 | 载荷 |
| --- | --- |
| 静态文字 + 对齐 | `01 <R> <G> <B> 00 02 00 <align>` + `FF`（align: 0 左 / 1 中 / 2 右 / 3 两端） |
| 动态文字 | `01 <R> <G> <B> 00 02 01 <dir>` + `FF`（dir: 0 左→右 / 1 右→左） |
| 歌词叠加层 | `01 <R> <G> <B> 00 00 <enable> <0> <preset>`（本机实测 preset `0..4` 五档进出动画，会写进 `0xEE[11]`） |
| 内置场景 | `02 <R> <G> <B> 00 01 <category> <template>` + `FF`（category: 时钟 0/游戏 1/工作 2/阅读 3/猫 4/狗 5/梗图 6/赛博 7/波 8） |
| 主题色 | `03 <R> <G> <B> 00 00 FF FF FF` |

恢复快照 = `0xEE` 的 12 字节 + 两个文字槽（`0xE7` source 0 和 0x0E）。本机已验证"写完 → 恢复"闭环。

### 2.5 点阵上传（可选增强，社区项目已验证）

- 屏幕 **256×32 单色**，1 帧 = 1024 字节位图（按 4 个 page、每 page 8 行、每字节 8 像素竖排）。
- 单帧记录：`<category=3> <index_hi> <index_lo> <delay_hi> <delay_lo>` + 128 字节，共 8 条 = 1064 字节。
- 动画：1–8 帧，每帧 100–65535 ms，`category=6`。
- 流程：`0xEE` 读状态 → `0x17 [00 00 <总长4字节>]` 请求上传 → 设备回 `[00 01 <分包大小>]`（实测 53）→ 循环 `0x17 [00 02 <序号2字节> <数据>]` 并等 `[00 03]` → `0x17 [00 04]` 结束 → 等 `[00 05]` 校验完成 → `0xEF` 切到对应场景槽 → `0xEE` 核对。
- **注意**：上传会**覆盖对应场景槽的图案**，恢复官方图案需要回 TempoHub 重新发送。这是"点阵模式"必须做成可选、并明确警告的原因。
- 参考实现：[trlanfeng/OpenHaloPixelBar](https://github.com/trlanfeng/OpenHaloPixelBar)（Go，含 `internal/pixelbar/protocol.go`、`pixelbar.py`）。

---

## 3. 蓝牙可行性（证据链）

### 3.1 本机实测

- `HKLM\SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices\6c1629e07d16` 下 `CachedServices` 的 SDP 记录解码后只含：
  - `0x110B` A2DP **AudioSink**、`0x110C/0x110E` AVRCP、`0x111E` HFP（免提）
  - `0x1101` SPP（名称 `JL_SPP`，RFCOMM 通道 1）
  - 厂商 128-bit UUID `fe010000-1234-5678-abcd-00805f9b34fb`（同样命名 `JL_SPP`，RFCOMM 通道 10）
- COD = `0x240414` → 音频/扬声器；`ManufacturerId=0x05D6`（珠海杰理 JieLi）、`LmpVersion=14`（BT 5.4）。
- **没有** `BTHENUM\...` 的 HID 服务，也**没有** `BTHLE\DEV_6c1629e07d16` 节点 → 无 BLE GATT。

### 3.2 官方资料

- 官方手册（型号 EDF286015）与产品页写明：PC 端 TempoHub 需「USB 线 + 切到声卡输入（红灯）」；Q&A 明确「歌词显示仅手机/平板蓝牙模式连接支持」。
- 也就是说：**官方确实存在"蓝牙歌词"**，但走的是手机 App → 杰理 SPP 的私有协议，PC 端没有对应实现。

### 3.3 结论与可行形态

| 形态 | 可行性 | 说明 |
| --- | --- | --- |
| 纯蓝牙（不插 USB）控制屏显 | ❌ 现阶段不可行 | 需逆向 `JL_SPP` 私有协议（大概率与 USB 的 `2E AA EC` 不同，且可能带会话校验），无任何公开实现 |
| 蓝牙音频 + USB 供电/控制（双连接） | ✅ 大概率可行，待实测 | 音箱本就必须插 USB 供电（内置电池"仅做播放补强"），一根线同时给 HID；**需要验证切到蓝牙输入（白灯）后 HID 是否仍接受写屏** |
| USB 一线通 | ✅ 已验证 | USB 同时承担音频 + HID 控制 |
| 手机 App 官方蓝牙歌词 | ✅ 官方功能 | 但只能播手机上的音乐，PC 无法接入 |

> P0 必须先做的实验：把音箱切到蓝牙输入（白灯），重跑 `probe/hid_probe.py`，看 `mi_04` 的 HID 是否还在、是否仍能读写。若还在 → "蓝牙放歌 + USB 控制"成立，产品可以直接这么宣传；若不在 → 只能 USB 一线通。

---

## 4. 歌词采集层设计

### 4.1 统一抽象

```
IPlaybackSource
 ├─ SmtcPlaybackSource   (默认，覆盖所有走 SMTC 的播放器)
 └─ NeteaseMemorySource  (兜底：直接读桌面歌词窗口内存，或桌面歌词文本)
      ↓
PlaybackSnapshot { Title, Artist, Album, Duration, Position, Rate, Playing, SourceAppId, Artwork, ObservedAt }
```

### 4.2 SMTC 细节（HaloLyrics 的实现可直接抄）

- `SessionManager.SessionsChanged` 事件 + 2 s 兜底扫描；
- `MediaPropertiesChanged` / `PlaybackInfoChanged` / `TimelinePropertiesChanged` 三个事件做失效标记，避免高频 COM 调用；
- 进度外推：`pos = Position + (now - LastUpdatedTime) * Rate`；
- `PlaybackClock` 平滑：同一首歌且连续播放时，若与预测误差 < 1.25 s，只做 20% 缓修正，避免 SMTC 时间戳抖动导致歌词"回跳"；超过阈值（拖动进度）才硬跳。
- 曲目键：`Title + Artist + Album + round(Duration)`，用来判定"换歌"。

### 4.3 歌词源优先级

1. 本地缓存（`%LOCALAPPDATA%\<App>\Lyrics\<artist> - <title>.lrc`）
2. `https://lrclib.net/api/get?track_name=&artist_name=&duration=` （带 UA，遵守 429 `Retry-After`）
3. `lrclib /api/get-cached`、`/api/search`（宽松匹配：去停用词、忽略 `feat./remix/live`）
4. QQ 音乐 `music.musichallSong.PlayLyricInfo.GetPlayLyricInfo`（qrc，中文曲库覆盖好）
5. 网易云 `music.163.com/api/song/lyric`（需搜索 songId）
6. 网易云桌面歌词内存/窗口文本（无时间轴，仅"当前行"模式）
7. 都没有 → 屏幕上显示 `艺人 - 歌名`，不发歌词

> 中文歌曲在 lrclib 命中率一般，建议 P1 就把 QQ 音乐接口作为第二顺位，否则中文歌体验会很差。

### 4.4 时间轴驱动

```
tick = 100 ms（播放中）/ 500 ms（暂停）
currentLine = 最后一个 Begin <= pos 的行
若 pos >= line.End 且下一行 Begin - line.End > 0.5s → 视为间奏 → 清屏或律动
```

---

## 5. 显示渲染策略

### 5.1 策略矩阵

| 场景 | 方案 | 延迟 | 风险 |
| --- | --- | --- | --- |
| 歌词 ≤ 16 字（≤48 字节） | `0xEF` 设静态居中/左对齐 + `0xE8` 写文字（source 0 或 0x0E） | ~2 ms | 无 |
| 歌词 17–48 字 | **按行内时间切片分段**：一行切成 2–3 段，按 `(pos-Begin)/(End-Begin)` 选第几段（HaloLyrics 的做法） | ~2 ms | 段切换有跳变，可用淡入淡出动画掩盖 |
| 间奏/前奏 | 清空文字 + 切到内置"波"场景（`02 R G B 00 01 08 00 FF`）做律动；下一句开始立刻切回 | ~5 ms | 需要记住原场景用于恢复 |
| 想要自定义字体/排版 | 256×32 点阵上传（1 帧） | **实测 ≈1.2 s/帧**（8 帧 9.2 s） | 太慢，**不适合做歌词**；适合"待机画面/短循环动画"；且会覆盖场景槽 |
| 颜色跟随封面 | `0xEF 03 R G B 00 00 FF FF FF`（像素）+ `0x6B`（氛围灯） | ~5 ms | 深色封面会让文字看不见，需要亮度下限 |

### 5.2 为什么优先"原生文字通道"

- 写入 + ACK **2 ms**，100 ms 级同步完全无压力；
- 不碰场景槽，**可恢复**（本机已验证写完再恢复）；
- 固件自带字体渲染、滚动、5 档进出动画，效果比自己点阵渲染更"原生"；
- 唯一硬伤是 16 字上限——用"行内切片"绕过去，成本远低于点阵方案。

---

## 6. 系统架构

```
┌──────────────────────────────────────────────────────────────────┐
│ HaloBar.App (WPF/WinUI3 托盘 + 素材工作台)                        │
│  设置：播放器 / 歌词源优先级 / 颜色跟随 / 动画档位 / 待机画面      │
└───────────────┬──────────────────────────────────────────────────┘
                │ IPlaybackSource / ILyricsProvider / IDeviceTransport
┌───────────────┴──────────────┐   ┌───────────────────────────────┐
│ HaloBar.Media                │   │ HaloBar.Device                │
│  SmtcPlaybackSource          │   │  HidTransport (2D99:A160)      │
│  PlaybackClock (平滑)        │   │  PacketCodec (2E AA EC)        │
│  LrclibClient / QQ / Netease │   │  ScreenState (12B 快照)        │
│  LrcParser (LRC/QRC/YRC)     │   │  TextWriter (E8, ≤50B, 2 ms)   │
└──────────────┬───────────────┘   │  LayoutWriter (EF, 瞬时)       │
               │                   │  BitmapUploader (0x17, ~1.2s/帧)│
┌──────────────┴───────────────────┴───────────────────────────────┐
│ HaloBar.Content：素材工作台                                       │
│  导入/裁切/阈值/抖动 → 256×32 1bpp → 8 帧 → 上传 → 槽位记录        │
└──────────────┬───────────────────────────────────────────────────┘
┌──────────────┴───────────────────────────────────────────────────┐
│ HaloBar.Sync：屏幕仲裁器（100ms 调度）                             │
│  Idle → Lyrics(播放) → Paused → Standby(自定义画面) → Restoring    │
│  设备互斥锁（单写者）、失败不自动重试、断线重连、退出恢复           │
└──────────────────────────────────────────────────────────────────┘
```

关键工程约束：

1. **单写者**：所有 HID 写操作走一个串行队列，避免和"读状态/回读校验"交错。
2. **先快照再接管**：启动同步时保存 `0xEE` 12 字节 + 两个文字槽；暂停/退出/掉线时恢复并回读确认。
3. **失败不重试**：写失败 → 暂停同步 + 托盘提示，避免把固件写进奇怪状态（这是 OpenHaloPixelBar 的既有结论）。
4. **设备独占**：检测 TempoHub / HaloLyrics / OpenHaloPixelBar 是否在跑，提示先退出。
5. **可恢复性 > 花哨**：点阵上传做成默认关闭的高级功能。

技术栈建议：

- **生产实现**：C# .NET 8/10 + `HidSharp` + `Microsoft.Windows.SDK`（SMTC 投影，参考 HaloLyrics 的 `HaloLyrics.Media`）+ WPF 托盘。
- **PoC**：Python + ctypes（本仓库 `probe/` 已可用，零依赖）。

> 许可提醒：HaloPixelToolBox 为 MIT，可放心参考/复用；HaloLyrics 仓库**没有 LICENSE 文件**（默认保留所有权利），只能参考实现思路，不要直接拷代码；OpenHaloPixelBar 的 Python/Go 协议实现可作协议对照。

---

## 7. 需求②：自定义像素屏（官方素材现状 + DIY 能力）

### 7.1 官方到底给了什么

以官方手册（型号 EDF286015，2025 版）和本机设备为准：

| 维度 | 官方能力 | 说明 |
| --- | --- | --- |
| 个性场景 | **9 类内置场景** | 时钟、游戏、工作、阅读、猫咪、狗狗、梗图、赛博、波（`0xEF` 的 category 0–8）。每类在 App/TempoHub 里可切模板，具体模板数未公开 |
| 歌词可视化 | **仅手机蓝牙 + App 开关** | 设备预设 **5 种歌词进出动画**；官方明确"歌词显示优先级高于个性场景和自创空间"；支持中/英/日/韩 |
| 自创空间 | **只有文字** | 静态显示**限 16 个字**（与本机 `0xE7` 返回的 `max=16` 完全一致），滚动显示标称 **32 个字**；可调对齐/滚动方向；不支持图片 |
| 图片/动图 | **官方没有面向用户的上传入口** | 手册只说 PC 端 TempoHub / 手机 App "可更换或自定义像素屏的内容显示"，实际是场景模板 + 文字 |
| 按键 | 短按切换个性场景、长按开关灯效与像素屏、双击切灯效 | — |

> 关于"滚动 32 个字"：本机实测单包写入的硬上限是 **50 个 UTF-8 字节**（≈16 个汉字、50 个 ASCII 字符），32 个汉字（96 字节）单包放不下。所以 32 字大概率是按 ASCII 计，或者官方 App 用了尚未被逆向的**分包写法（`0xE7` 回报的 `pkg=50` 暗示存在分包能力）**。这是 P0 待验证项：若分包可解，歌词单行容量能从 16 字提到 32 字。

**结论：官方素材确实很少——9 个场景 + 5 个歌词动画 + 16 字文字，用户没有任何"上传自己的图"的官方途径。** 这正是社区项目（OpenHaloPixelBar、HaloStudio/pixelbardiy、pixelbar-one-look）存在的原因。

### 7.2 协议能力上限（本机实测）

| 能力 | 实测/文档值 | 备注 |
| --- | --- | --- |
| 分辨率 | 256 × 32 单色（1bpp） | 每帧 1024 字节 |
| 帧数 | 1–8 帧 | 超过 8 帧必须报错，不能静默丢帧 |
| 每帧时长 | 100–65535 ms | 低于 100 ms 会被抬到 100 ms |
| 单帧上传耗时 | **≈1.2 s**（静态 1064 B） | 设备分包 50 B/包，约 54 ms/包 |
| 2 帧动画 | **≈2.4 s** | 2128 B |
| 8 帧动画 | **≈9.2 s** | 8512 B |
| 切换已上传的场景 | **瞬时** | 只是 `0xEF` 一条指令 |
| 上传后确认 | `0x17` 逐包 `[0,3]` + 最终 `[0,5]` + `0xEE` 场景核对 | 缺一不可，失败不得显示成功 |
| 覆盖行为 | **会覆盖对应自定义场景槽** | 恢复官方图案需要回 TempoHub / App 重新发送；跨重启是否保留待验证 |

**实测记录（今天，你的这台设备）**：上传 "HALO 自定义点阵" 静态图 → `state` 变为 `01 03 06`，设备确认并显示；上传 8 帧 "动画 ●●●●●●●●" → `state` 变为 `01 06 34`，设备确认并显示；随后已切回 Waves 场景 `01 08 00`。

### 7.3 这意味着什么（产品判断）

1. **自定义像素屏 100% 可行**，而且能做的比官方多：任意图片、任意字体渲染的文字、8 帧像素动画、随时切换。
2. **但它是"内容"而不是"实时画面"**：1.2 s/帧 的写入速度决定了它做不了 60 fps 动画、也做不了"歌词逐帧滚动"，只适合**静态画面、短循环动画、定时刷新（时钟/天气/股票，分钟级）**。
3. **和歌词是两条独立通道，可以共存**：歌词走 `0xE8`（2 ms），自定义画面走 `0x17`（秒级）；切场景瞬时 → 播放时歌词、暂停时切回自制待机动画，体验完整。
4. **必须管理"槽位"**：上传会占掉一个自定义场景槽，产品要能显示"当前槽位内容是什么、什么时候传的"，并提供"恢复默认"。

### 7.4 自定义像素屏的功能设计（建议）

**素材工作台（P2）**
- 导入 PNG/JPG/BMP/WebP/GIF（≤32 MB、≤8 帧、合计 ≤4000 万像素）；
- 处理：等比包含 / 铺满裁切 / 拉伸、阈值、反色、Floyd–Steinberg 抖动（openhalo 已验证的算法）；
- 预览：左"设备模拟屏"（1:1 像素 + 1bpp 黑白），右"原图 + 参数"；
- 导出：PNG / GIF（与上传帧完全一致，便于分享）；
- 内置主题：内置几套原创像素动画（公路旅行、海底、山水…这类 8 帧循环）作为"官方素材太少"的补位；
- 上传：逐包进度 + 最终校验 + 槽位分配。

**文字工坊（P1 就能做）**
- 任意 TTF/OTF/TTC、8–32 px，静态/左右滚动/上下移动；
- 直接走 `0xE8` 原生文字（≤16 字 / ≤48 B），超出则渲染成点阵帧上传（不受 16 字限制）。

**和歌词的联动（仲裁器）**
```
播放中      → 歌词文字通道（0xE8，2 ms 级更新）
暂停 / 空闲 → 切到已上传的自定义待机场景（0xEF，瞬时）
退出程序    → 恢复接管前的场景 + 文字（0xEE 快照 + 两个文字槽）
```

**素材来源**（解决"官方素材太少"的根本）
- 应用内置 8–12 套原创 8 帧循环动画；
- 用户导入图片/GIF；
- 在线素材包（可选，需自建 CDN 或 GitHub Release 分发）；
- "一键把当前专辑封面做成点阵"——播放时抓 SMTC 缩略图 → 抖动 → 上传，作为待机画面。

### 7.5 关键限制与风险

| 限制 | 影响 | 对策 |
| --- | --- | --- |
| 1.2 s/帧 写入 | 不能做实时动画 | 只做静态/短循环/分钟级刷新 |
| 8 帧上限 | 循环动画很短 | 设计上按"逐帧动画"而非"视频" |
| 1bpp 单色 | 照片只能看轮廓 | 抖动 + 阈值 + 裁切，配合"高对比像素风"素材 |
| 覆盖槽位不可回读 | 用户素材可能丢 | 本地保存原始帧（PNG/GIF）+ 一键重传；提示用户 |
| 与 TempoHub 抢占 | 上传失败 | 启动互斥检测 |
| 上传中途断电 | 槽位可能损坏 | 失败不重试；提示重新上传 |

---

## 8. 分阶段实施计划

| 阶段 | 内容 | 产出 | 预估 |
| --- | --- | --- | --- |
| **P0 验证** | ① 切蓝牙输入（白灯）后 HID 是否仍可用；② 播放音乐时向歌词槽（0x0E）写入是否能显示；③ 点阵上传耗时与恢复代价（已完成） | 一页结论 + 决策 | 0.5 天 |
| **P1 MVP（歌词）** | SMTC 采集 + lrclib + 原生文字 + 行内切片 + 暂停清屏 + 退出恢复 + 托盘 | 可日常使用的 exe | 2–3 天 |
| **P2 自定义画面** | 素材工作台（导入/抖动/预览/上传/槽位管理）+ 内置主题 + 暂停切待机画面 | "官方素材太少"被补上 | 2–3 天 |
| **P3 体验** | QQ 音乐歌词源、动画档位、封面主色同步、间奏律动、专辑封面→点阵、开机自启、断线重连 | 完整产品 | 2–3 天 |
| **P4 增强** | 文字工坊（任意字体点阵）、网易云内存兜底、在线素材包、多设备/多播放器仲裁 | 差异化功能 | 3–5 天 |

---

## 9. 风险清单

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 固件把"蓝牙输入"与"HID 控制"互斥 | 双连接方案失效 | P0 实测；若互斥则明确"USB 一线通" |
| 歌词槽（0x0E）无音乐播放时不接受写入 | 无法用原生歌词通道 | 已实测：空闲时写入被丢弃（`0xE7` 回读为空）；HaloLyrics 在 Apple Music 播放期间确认该槽可用（5 组动画均收到设备确认），推测与「固件认为有音频在播」有关 → 主路径改用已验证的自定义文字槽（source 0） |
| 16 字限制 | 长句体验差 | 行内切片 + 可选点阵 |
| 中文歌词在 lrclib 覆盖不足 | 无歌词 | 第二顺位接 QQ 音乐接口 |
| TempoHub 抢设备 | 打不开设备 | 启动检测 + 提示；不强行抢占 |
| 内存地址失效（网易云方案） | 取词失败 | 只作兜底；默认 SMTC |
| 点阵上传覆盖场景槽 | 用户图案丢失 | 默认关闭 + 明确警告 + 记录原槽位 |
| 协议 0xEC/0xED 变体、PID A106/A160 差异 | 兼容性 | 握手 + 版本校验 + 能力查询（`0xE7` 的 max/pkg）再决定行为 |

---

## 10. 已有实现对照（避免重复造轮子）

| 项目 | 语言 | 能力 | 对我们的价值 |
| --- | --- | --- | --- |
| [XFEstudio/HaloPixelToolBox](https://github.com/XFEstudio/HaloPixelToolBox) | C#/WinUI3 | 网易云（内存读行）+ Spotify（SMTC+lrclib）歌词、氛围灯/像素色同步 | **协议与业务逻辑的主要参考**；MIT，可复用 |
| [trlanfeng/OpenHaloPixelBar](https://github.com/trlanfeng/OpenHaloPixelBar) | Go | 256×32 图/8 帧动画上传、原生文字、应用联动、完整校验与恢复 | **点阵上传协议的唯一完整公开实现** |
| [Tianyvli/HaloLyrics](https://github.com/Tianyvli/HaloLyrics) | C#/.NET | Apple Music 原词 + 时间轴 + 5 档进出动画 + 间奏律动 + 屏幕恢复 | **最接近目标形态**；可参考架构（无 LICENSE，仅参考思路） |
| [Seraph310/halo-pixelbar-mcp](https://github.com/Seraph310/halo-pixelbar-mcp) | Python | MCP 控制灯效/文字/硬件音量（A106 变体） | 命令表补充（0x66/0x67/0x6A/0x6B） |
| [woozyc/pixelbardiy](https://github.com/woozyc/pixelbardiy) | — | HaloStudio，自绘图案/文字 | 协议文档与场景管理思路 |
| [Serius117/pixelbar-one-look](https://github.com/Serius117/pixelbar-one-look-public) | TypeScript | 把 PixelBar 当 Agent 状态灯（插件形态） | 进程内插件化、灯效映射参考 |

**空白点 = 我们的机会**：以上没有一个覆盖「网易云 / QQ 音乐 / 通用 SMTC + 中文歌词源 + 蓝牙场景说明 + 一键托盘」的完整产品。HaloLyrics 只做 Apple Music，HaloPixelToolBox 的中文取词靠易碎的内存地址，OpenHaloPixelBar 只做图案不做歌词。

---

## 11. 本次已产出的验证脚本

| 文件 | 作用 | 是否写设备 |
| --- | --- | --- |
| `probe/hid_probe.py` | 枚举 `2D99:A160` 的 3 个 HID 集合，读握手/版本/文字/屏幕状态 | 只读 |
| `probe/write_test.py` | 文字容量、静态/滚动布局、UI 场景恢复 | 会写（可恢复） |
| `probe/limit_test.py` | 50 字节边界（ASCII/中文各档） | 会写（可恢复） |
| `probe/lyric_slot_test.py` | 歌词槽（0x0E）能力查询、5 档动画 | 会写（可恢复） |
| `probe/upload_test.py` | 自定义点阵：静态图 + 8 帧动画上传、分包/确认/场景核对 | **会覆盖自定义场景槽** |
| `probe/anim_test.py` | 2 帧 / 8 帧动画上传计时，用完切回内置场景 | **会覆盖自定义场景槽** |

运行方式：`cd probe; python hid_probe.py`（Windows + Python 3；`hid_probe.py` 纯 ctypes 零依赖，上传脚本需要 `Pillow`）。

> 注意：`upload_test.py` / `anim_test.py` 会写入设备的自定义场景槽，覆盖里面原有的内容；槽内容无法回读，想恢复官方图案需要回 TempoHub / EDIFIER Connect 重新发送。执行前请确认可以接受。本次实测已完成：静态图 → `01 03 06`，8 帧动画 → `01 06 34`，最后切回 Waves `01 08 00`。