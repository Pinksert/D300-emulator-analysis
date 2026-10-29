frbox 线的原始输出
==================

全部由 emu/ 下的代码在 1.11 固件上直接跑出来，没有手工编辑（只把本机绝对路径替换成 <repo>）。

文件                    生成命令
----------------------  ---------------------------------------------------------------
selftest.txt            cd emu && python frbox_selftest.py        （120 条断言）
cpu-selftest.txt        cd emu && python fr_selftest.py           （54 条 CPU 断言）
boot.txt                cd emu && python frbox_boot.py --max 300000
coverage.txt            cd emu && python fr_cover.py              （65536 个编码逐个喂）
isr-injection.txt       cd emu && python probe/exp_allisr.py      （48 个向量全量注入）
session.txt             一组 monitor 命令的实录（含启动、断点、任务启动、反汇编）

关键读数
--------
A 段   43,446 步 / 824 个不同 PC / 0 次中断 / 停 0x000FE50C 的 reti
B 段  191,518 步 / 1,239 个不同 PC / 1 次中断 / 停 0x000407D8 的 reti
48 向量注入：39 个精确 reti 回注入点且 r15/SSP 逐位复原
编码空间：可执行 64,682 / 未定义 854 / 未实现 0 / 助记符 122

boot.txt 里 A 段有两种跑法（时钟自己锁 8 tick / 立刻锁定），两种都收敛到同一组 824 个 PC
—— 这是"时钟建模没有改变程序行为、只是让等待走完"的判据。
