# 环境搭建与运行

本仓库有两条线，环境要求完全不同：**只想跑模拟器的话只需要 Python 3，B 部分可以整段跳过。**

| | A 部分：frbox | B 部分：NikonEmulator |
|---|---|---|
| 需要装 | Python 3（3.8+，标准库） | JDK 8 + 上游源码 + 5 处补丁 |
| 在哪 | `emu/` | `drivers/` |
| 上手时间 | 一条命令 | 编译一次几十分钟 |

---

# A 部分：frbox（纯 Python，零依赖）

## A.0 准备

不需要装任何东西。固件已经在仓库里：

```bash
data/A3000111.bin      # A 段 512 KB，未压缩
data/B3000111.bin      # B 段 7.0 MB，未压缩
```

要用别的目录的固件（比如自己从尼康升级包解出来的）：

```bash
export FRBOX_FW_DIR=/path/to/D300Update      # Windows: set FRBOX_FW_DIR=...
```

解析顺序是 `FRBOX_FW_DIR` → 仓库 `data/` → 仓库同级 `D300Update/`。

## A.1 自检（先跑这个）

```bash
cd emu
python fr_selftest.py        # CPU 核心 54 条
python frbox_selftest.py     # 整机 120 条
```

两套都应该是全绿。**自检不过时，取证脚本的结论没有意义** —— 这是这个项目自己踩过的坑。

## A.2 运行

```bash
python -m frbox -M help                                   # 列出机器
python -m frbox -M d300-a -c "bp 0xFE50C;c;info"          # A 段跑到启动链尽头
python -m frbox -M d300-b -c "bp 0x407D8;c;info"          # B 段跑到复位链尽头
python -m frbox -M d300-a -S                              # 停在 monitor 交互
python -m frbox -M d300-a -c "boottask 0x8133A:0x4616C:0:0x80506;info"
```

常用参数：

| 参数 | 作用 |
|---|---|
| `-M d300-a` / `-M d300-b` | 选机器（A 段 IO 协处理 / B 段主控） |
| `-kernel PATH` | 换固件镜像 |
| `-S` | 启动即暂停，进 monitor |
| `-c "cmd;cmd"` | 执行 monitor 命令后退出（脚本化用这个） |
| `-d in_asm,int,dev,svc,guest` | 日志类别，`-D file` 落盘 |
| `-max-steps N` | 步数上限 |
| `--clk-lock N` | 时钟寄存器 `0x482` 就绪要几个 tick（默认 8，0 = 立刻） |
| `--poke ADDR=VAL[:SIZE]` | 复位后写一次（模拟外设就绪） |
| `--latch ADDR=VAL[:N]` | 第 N 次读之后返回 VAL（模拟对方置位） |
| `--task ENTRY:SP:ARG:STOPPC` | 手工启动任务（跳过 REALOS 调度器） |
| `--itron` | 挂表驱动 μITRON 层 |

monitor 里能做什么、快照格式、分层设计，见 [`docs/03-frbox.md`](docs/03-frbox.md)。

## A.3 速度

单线程 CPython 3.13 下 30,000–43,000 步/秒。B 段复位链 191,518 步 ≈ 6 秒，
A 段 43,446 步 ≈ 1.5 秒。跑长实验（千万步级）请用快照代替重跑：

```bash
python -m frbox -M d300-b -c "bp 0x407D8;c;savevm b_base.snap"      # 存一次
python -m frbox -M d300-b -c "loadvm b_base.snap;step 100000;info"  # 之后每次从同一点开始
```

---

# B 部分：NikonEmulator 函数级（需要 JDK 8）

## 0. 你需要准备的东西

### 仓库里已经有的

```bash
data/B3000111.bin          # D300 B 段固件，7.0 MB，未压缩，直接可用
data/A3000111.bin          # A 段固件，512 KB
data/d300_b.asm            # 37 KB

# 压缩过的，用之前解压
gunzip -k data/d300_b_full.asm.gz      # 156 MB，tools/analyze_cover.py 要用它
gunzip -k data/d300_full.asm.gz        # 10.4 MB
```

### 需要自己获取的

- **上游 NikonEmulator 源码** —— 本仓库的 `drivers/` 只是补丁层，必须放进上游工程才能编译。
- （可选）从尼康官方升级包自行提取固件，用于核对 `data/` 下的两份 bin 是否一致。

## 1. 环境

- **JDK 8**（上游工程用的就是这个版本，高版本可能有兼容问题）
- **Bash**（Windows 下 Git Bash 可用）
- **Python 3** + 标准库（`tools/analyze_cover.py`，无第三方依赖）

编译依赖的 jar 在上游工程的 `lib/` 下，不需要额外下载。

## 2. 放置驱动

```
drivers/com/nikonhacker/emu/D300*.java
    → <上游>/src/main/com/nikonhacker/emu/

drivers/com/nikonhacker/emu/memory/listener/fr/Expeed7000LogListener.java
drivers/com/nikonhacker/emu/memory/listener/fr/IoCensusListener.java
    → <上游>/src/main/com/nikonhacker/emu/memory/listener/fr/
```

## 3. 打上游补丁（必须）

