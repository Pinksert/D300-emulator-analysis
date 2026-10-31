# D300 Firmware Emulator Analysis

把尼康 D300 相机的固件放进模拟器里跑，看它到底做了什么。

这里有两套东西，目标不同：

| | **frbox 0.10.0** | NikonEmulator 驱动 |
|---|---|---|
| 是什么 | 纯 Python 写的 **D300 专用全系统模拟器**（FR CPU + 总线 + 外设 + 中断 + 快照 + 自旋消除 + monitor） | 给上游 [NikonEmulator](https://github.com/nikonhacker) 写的 18 个实验驱动 |
| 跑什么 | D300 的 **A 段**（IO 协处理，512 KB）和 **B 段**（主控，7 MB）整机 | B 段为主，函数级打桩 |
| 要不要装东西 | 不要，标准库就行 | 要 JDK 8 + 编译上游 + 打 5 处补丁 |
| 代码 | [`emu/`](emu/)　·　设计文档 [`docs/03-frbox.md`](docs/03-frbox.md) | [`drivers/`](drivers/) |
| 什么时候用 | 想看指令级执行、中断进出、内存映射、快照回放 | 想复现 2026-10 那批显示管线/总线实验 |

**这不是一个"能开机出画面"的模拟器**，原因写在 [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md)：
固件的启动链走到某一条 `ret` 就结束了，跟模拟器完整度无关。
它是一份**逆向过程的记录**：哪些链路已经走通、卡在哪一位上、踩过哪些坑。

---

## 30 秒上手

```bash
git clone https://github.com/Pinksert/D300-emulator-analysis
cd D300-emulator-analysis/emu

python -m frbox -M help                              # 两台机器
python -m frbox -M d300-a -c "bp 0xFE50C;c;info"     # A 段跑到启动链尽头（43,446 步）
python -m frbox -M d300-b -c "bp 0x407D8;c;info"     # B 段跑到复位链尽头（191,518 步）
python -m frbox -M d300-a -c "tasks"                    # REALOS 任务表（入口 / 栈顶）
python -m frbox -M d300-a --itron-block --autoevent --autospin \
       --task 0x8133A::0:0x80506 --task-from-table --rp 0x81338 \
       -c "c 200000;info;svc"                            # 任务 1 跑完整条启动链
python -m frbox -M d300-a --realos 1 -c "c 300000;realos"
                                                        # 走固件自己的内核（V10 起）

python frbox_selftest.py     # 258/258
python fr_selftest.py        # 63/63
```

固件从仓库 `data/` 读；要用别的目录就 `FRBOX_FW_DIR=/path/to/D300Update`。
完整命令（monitor、断点、快照、日志类别）见 [`docs/03-frbox.md`](docs/03-frbox.md) §11–12。

---

## 三句话说清现状（frbox 线）

1. **两台机器都跑到了固件自己停下为止，一次没崩。** A 段 43,446 步 / 824 个不同 PC /
   0 次中断，停在 `0x000FE50C` 的 `reti`；B 段 191,518 步 / 1,239 个 PC / 1 次中断，
   停在 `0x000407D8` 的 `reti`。之后 PC 从系统栈弹出 0 —— **是固件不再往下走，不是模拟器走不动**。
2. **停下来的原因找到了，是四处空桩。** A 段启动链 `0xFC000 → 0xF8010 → 0x80044 → 0x804FA`，
   第四跳是一条孤立 `ret`（正是 REALOS `vsta_knl()` 该在的位置），main 的第二条 `call 0x80520`
   同样是 `ret`。内核对象都建好了（4 个 TCB 在 `0x00045C38`，自引用空环、状态 0），
   **只是从没 `sta_tsk` 挂上就绪队列**。这四处空桩在 1.02/1.03/1.10/1.11 四版**逐字节一致**。
3. **那就跳过调度器。** `boottask` 手工构造任务上下文跳进任务 1（`0x8133A`）：1,981 步后
   到达第一个硬件等待 `0x0DCD2C`，期间踩过 500 个应用区 PC、发过 10 次 `int #0x40`、
   自己写了 6 个中断回调槽。中断通路本身是通的 —— 48 个向量全量注入，**39 个精确 `reti`
   回原位且 `r15`/SSP 逐位复原**（`results/frbox/isr-injection.txt`）。
4. **硬件等待不再需要手抠。** `--autospin` 在运行期读轮询指令用的那个寄存器，把就绪位
   置 1 后放行：三处忙等（`0x0DCD2C` 等 `0x0AF` bit7、`0x09C97A` 等 `0x174` bit6、
   `0x0CE134` 等 `0x524` bit6）**全自动认出，含第三处地址是算出来的那种**。
5. **任务表解出来了。** `0x0FE000` 头部 + 8 条 `0x1C` 字节记录，每条给 TCB / 任务号 /
   状态 / 入口 / 栈顶。任务 1 的栈顶是 **`0x0462EC`** —— 此前一直用的 `0x04616C`
   是表头那项，不是任何任务的栈顶。真正让任务正常返回的是 **RP**：`boot_task` 原来没设，
   于是沿用了引导阶段最后一次 `call` 的 `0x080500`。补上 `rp=0x81338` 之后，任务 1 跑
   **42,442 步**回到固件自带的 `bra -1` 停机占位，不再掉到地址 0。
   （栈顶反而无关：从 `0x45000` 到系统栈 `0x7F000` 五种取值，结果完全一样。）
6. **μITRON 会阻塞了。** `--itron-block` 让 `wai_flg` 真的会等：条件不满足就把机器置为
   「阻塞」并记下标志号 / 等待位 / PC。任务 1 在 **9,841 步**停在第一个握手上
   （`wai_flg(flag 3, 0x2000)`）。用 `--autoevent` 把事件补上，它走完整条启动链：
   **56** 次服务调用、**10** 次 `sta_tsk`、13 个握手事件，最后 `sys_ext_tsk` 退出。
   结论：**任务 1 是一次性初始化任务**，不返回、不循环。

> 老线（NikonEmulator）的三句话仍然成立，且是另一层结论：B 段显示管线能自动前进三级；
> 但"绘制"只是 `set_flg(0x0A, 0x8000); RET`，不是画像素；剩下的坎是串行口波形，不是隐藏分支。

---

## 目录地图

```
D300-emulator-analysis/
├── README.md               本文件
├── KNOWN_ISSUES.md         ★ 已知问题：已修缺陷、踩坑记录、当前边界
├── BUILD.md                环境搭建（Java 线）＋ frbox 的运行方式
├── emu/                    ★ frbox 0.10.0：纯 Python 全系统模拟器
│   ├── fr_emu.py           CPU 核心（含 EIT、μITRON 陷阱）
│   ├── fr30.cpu            binutils CGEN 指令描述（解码表输入，GPL，见 emu/NOTICE.md）
│   ├── fr30-opc.c
│   ├── fr_itron.py         μITRON 半主机层
│   ├── fr_svc.py           服务表倒出
│   ├── fr_dis.py           反汇编格式化
│   ├── fr_cover.py         指令编码空间覆盖率
│   ├── fr_run.py           函数级运行器
│   ├── fr_selftest.py      54 条 CPU 断言
│   ├── frbox/              分层：bus / device / dev_clk / dev_serial / dev_timer
│   │                       / icu / machine / monitor / snapshot / trace / cli
│   ├── frbox_selftest.py   258 条断言
│   ├── frbox_boot.py       启动诊断
│   └── probe/              21 个取证脚本（见 probe/README.md）
├── docs/
│   ├── 01-architecture.md  静态结构：地址表、任务表、管线、总线协议
│   ├── 02-findings.md      各轮结论的时间线，含被证伪的假设
│   ├── 03-frbox.md         ★ 模拟器设计与命令参考
│   └── reports/            19 份报告（HTML，按时间顺序编号）
├── drivers/                18 个 NikonEmulator 驱动（Java，放上游对应目录）
├── patches/
│   └── upstream-changes.md 对上游的 5 处修改
├── tools/
│   ├── analyze_cover.py    覆盖率位图的调用图前沿分析
│   └── build_emu.sh        编译脚本
├── data/                   固件 + 反汇编（>2 MB 的已 gzip，需解压）
└── results/                原始实验输出；frbox 的在 results/frbox/
```

> **数据文件**：`data/B3000111.bin`、`A3000111.bin` 未压缩，frbox 直接就用；
> `d300_b_full.asm.gz`（156 MB 原始 / 18.5 MB 压缩）超过 GitHub 单文件上限，用前 `gunzip -k`。

---

## 关键数字

### frbox 实测（1.11 固件）

| 指标 | 值 |
|---|---|
| 可执行指令编码 | 64,682（16 位空间 65,536） |
| 未定义编码 / 未实现指令 | 854 / **0** |
| 覆盖助记符 | 122 种 |
| 执行速度 | 30,000–43,000 步/秒（CPython 3.13 单线程） |
| 自检 | 258 条（frbox）+ 63 条（CPU），全绿 |
| A 段行程 | 43,446 步 / 824 PC / 0 中断 / 停 `0xFE50C` |
| B 段行程 | 191,518 步 / 1,239 PC / 1 中断 / 停 `0x407D8` |
| 中断向量 | 48 槽 = 39 真 ISR + 8 未用（兜底 `0xFE250`）+ 1 特殊（`0x3F`） |
| 48 向量注入 | 39 个精确 `reti` 回原位，`r15`/SSP 逐位复原 |
| 任务 1 启动 | 1,981 步到第一个硬件等待 `0x0DCD2C`，500 个应用区 PC |
| 任务 1 + 自旋消除 | 3 处忙等全放行 → 4,473 个 PC / 6 个回调槽（硬件事件仍为 0） |
| REALOS 任务表 | `0x0FE000` 头 + 8 条 `0x1C` 字节记录，任务 1 入口 `0x08133A` / 栈顶 `0x0462EC` |
| 任务 1 正常返回 | 42,442 步回到固件自带的 `bra -1` 停机占位（此前掉到地址 0；关键参数是 RP，不是栈顶） |
| 任务 1 + 真内核 | 9,841 步阻塞于 `wai_flg(flag 3, 0x2000)` —— 等的是别的任务，不是硬件 |
| 任务 1 跑完整条启动链 | 35,573 步 / 56 次服务 / 10 次 `sta_tsk` / 13 个握手事件 / `sys_ext_tsk` 退出 |
| 快照体积 | B 段 191,517 步时 1,312,632 字节 |

### 老线统计（NikonEmulator，仅供对照）

| 指标 | 值 |
|---|---|
| 代码覆盖率（20M 步） | 0.144%（5138 / 3,558,418 条指令） |
| 触达函数 | 199 / 20,741 |
| 触达 4KB 页 | 36 / 1792 |
| 任务数 | 28（表 `0x00054C10`，步长 `0x1C`） |
| 事件标志对象 | 17（表 `0x000551B0`） |
| 显示对象描述符 | 70 条（表 `0x001BC020`，每条 16 B） |

---

## 两台机器

| | `d300-a` | `d300-b` |
|---|---|---|
| 角色 | IO 协处理 | 主控 / UI |
| ROM | `0x00080000` + 524,290 B | `0x00040000` + 7,340,032 B |
| RAM | `0x00001000–0x0007FFFF` | `0x00001000–0x0003EFFF` + `0x9F580000` 1 MB |
| TBR / SSP | `0x000FFC00` / `0x0007F000` | `0x000EFC00` / `0x0003F000` |
| 复位入口 | `0x000FC000` | `0x00040000` |

数字来源全部是固件自身：ROM 基址与长度 = 镜像大小；TBR = 向量槽里读出复位入口的那个基址；
RAM 上界 = 固件真正写过的高地址。`0x9F580000` 那块 1 MB 是"未映射写 6,000 多次"逼出来的。

**别忘了**：上面是 **CPU 地址**，读固件文件时 B 段要减 `0x40000`、A 段减 `0x80000`。

---

## frbox 与 QEMU 的对照

| QEMU | frbox |
|---|---|
| `qemu-system-x86_64 -M pc` | `python -m frbox -M d300-b` |
| `-kernel vmlinuz` | `-kernel B3000111.bin` |
| `-S` | `-S` |
| `-d in_asm,int -D log.txt` | `-d in_asm,int -D log.txt` |
| `-monitor stdio` / `-serial stdio` | 同名 |
| `savevm` / `loadvm` | 同名（自定义格式 `FRBOXSNP`） |
| `info mtree` / `info registers` | 同名 |

| `-cpu max,check` 之类 | `--autospin`（自动放行「等硬件就绪」忙等） |

没有做的：gdbstub、多线程 TCG、设备热插拔。原因和取舍见 [`docs/03-frbox.md`](docs/03-frbox.md) §14。

---

## 解出来的东西（速查）

完整版在 [`docs/01-architecture.md`](docs/01-architecture.md)，这里只列最常用的（B 段 CPU 地址）：

```
B 段基址           0x00040000      固件 7 MB
空闲任务           0x000407DC
μITRON dispatch    0x00040668      服务号 = R12 的低 8 位（负数）
中断向量表 TBR     0x000EFC00      向量 n 在 TBR + (255-n)*4  ← 倒序
任务表             0x00054C10      28 条 × 0x1C
flag 对象表        0x000551B0      17 条 × 16 B
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

A 段（frbox 线）常用：

```
启动链             0xFC000 → 0xF8010 → 0x80044 → 0x804FA(空 ret) → 0xFE426
BSS 清零           0x80506         （0x3F000 → 0x4592C，6,731 字）
任务表             0xFE00C         任务 1 入口 0x8133A
TCB                0x45C38 起 4 个，步长 0x10，自引用空环
中断回调槽         0x3F01C + (vec-0x10)*4
时钟/复位组        0x482 / 0x483 / 0x484
48 个 ICR          0x440–0x46F，级别 0x12–0x1F
```

---

## 老线：NikonEmulator 驱动（18 个）

```bash
gunzip -k data/d300_b_full.asm.gz data/d300_full.asm.gz
# 把 drivers/ 下的文件放进上游对应包目录，按 patches/upstream-changes.md 打 5 处补丁
bash tools/build_emu.sh
java -Xmx1g com.nikonhacker.emu.D300Draw B3000111.bin 20000000 10 draw10.out   # 补 bit4：绘制 7 个对象
java -Xmx1g com.nikonhacker.emu.D300Isr  B3000111.bin 15000000 11 5000 isr11.out
java -Xmx1500m com.nikonhacker.emu.D300Cover B3000111.bin 20000000 0 cover0.txt cover0.bin
```

| 驱动 | 用途 |
|---|---|
| `D300Run` / `D300Full` | 最小骨架 / 带外设的完整建模 |
| `D300Idle` | 越过空闲任务（修了 4 个缺陷才跑通） |
| `D300Sys` / `D300Flag` | μITRON 系统调用 / 事件标志系统调用追踪 |
| `D300UI` / `D300Write` | 界面文字区访问 / 内存写入追踪 |
| `D300Go` / `D300Exp` / `D300Stream` | EXPEED 窗口录制 / `0x7000` 探针 / 总线字节流 |
| `D300Key` / `D300Press` | 按键中断注入 / 完整按键链路 |
| `D300Clock` | EXPEED 实时时钟应答模型（5 种模式） |
| `D300Draw` | ★ 显示管线驱动（mode 10 = 补 bit4） |
| `D300Cover` | ★ 代码覆盖率位图 |
| `D300Isr` | ★ 中断注入（11 种模式） |
| `Expeed7000LogListener` / `IoCensusListener` | `0x7000` 窗口透传 / I/O 普查 |

参数说明见 [`BUILD.md`](BUILD.md)。

---

## 报告索引（21 份）

01–13 是 NikonEmulator 线，14–21 是 frbox 线。

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
| 14 | `14-frbox-v2-hardening.html` | frbox V2：加固与真调用 |
| 15 | `15-frbox-v3-interrupt-kernel.html` | frbox V3：中断、向量表与 μITRON 内核 |
| 16 | `16-frbox-v4-full-system.html` | frbox V4：从函数级仿真到全系统模拟器 |
| 17 | `17-frbox-v5-clock-snapshot.html` | frbox V5：`0x482` 的真身与整机快照 |
| 18 | `18-frbox-v6-interrupt-system.html` | frbox V6：A 段中断系统全解与 48 向量注入 |
| 19 | `19-frbox-v7-app-startup.html` | frbox V7：让 A 段应用区跑起来 |
| 20 | `20-frbox-v8-autospin.html` | frbox V8：中断闭环与自旋消除器 |
| 21 | `21-frbox-v9-task-table-boot.html` | frbox V9：任务表解密与 μITRON 半主机 |
| 22 | `22-frbox-v10-native-realos.html` | frbox V10：固件自带的 REALOS 第一次真的跑起来了 |

---

## 引用与合规

如果你用到了这里的结论，请同时引用上游项目 NikonEmulator。

`emu/fr30.cpu` 与 `emu/fr30-opc.c` 取自 GNU binutils（GPLv3），只作为解码表输入，未做修改，
许可见 [`emu/NOTICE.md`](emu/NOTICE.md)。

`data/` 下的固件二进制是尼康公司的知识产权，放在这里只为了让分析过程可复现 ——
引用前请自行判断合规性。
