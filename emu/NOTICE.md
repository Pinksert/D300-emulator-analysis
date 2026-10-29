# NOTICE

## fr30.cpu / fr30-opc.c

这两份文件是 **GNU binutils** 的一部分（`cpu/fr30.cpu`、`opcodes/fr30-opc.c`），
由 CGEN 描述语言写成，版权归 Free Software Foundation 所有，以 **GPLv3** 发布。
此处原样引入、未作修改，只作为 `fr_emu.InsnTable` 在 import 时生成解码表的输入。

如果要把本项目以非 GPL 许可再分发，请先移除这两个文件并自行提供等价描述；
`fr_emu.py` 本身不包含它们的任何内容，只解析它们的文本。

## 其余代码

`emu/fr_emu.py`、`emu/frbox/*`、`emu/fr_*.py`、`emu/probe/*` 与 `tools/`、`drivers/`
均为本项目的分析代码。`drivers/` 下的 Java 驱动需要配合上游
[NikonEmulator](https://github.com/nikonhacker) 使用，遵循上游项目的许可。

## 固件

`data/` 下的 `A3000111.bin`、`B3000111.bin` 是尼康公司的固件镜像，**不属于本项目**。
把它们放在仓库里只是为了让所有实验可复现。使用者需自行判断所在司法辖区的合规性。
