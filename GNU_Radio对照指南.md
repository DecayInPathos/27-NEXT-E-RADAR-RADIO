# GNU Radio：与统一核心一致的回放和诊断

目标 GNU Radio 3.10 / Ubuntu 22.04。提供可执行 `stage_o_gr_replay.py`、适配模块 `stage_o_gnuradio.py`、三个 `.block.yml` 与完整 `StageO_IQ_Replay.grc`。这些组件已做语法、结构和无 GNU 依赖的输出传输测试；本沙箱无 GNU 运行时，调度器/Qt 编译运行需按下述命令在主机验收。

## 1. 环境与打开图形界面

```bash
sudo apt install gnuradio python3-numpy python3-scipy python3-matplotlib python3-pyqt5
python3 -c 'from gnuradio import gr, qtgui; import numpy, scipy'
bash run_grc.sh
```

使用安装 GNU Radio 的系统 Python；不要在缺少 GNU Radio 的虚拟环境中仅安装同名非官方 pip 包。无需编译旧 gr-piny 模块，因为本包直接提供 Python GNU block。

`run_grc.sh` 会设置 `PYTHONPATH` 与 `GRC_BLOCKS_PATH`；若手动从其他位置打开 Companion，应保留同样的路径。默认 GRC 图回放本包的真实 250 ms 切片，变量 `iq_file` 可改为完整 `.c64` 的绝对路径。

## 2. 实际连接与参数

| 顺序/分支 | GNU block | 核心参数 |
|---|---|---|
| IQ 输入 | Stage O IQ File Source | little-endian complex64，repeat=false，最后一个样本带 `rx_eof` 标签 |
| GUI 限速 | Throttle | complex，1 MS/s；离线最快验收省略该块 |
| 统一接收 | Stage O Patched RX | input/lane Fs=1e6，SPS=`47` 或 `auto`，CFO=`None` 自动捕获，cutoff=60000，chunk=0.5，tracking=true |
| 输出 0 → 眼图 | QT GUI Eye Sink | float，Fs=1e6，SPS=47，size=6016，Y=-2.5..2.5，tag trigger=`symbol_center`，trigger delay=47/Fs |
| 输出 0 → 软符号 | Stage O Recovered Symbol Sampler | 仅在解调核心恢复的符号中心采样，输出 complex(real=软电平, imag=0) |
| 软符号 → 图形 | QT GUI Constellation Sink | X=-2.5..2.5，Y=-0.25..0.25，size=1024 |
| 输出 1 → 时间图 | QT GUI Time Sink | float，Fs=1e6，显示原始鉴频 Hz |
| 已校验消息 | `frame_out` | PIONEER 兼容 PMT pair：cmd_id/seq/source_sample 元数据 + payload uint8vector |
| 完整诊断消息 | `events` | JSON，含完整帧字节、CRC、字段、样本索引及软件延时 |

`StageOPatchedRxBlock` 内部顺序是 CFO 反向混频 → 257 taps Hamming FIR → 相位差鉴频 → Gaussian 匹配滤波 → 全相位捕获/每包同步拟合 → 载荷判决 → PIONEER 兼容重组和 CRC。捕获阶段会先做宽带同步探测，验证后重新处理同一窗，因此不丢掉用于捕获的首窗有效数据。

它的 GUI 输出和独立脚本来自同一个 `StreamingReceiver`，参数相同且输入未更改时应产生相同帧与诊断样本；验收脚本实际检查哈希，不只肉眼比较图形。眼图用符号标签触发；软符号采样器同样使用这些标签，避免简单 Keep 1 in N 因相位或钟差改变而取错点。

GFSK 原始 RF IQ 通常呈旋转相位轨迹，不能要求它像 BPSK 那样只有两点。本图的“星座”是**鉴频后判决平面**，虚部为零；成形/ISI 会形成多组软电平。没有把收到的数据硬映射成 ±1 来伪造完美双点图。

## 3. 固定参照与自动捕获

70 秒实测参照配置：

```bash
python3 stage_o_patched_rx.py --iq radio_logs/aux_info_usb_20260804_163352_456673.c64 --sps 47 --cfo-hz 32760.823911734893 --no-track --output fixed_cli
python3 stage_o_gr_replay.py --iq radio_logs/aux_info_usb_20260804_163352_456673.c64 --sps 47 --cfo-hz 32760.823911734893 --no-track --output fixed_gnu --reference-summary fixed_cli/summary.json --verify-traces
```

