# D300 Firmware Emulator Analysis

把尼康 D300 相机的 **B 段主控固件**（富士通 FR 架构，富士通/ITRON/Softune 工具链）放进 [NikonEmulator](https://github.com/nikonhacker) 做函数级仿真，逐段拆开它的显示管线、事件标志系统和与 EXPEED 芯片之间的通信。

这不是一个"能开机出画面"的模拟器 —— 它跑不到那一步，原因写在 [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md)。它是一份**逆向过程的记录**：哪些链路已经走通、卡在哪一位上、以及踩过哪些坑。

> ⚠️ 先读 [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md)。那里有 19 条，其中 5 条是**必须对上游打补丁才能跑起来**的缺陷，6 条是让我得出过错误结论的方法论陷阱。

---

## 目录地图

```
D300-emulator/
├── README.md               本文件
├── KNOWN_ISSUES.md         ★ 已知问题（19 条，含已修缺陷与踩坑记录）
├── BUILD.md                环境搭建：JDK8、编译、固件提取、跑法
├── docs/
│   ├── 01-architecture.md  解出来的静态结构：地址表、任务表、管线、总线协议
│   ├── 02-findings.md      各轮结论的时间线，含被证伪的假设
│   └── reports/            13 份实验报告（HTML，按时间顺序编号）
├── drivers/                我们写的 18 个仿真驱动（放上游对应目录即可编译）
├── patches/
│   └── upstream-changes.md 对 NikonEmulator 上游的 5 处修改
├── tools/
│   ├── analyze_cover.py    覆盖率位图的调用图前沿分析
│   └── build_emu.sh        编译脚本
└── results/                筛选过的实验输出（< 400 KB/项）
```

---

## 三句话说清现状

1. **显示管线的前半段跑通了。** 只补中断、不打任何补丁，管线自动前进三级（`0x12E162` / `0x12E1EC` / `0x12E326` 各 1195 次），文本缓冲 `0x9F58D97C` 里出现了固件自己写进去的 `31 31 31`（"111"）。
2. **但"绘制"不是画像素。** `0x12F452` 全文只有 `set_flg(0x0A, 0x8000); RET`。B 段只维护状态并通知下游，真正的输出在任务 21（`0x242DA0`），还没接上。
3. **剩下的坎不是缺代码，是缺波形。** 卡点是一串串行口时序（见 `KNOWN_ISSUES.md` 第 6 条），需要的是波特率级的收发模型，不是继续找隐藏分支。

---

## 关键数字

| 指标 | 值 |
|---|---|
| 代码覆盖率（20M 步） | **0.144%**（5138 / 3,558,418 条指令） |
| 触达函数 | 199 / 20,741 |
| 触达 4KB 页 | 36 / 1792 |
| B 段固件大小 | 7 MB（CPU 基址 `0x00040000`） |
| 任务数 | 28（表 `0x00054C10`，步长 `0x1C`） |
| 事件标志对象 | 17（表 `0x000551B0`） |
| 显示对象描述符 | 70 条（表 `0x001BC020`，每条 16 B） |
| 补上 flag 3 bit4 后绘制的对象 | 7 个（id `01/07/29/05/45/0B/10`） |

---

## 解出来的东西（速查）

完整版在 [`docs/01-architecture.md`](docs/01-architecture.md)，这里只列最常用的：

```
B 段基址           0x00040000      固件 7 MB
空闲任务           0x000407DC
μITRON dispatch    0x00040668     服务号 = R12 的低 8 位（负数）
中断向量表 TBR     0x000EFC00     向量 n 在 TBR + (255-n)*4  ← 倒序
任务表             0x00054C10     28 条 × 0x1C
flag 对象表        0x000551B0     17 条 × 16 B
flag N 控制块      0x680008B8 + (N-1)*0x10    位模式在 +8
                   (flag 3 → 0x680008E0)
心跳计数器         0x5C003034

显示任务           任务 4，入口 0x12DFAE
显示管线           0x12E006 → 0x12E0F8 → 0x12E162 → 0x12E1EC
                           → 0x12E326 → 0x12E412 → 0x12E674(绘制)
描述符表           0x001BC020     +0 数据 / +4 长度 / +8 类型 / +0C 绘制函数
函数指针表         0x001BC480     25 项，落在 0x12F778..0x12FD9E
待绘制链表头       0x9F58D500     12 B 节点，+8 = id（最高位 0x80 = 脏）
当前 id            0x9F58DA82
源缓冲 / 目标缓冲   0x9F58D87C / 0x9F58D97C

B↔EXPEED 总线      位串行，不是显存
   CS              0x70000002 bit2
   CLK             0x7000000A bit1
   DATA            0x7000000A bit2
   命令字节        寄存器号 | 读标志 0x80      8 位 LSB 先
   BCD 编码表      0x001CFCA8
   BCD 解码表      0x001CFDA8
   心跳命令        0xA6，读 7 字节
```

**别忘了**：上面这些是 **CPU 地址**，读固件文件时全部要减 `0x40000`。

---

## 快速开始

```bash
# 1. 拿到上游 NikonEmulator，把 drivers/ 下的文件放进对应包目录
#    drivers/com/nikonhacker/emu/*.java              → src/main/com/nikonhacker/emu/
#    drivers/com/nikonhacker/emu/memory/listener/fr/ → 同名目录

# 2. 打上游补丁（5 处，必须打，否则跑不起来）
#    见 patches/upstream-changes.md

# 3. 编译
bash tools/build_emu.sh

# 4. 跑基线：显示管线全程 0 次绘制
java -Xmx1g com.nikonhacker.emu.D300Draw B3000111.bin 20000000 0 draw0.out

# 5. 补上 flag 3 bit4：管线跑通，绘制 7 个对象
java -Xmx1g com.nikonhacker.emu.D300Draw B3000111.bin 20000000 10 draw10.out

# 6. 不打补丁，只喂中断：管线自己前进三级
java -Xmx1g com.nikonhacker.emu.D300Isr B3000111.bin 15000000 11 5000 isr11.out

# 7. 覆盖率
java -Xmx1500m com.nikonhacker.emu.D300Cover B3000111.bin 20000000 0 cover0.txt cover0.bin
python tools/analyze_cover.py d300_b_full.asm cover0.bin
```

完整参数说明见 [`BUILD.md`](BUILD.md)。

---

## 驱动清单（18 个）

| 驱动 | 用途 |
|---|---|
| `D300Run` | 最小可跑骨架 |
| `D300Full` | 带外设的完整建模 |
| `D300Idle` | 越过空闲任务（修了 4 个缺陷才跑通） |
| `D300Sys` | μITRON 系统调用采样 |
| `D300UI` | 界面文字区访问追踪 |
| `D300Write` | 内存写入追踪 |
| `D300Go` | EXPEED 窗口录制 |
| `D300Exp` | `0x7000` 窗口探针 |
| `D300Stream` | 总线字节流 + 应答桩 |
| `D300Key` | 按键中断注入 / 剂量曲线 |
| `D300Press` | 完整按键链路注入 |
| `D300Clock` | EXPEED 实时时钟应答模型（5 种模式） |
| `D300Flag` | 事件标志系统调用追踪 |
| `D300Draw` | ★ 显示管线驱动（mode 10 = 补 bit4） |
| `D300Cover` | ★ 代码覆盖率位图 |
| `D300Isr` | ★ 中断注入（11 种模式） |
| `Expeed7000LogListener` | `0x7000` 窗口透传探针 |
| `IoCensusListener` | I/O 普查 |

---

## 报告索引（13 份）

按时间顺序，`docs/reports/` 下：

| # | 文件 | 主题 |
|---|---|---|
| 01 | `01-function-level-simulation.html` | 函数级仿真的第一次尝试 |
| 02 | `02-nikonemulator-first-run.html` | NikonEmulator 首次实跑 |
| 03 | `03-past-the-idle-task.html` | 修 4 个缺陷，越过空闲任务 |
| 04 | `04-ui-surface-and-assets.html` | UI 画面实测与素材提取 |
| 05 | `05-the-bus-to-expeed.html` | B 段与 EXPEED 之间的位串行总线 |
| 06 | `06-pressing-a-key.html` | 按键链路全解与注入实验 |
| 07 | `07-the-23-bytes.html` | 那 23 个字节是 RTC 记录区，不是命令流 |
| 08 | `08-display-pipeline-runs.html` | 显示管线解构与卡点定位 |
| 09 | `09-the-signal-comes-from-an-isr.html` | bit4 的真实来源：串行口中断 |
| 10 | `10-itron-syscall-table.html` | μITRON 系统调用表 |
| 11 | `11-itron-runtime-trace.html` | μITRON 运行时追踪 |
| 12 | `12-ui-text-across-four-bodies.html` | 四机型界面文字对照 |
| 13 | `13-cpu-architecture.html` | 处理器架构分析 |

---

## 引用

如果你用到了这里的结论，请同时引用上游项目 NikonEmulator。固件本身是尼康公司的知识产权，本仓库不包含任何固件二进制。
