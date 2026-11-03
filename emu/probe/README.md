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
| `exp_realos_sched.py` | 就绪位图 / 就绪队列 / 派发请求三张表 + 0x3F4–0x3FC 优先级编码器 + ILM 约定 + 100 万步切换时间线 | V11 全篇（6 次切换） |
| `exp_realos_flow.py` | 挂被动钩子记录 14 次服务调用，读出死锁现场「谁在等谁」 | V12 §3 |
| `exp_sta_map.py` | 带数据流的递归下降：122 处 `sta_tsk`、ISR→回调→任务可达图、ICR 0x1F 判决 | V12 §4/§6 |
| `exp_irq_wake.py` | 死锁现场 48 向量逐个点火，判「哪条中断能推动系统」 | V12 §5 |
| `exp_native_drive.py` | 用固件自己的内核 `set_flg` 注入事件，按贪心策略一路推 | V12 §8 |
| `exp_wait_kind.py` | 状态字 ↔ 该任务最后一句系统调用，逐项钉死「等标志/等消息/休眠」 | V13 §1–2 |
| `exp_events.py` | 挂「完成位+中断」事件源，扫 0/1024…16384 周期看系统走多远 | V13 §4–5 |

跑之前建议先 `python frbox_selftest.py`：如果自检不过，取证脚本的结论没有意义。

### V14：事件位图线

| 脚本 | 做什么 | 出处在 |
|---|---|---|
| `exp_slot_profile.py` | 找出被写过的 `0x3F0xx`、反查「槽 → 向量」、反汇编回调并**跟寄存器**抽位判定 | V14 §2（五个槽的画像表） |
| `exp_evt_bitmap.py` | 全段扫描谁碰 `0x41A56`，逐处给上下文；跑一段看位图实际值 | V14 §3（13 处；`0x0AD662` 转发链） |
| `exp_evt_matrix.py` | 13 种位图位组合 × 100 万步，看谁能拉起任务 8 | V14 §4（只有 bit4+bit8 有效） |
| `exp_dp_bit.py` | 直接页 `0x90` 逐位扫描，找真正放行任务 8 的那一位 | V14 §5（只有 bit1） |
| `exp_flag_writer.py` | 给 10 个 flag 控制块挂写监视，抓「位模式到底谁写的」 | V14 §6（只有内核 `0x0FED42` / `0x0FEDD6`） |

### V15：覆盖率与位掩码

| 脚本 | 做什么 | 出处在 |
|---|---|---|
| `exp_bitmask.py` | 把五个回调的判定按「掩码语义」翻译成「测的是哪几位」 | V15 §1 |
| `exp_bit_matrix.py` | 位图 16 位逐位单喂 + 不喂，复算 V14 的结论 | V15 §2（结论：不成立） |
| `exp_coverage.py` | 四场景覆盖率、未执行区间、13 个任务入口是否被执行 | V15 §3（`results/frbox/coverage.txt`） |
| `exp_ready_queue.py` | 就绪位图 / 13 个优先级槽 / TCB 对表 | V15 §4 |
| `exp_task4_unblock.py` | 0x90 逐位 vs 任务 4 是否真被派发 | V15 §4（2,764 → 6,382） |

### V16：任务启动与轨迹

| 脚本 | 做什么 | 出处在 |
|---|---|---|
| `exp_task_boot.py` | 单任务对照：每次只把 1 个 dormant 任务的「复位就激活」位置 1，看固件自己建不建 | V16 §2（7 个全都能起来；全开时 3 个被压住） |
| `exp_unlock_chain.py` | 逐任务轨迹：跑一段→看它在等哪个 flag 的哪几位→用内核置位→再跑 | V16 §3（`unlock-chain.txt` / `unlock-live.txt`） |
| `exp_all13.py` | A 原始 / B 全开 / C 全开+解锁 三场景总账 | V16 §2（`all13.txt`：5 → 9 → 11 / 12） |
