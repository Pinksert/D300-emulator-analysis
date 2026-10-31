# probe/ —— 取证脚本

这些不是模拟器的一部分，是**用来逼出结论的一次性实验**。每个脚本都对应报告里的一张表，
保留它们是为了让结论能被别人重跑，而不是只能相信文字。

统一约定：

- 都能在仓库里直接跑：`cd emu && python probe/exp_482.py`；
- 固件路径走 `FRBOX_FW_DIR` 或仓库 `data/`，没有一个脚本写死绝对路径；
- 输出打到 stdout（需要留档就 `> ../results/xx.txt`），不写别的文件；
- 判据写在 docstring 里，"什么算验证通过"必须先说清再跑。

| 脚本 | 干什么 | 结论去向 |
|---|---|---|
| `exp_482.py` | 三路证据查 `0x482`：动态 watch、静态引用扫描、跨版本比对 | V5 §2，推翻"双机握手" |
| `exp_init12.py` | 追 A 段启动里 `0xFE434–0xFE44A` 那 12 条初始化 call | V4 §3（7,198 条指令 / `int` 恒 0） |
| `exp_calltree.py` | 单步到停机，维护调用栈，出完整调用树 | V4 §4 |
| `exp_airq.py` | A 段 EIT → ISR → reti 全链路 + 回调分发两条分支 | V6 §3 |
| `exp_allisr.py` | 48 个向量全量注入，判"精确 reti 回原位 + r15/SSP 复原" | V6 §5（39 个 OK） |
| `exp_isrslot.py` | 每个 ISR 读哪个 `0x3F0xx` 回调槽 | V6 §6 |
| `exp_cbmap.py` | 反向推"回调表 0x3F000 本该填什么"（跟踪寄存器已知立即数） | V6 §7 |
| `exp_sta_tsk.py` | 全 ROM 扫 μITRON 服务调用点（A 1,000 处 / B 2,282 处） | V7 §4 |
| `exp_spin.py` | 自动找应用区所有"等硬件就绪"的轮询点 | V7 §5 |
| `exp_autospin.py` | 自旋检测 → 反汇编实际范围 → 取掩码与等待寄存器 → 自动开影子位 | V7 §6（三处同构卡点） |
| `exp_task1.py` | 手工构造任务上下文把应用区拉起来 | V7 §2 |
| `exp_task2.py` | 直接跑任务 1（`0x8133A`），看第一个硬件等待 | V7 §3 |
| `exp_task3.py` | 应用区长跑：能跑多远、覆盖率、最终卡点 | V7 §7 |
| `exp_xver.py` | 1.02 / 1.03 / 1.10 / 1.11 四版从各自复位入口解析 call 链 | V7 §8（空桩四版一致） |
| `exp_isrfire.py` | 以同一快照为基点，48 个向量逐个注入，判「回原位 + SP 复原 + 是否真进回调」 | V8 §3（5 个有真回调） |
| `exp_autospin_run.py` | 开自旋消除器把任务 1 一路跑到底，数 PC/服务调用/回调槽 | V8 §2–4（4,473 PC；V8 当时用错栈顶量到 4,512） |
| `exp_boot_seq.py` | 解 REALOS 任务表 → 旧半主机 / 真内核 / 真内核+补事件 三种跑法对照 → `sta_tsk` 明细 | V9 全篇（35,573 步 `ext_tsk` 退出） |
| `exp_sp_rp.py` | 栈顶 × RP 四种组合，看「掉到地址 0」是哪个参数造成的 | V9 §3（唯一成因是 RP 没设） |
| `exp_sp_depth.py` | RP 给对，栈顶从 0x45000 换到系统栈 0x7F000，验证栈顶不影响结局 | V9 §3.2（五种取值同一结果） |
| `exp_oldpath.py` | 四种 μITRON 挂法对照，`int #0x40` 按 opcode 计数而不是靠钩子 | V9 §6（次数不是判据） |
| `exp_boot_session.py` | 用 Monitor 脚本化跑一段启动会话，输出即 `results/frbox/boot-session.txt` | V9 §9（tasks / boottask / svc / flag / resume） |
| `exp_monitor_session.py` | 同上，`session` / `autospin` / `irq` 三种会话，分别生成三份 `*-session.txt` | V2–V8 的 monitor 实录现在都可复现 |
| `exp_111.py` | 1.10 vs 1.11 在同一段光圈驱动代码上的行为对照 | 早期报告（1.11 唯一改动 = 800mm f/5.6E） |
| `exp_order.py` | 两版从函数真入口跑，记录按时间顺序的写入 | 同上 |
| `exp_irq.py` | 用中断机制真跑 B 段 ISR（而非从函数入口硬跳） | V3 §5 |
| `exp_realos_chain.py` | 复位 → REALOS 首次派发的完整链条；任务表 13 条记录与三份初始上下文 | V10 §1–3（43,428 步） |
| `exp_realos_tcb.py` | TCB 布局（0x2C 字节）+ 13 个 TCB 的队列归属 + 任务表↔TCB 互指校验 | V10 §3 |
| `exp_realos_run.py` | 派发任务 1 后跑 30 万步：S 位五次切换、服务分布、`*(0x45980)` 只写过一次 | V10 §4–5（1,888 次陷阱） |
| `exp_realos_flgcb.py` | 服务分发表 / 对象登记表 / 标志控制块的逐字段布局 + 参数顺序的指令级证据 | V11（`@0x0FE3B8` / `@0x0FE180` / `@0x045C38`） |

跑之前建议先 `python frbox_selftest.py`：如果自检不过，取证脚本的结论没有意义。