对应此前 6695 个物理分片、3662 次 CRC 通过帧。4 秒实测的固定 CFO 为 `541.1274933700939` Hz，参考 392 分片、217 帧。

自动捕获的相同输入验收更简单：

```bash
bash validate_gnu_on_host.sh radio_logs/aux_info_usb_20260804_163352_456673.c64
```

本次独立脚本自动模式恢复 3664 帧，略多于固定模式；不能将旧固定计数填入新自动模式的 GNU 测试记录。主机实际结果以 `gnu_comparison.json` 为准，出现不一致会退出非零。

`--verify-traces` 校验 GNU block 实际输出的两个 `.f32` 文件；缺 EOF 标签导致末尾遗漏，或输出队列错位，都会造成文件长度/哈希不匹配。使用标准 File Source 而不提供 `rx_eof` 时，block.stop 会处理剩余 CRC 帧，但停止阶段无法再向已结束的下游补发诊断流；**要求完整图形/诊断流一致性时使用本包的 EOF tagged source**。

## 4. 与原生 PinyRadioV2 模块如何对应

| 原生模块配置 | 统一核心对应/差异 |
|---|---|
| `sps=52` 硬编码，或 Stage O 的 47/52 传参 | `RxConfig.sps_candidates` 独立管理，当前录波 CRC 确认 47 |
| quad_gain 来自名义 Δf | 保留原始相位差，以同步拟合 DC/scale 归一化 |
| `frequency_offset_hz` 静态已知中心偏置 | CRC 辅助 CFO 捕获与受控跟踪，CFO 与 FSK Δf 分开 |
| `firdes.gaussian(1,sps,.35,4*sps)` | 本核心使用实测包装流程已验证的显式 taps：t=-4..4 符号，共 8*SPS+1 taps，总和归一化；不要把两组系数视为字节相同 |
| M&M / max_deviation=0.001 | 本版使用已验证的每窗全相位捕获与逐包细化；没有在 GUI 中另外叠加 M&M 改变采样时刻 |
| Access threshold=6/12 等允许错位数 | 本版软相关门限 0.80，固定物理头默认严格正确，再由 CRC 守门；门限单位不同 |
| decoder 字节池和 CRC | 保持协议兼容，加源样本定位、断链缓存清理及统计 |

如果继续使用原生 M&M 流图，应先补 CFO 和鉴频电平归一化，再重新标定 TED gain 与钟差范围；仅填 SPS47、保持名义 quad_gain 与其他原生滤波系数，不能宣称与这套脚本完全一致。本包的同核心 GNU 路径正是为避免这种差异而提供。

在现有 Stage O 中接入时，使用已正确频移/采样到 1 MS/s 的 INFO lane 作输入，以 `frame_out` 接现有上层桥接。新的 stream 输出是诊断软电平，不能继续送入旧 decoder 再按 0/1 bit 解析；需移除该重复 decoder 连接。旧双路对象的动态 `set_profile` 接口与本 block 不同，本包不是直接覆盖原类的二进制兼容补丁；单机脚本和 GRC 回放均已给完整入口。JAM/双路/原控制接口整合仍需其独立 IQ 正例，当前不以 INFO 测试替代。

## 5. 复现与结果边界

本包的无 GNU 测试确实执行了：真实字节 CRC 交叉校验、实际录波全段回放、可变输入分块、末尾收尾、输出队列逐样本相等、CRC 损坏拒绝、SPS52/镜像与宽调制阳性控制、噪声阴性控制。实际 GNU 调度器/Qt GUI 不在沙箱中，因此尚无 `gnu_radio_runtime_executed=true` 的交付记录。只有主机真实运行后的验收文件会写入该字段。

官方 API 参考：[GNU block 的 forecast/consume 接口](https://www.gnuradio.org/doc/doxygen/classgr_1_1block.html)、[Eye Sink](https://www.gnuradio.org/doc/doxygen-v3.10.9.1/classgr_1_1qtgui_1_1eye__sink__f.html)、[Symbol Sync](https://www.gnuradio.org/doc/doxygen/classgr_1_1digital_1_1symbol__sync__ff.html)。