**不打这 5 处补丁，D300 固件跑不起来** —— 要么崩溃，要么静默失效让你误以为"固件从不访问某片内存"。

详见 [`patches/upstream-changes.md`](patches/upstream-changes.md)，摘要：

1. `AbstractInterruptController.removeEdgeTriggeredRequest()` 空实现 → 手动在中断被接受后 `removeRequest()`
2. `ExpeedPinIoListener.onLoadData8` 返回 `IoPort.getValue()` → 改用 `Pin.setInputValue()`
3. `ExpeedProxyIoListener.get()` 地址守卫 → 改为 `page == 0x0000 && (addr & 0xFFFF) >= 0x1000`
4. `FrSerialInterface.getFbyte1/2()` → 加 null FIFO 保护
5. `FrSerialInterface.setSmr()` → 放行 MD=0b001

## 4. 编译

```bash
bash tools/build_emu.sh
```

产物在 `batch_build/classes/`。

> 注意：不要用 `rm -rf batch_build` 清理后重编 —— 该目录有 1300+ 文件，会触发批量删除保护。直接覆盖重编即可。

## 5. 运行

统一命令格式（classpath 按上游 `lib/` 下的 jar 自行补齐）：

```bash
java -Xmx1g -cp "batch_build/classes;<上游 lib 下的 jar，分号分隔>" \
     com.nikonhacker.emu.<驱动> <固件> <步数> [mode] [输出...]
```

### D300Draw — 显示管线

```bash
java ... com.nikonhacker.emu.D300Draw B3000111.bin 20000000 <mode> <out>
```

- `mode 0` 基线：不干预。`0x12E674`（绘制）命中 **0** 次
- `mode 1..6` 周期性往 flag 3 的位模式里或位（1=全置 / 2=bit4 / 3=bit1 / 4=bit3 / 5=bit4+bit1 / 6=bit0）
- `mode 7, 9, 10` **每步**精确补位（`10` = 每步 `(cur & ~0x64) | 0x10`）

  > mode 1..6 是周期注入，会与任务调度产生时序竞争而失效；7/9/10 改成每步注入才稳定。

- `mode 10` 结果：绘制 **7 次**，对象 id `01/07/29/05/45/0B/10`，调用 4 个真绘制函数 `0x12E8E2` / `0x12F452` / `0x12E806` / `0x12EC98`，约 2.6 万步清空 backlog

### D300Isr — 中断注入（不打 flag 补丁）

```bash
java ... com.nikonhacker.emu.D300Isr B3000111.bin 15000000 <mode> <周期> <out>
```

| mode | 行为 |
|---|---|
| 0 | 基线 |
| 1 | 周期打 IRQ 0x1B（Serial IF 0） |
| 2 | 周期打 IRQ 0x20（EXT9） |
| 3 | 两个都周期打 |
| 5–8 | 状态字节驱动 |
| 9/10 | IRQ20 周期 + IRQ1B 由状态字节 `0x9F58DA86` 驱动 |
| 11 | IRQ1B **电平触发**（状态字节非零就一直挂着中断） |

- `mode 3` 周期 5000：管线前进三级 `0x12E162`/`0x12E1EC`/`0x12E326` 各 **1195 次**，第一道判定读 `0x10` 通过 **1195/1195**
- `mode 11` 会在 `0x12FA06` 自旋死循环（见 KNOWN_ISSUES 第 6 条）

### D300Cover — 覆盖率

```bash
java -Xmx1500m ... com.nikonhacker.emu.D300Cover B3000111.bin 20000000 <mode> <out.txt> <out.bin>
python tools/analyze_cover.py d300_b_full.asm cover0.bin
```

`mode 0` 基线 / `mode 10` 带 bit4 注入。输出覆盖率位图 + 分页统计 + 关注点清单；`analyze_cover.py` 再对位图做**调用图前沿分析**（跑了的函数里，哪个调用没被执行）。

### 其他驱动

```bash
# EXPEED 实时时钟应答模型（mode 0..4，mode 4 = 标记字节实验）
java ... com.nikonhacker.emu.D300Clock B3000111.bin 20000000 4 clock4.out

# 事件标志系统调用追踪
java ... com.nikonhacker.emu.D300Flag B3000111.bin 20000000 flagall.out

# 按键中断注入 / 剂量曲线
java ... com.nikonhacker.emu.D300Key   B3000111.bin <步数> <mode> <out>
java ... com.nikonhacker.emu.D300Press B3000111.bin <步数> <mode> <out>
```

## 6. 关于运行步数

- 20M 步 ≈ 数十秒到数分钟，取决于机器和注入模式
- 60M 步用于长跑对照
- 覆盖率统计建议 `-Xmx1500m`（位图按 7 MB 代码空间分配）

## 7. 复现结论时容易踩的坑

见 [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) 第三节（第 13–19 条）。其中最容易翻车的是：

- **CPU 地址要减 `0x40000` 才是文件偏移**
- **CPU 地址要减 `0x40000` 才是文件偏移**
- `wai_flg` 和 `set_flg` 的 **flag 号在不同寄存器里**

（第一条值得说两遍。）
