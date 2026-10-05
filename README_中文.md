# Stage O 修复版统一单路接收流水线

入口 **`stage_o_patched_rx.py` 是完整独立脚本**，只依赖 NumPy、SciPy；生成图片另用 Matplotlib，Pluto 接口另用 pyadi-iio/libiio。无需导入原 Stage F/G/H 或编译 PinyRadioV2 即可解码录波。

它保留 PIONEER 的反射 CRC8/CRC16 与 A5 滑动重组规则，并将接收 Fs/SPS/BT/CFO/判决电平独立管理。GNU Radio 适配器直接调用同一核心，不另写一套近似解调器。目录中的 `validation/` 是本次实际执行证据；`upstream/` 保留来源和原始 decoder，许可证见 `LICENSE`。

## 快速运行

在本目录执行：

```bash
python3 -m pip install -r requirements.txt
python3 stage_o_patched_rx.py --iq fixtures/recorded_info_250ms.c64 --output fixture_output --plots
python3 test_stage_o.py
```

`fixtures/recorded_info_250ms.c64` 是 08-02 实测录波的前 250 ms，包含真实接收字节；不是合成示例。配套 JSON 保存来源样本索引和 SHA-256。

将原始 `radio_logs_selected.zip` 解压到本目录的 `radio_logs/` 后，完整自动捕获命令是：

```bash
python3 stage_o_patched_rx.py --iq radio_logs/aux_info_usb_20260804_163352_456673.c64 --output info_163352_auto --plots
```

程序每两秒打印统计，前五条帧直接显示通信字段，所有帧写入 JSONL。要逐条打印加 `--verbose-frames`。若 ZIP 解压包含子目录，只需将 `--iq` 指向实际文件；同名 JSON 自动读取。

固定此前测得参数、复现既有回放结果：

```bash
python3 stage_o_patched_rx.py --iq radio_logs/aux_info_usb_20260804_163352_456673.c64 --sps 47 --cfo-hz 32760.823911734893 --no-track --output info_163352_fixed --plots
python3 stage_o_patched_rx.py --iq radio_logs/match_20260802_032624_557875.c64 --sps 47 --cfo-hz 541.1274933700939 --no-track --output info_0802_fixed --plots
```

自动模式不是从文件名读取 CFO：先用宽数字低通探测 SPS47/52，以真实 CRC 帧确认候选，再对齐 CFO 并尝试 60 kHz 低通；后续仅在收到有效 CRC 帧时更新 CFO。若 60 kHz 滤波会丢掉已验证的宽调制波形，则保留较宽滤波。`--cutoff-hz` 是数字低通截止频率，不能当作模拟 RF 带宽或调制频偏。

## 实时 Pluto 接收

```bash
python3 -m pip install -r requirements-pluto.txt
python3 stage_o_patched_rx.py --pluto ip:192.168.1.10 --sample-rate 1000000 --sps auto --lo 433200000 --gain 50 --rf-bandwidth 540000 --output pluto_info --seconds 30
```

也可将 `--pluto` 换成已确认属于 RX 板的物理 USB URI，例如 `usb:1.8.5`。两块板序列号相同的情况下，继续使用物理 USB 拓扑识别；不要依赖发现顺序。该模式只创建 RX 缓冲，不发送。

`PlutoIQSource` 配置并回读 Fs；`StreamingReceiver.feed(iq)` 接收任意大小的连续块；`finish()` 处理末尾未满块的数据，重复调用不会重复发帧。外部采集器如能确认丢失样本，应调用 `mark_gap(missing_samples)`，它会结束旧片段并清理跨断流重组。pyadi 的常规 RX 数据没有逐样本硬件时间戳，本脚本不虚构设备丢样计数或空口总时延。

接入已有采集器无需改解调算法：

```python
from stage_o_patched_rx import RxConfig, StreamingReceiver

received_frames = []
receiver = StreamingReceiver(RxConfig(sample_rate=1_000_000, sps_candidates=(47,52)), received_frames.append)
receiver.feed(iq_chunk)
receiver.finish()
```

以上 `iq_chunk` 是调用者已有的真实 NumPy complex64 采集块；这是 API 用法，独立脚本的文件/Pluto 两种输入已经完整实现。

## 参数冲突怎样修复

`RxConfig` 从不导入 TX profile，也不执行 Carson 带宽一致性检查。`TxNominalProfile` 作为独立对象保留名义发射参数；RX 选择 47 不会更改它的敏感度或触发原先约 4.1 kHz 的检查错误。原上传 Stage F/G/H 文件未被全局替换 SPS；其 SPS52 仍可作为原 TX 模型的基准。

| 参数 | 接收行为 | 与 TX 的关系 |
|---|---|---|
| Fs | 来自真实输入/同名元数据，当前 IQ 为 1 MS/s | 不取 TX 捕获率或默认 2.083333 MS/s |
| SPS | 当前输入域的每符号采样数；自动验证 47/52 | 不改变 TX nominal_sps |
| BT | 元数据/命令参数，当前使用 0.35 | 本批未独立测量真实发射 BT |
| CFO | 同步码拟合 DC 换算出的接收对齐估计 | 不是公式里的 FSK 正负调制偏移 Δf |
| 判决电平 | 每包拟合 `x = scale × 已知同步符号 + DC`；载荷用 `(x-DC)/scale` 判决 | 不用名义 249 kHz 归一化本批约数 kHz 的鉴频电平 |
| 数字滤波 | 宽带捕获后再 CFO 对齐/缩窄；宽调制阳性控制可回退 | 不改硬件名义 RF 带宽 |

不能对已为 1 MS/s 的录波再次做 Stage G 的 12/25 降采样。外层 2 MS/s 先抽取到 1 MS/s 的 Stage O lane 才使用 SPS47；若直接在 2 MS/s 输入域处理同一波形，应显式使用 SPS94。该单路脚本没有隐式重采样；不同输入率应保持 Fs/SPS 的物理含义一致。

DC 消除使用同步辅助拟合，保留协议合法长串比特；没有用强高通把长 0/1 当成 DC 去掉。幅度使用局部已知符号标定，输出约在判决所需量级，但不会把所有软样本硬截成 ±1。GFSK 成形及码间影响可能产生多组软电平，诊断图忠实保留它们。

## 输出与统计定义

- `frames.jsonl`：完整接收帧字节、CMD、SEQ、CRC、通信字段和源样本索引。
- `physical.jsonl`：物理头、15 字节载荷、同步拟合值、SPS 和源同步索引。物理分片本身没有独立 CRC。
- `summary.json`：帧数、吞吐、CFO、失锁、CRC 拒绝原因、时延统计及完整帧/诊断流哈希。
- `eye_symbols.npz/png`：加 `--plots` 后，从实际收到的同步/载荷生成眼图和判决平面软符号图。

终端的 `air_fps` 是 CRC 帧次 / 已处理 IQ 时长；`decode_fps` 是 CRC 帧次 / 接收核心墙钟耗时；`speed` 是 IQ 时长 / 该耗时。重复发射计为多帧次，不代表多次状态变化。

`success` 的分母是：CRC8 正确、已知长度与 CMD、完整到齐后尝试 CRC16 的逻辑帧候选。它不是无线物理包投递率，也不是 BER；CRC8 拒绝和分片间隔另列。

`processing_p95` 为单批软件处理耗时；`sample_buffer_p95` 为模型中从最后一位到右上下文可用的采样等待时间。默认 0.5 秒处理块加约 0.12 秒右上下文，因此缓冲等待可接近 0.62 秒；CPU 处理和设备缓冲还会增加时延。没有 TX/硬件时间戳，`rf_to_host_latency_ms` 明确标为不可测，不用文件录制时间冒充该指标。

更低缓冲等待可用 `--chunk-seconds 0.1`。在 4 秒实测回放中，该配置仍为 392 分片/217 帧，模型缓冲 p95 从约 581 ms 降到约 207 ms；相同短录波的 CPU 回放从约 1.30 秒增加到约 2.61 秒。完整 70 秒录波的本次基准仍使用默认 0.5 秒块。

## GNU Radio 与回归

完整说明见 `GNU_Radio对照指南.md`。支持两种使用方式：

```bash
python3 stage_o_gr_replay.py --iq fixtures/recorded_info_250ms.c64 --sps 47 --gui
bash run_grc.sh
```

`run_grc.sh` 设置本包的 Python 与 GRC 搜索路径，打开 `StageO_IQ_Replay.grc`。图中默认实测切片，改 `iq_file` 即可回放完整文件。

在装有 GNU Radio 的 Ubuntu 主机上执行真正的相同输入验收：

```bash
bash validate_gnu_on_host.sh radio_logs/aux_info_usb_20260804_163352_456673.c64
```

它先运行独立接收，再运行实际 GNU 调度器；检查包数、帧序列 SHA-256、样本数、归一化诊断流、原鉴频诊断流与符号索引哈希。还把 GNU 的真实输出流写入 `.f32`，分块计算哈希与核心结果对比。70 秒回放两路 float32 诊断流共约 560 MB，按块落盘，不读入全量数组。

本包交付时的沙箱未安装 GNU Radio，也没有连接 Pluto。已实际执行 NumPy/SciPy 全段回放、七项控制、GNU 输出传输队列测试、Python 语法和 GRC YAML 结构检查；**尚未执行 GNU 调度器、GRC 编译器、Qt GUI 或 Pluto 硬件验收**。脚本遇到缺失 GNU 运行时会明确退出，绝不以模拟模块生成通过记录。

完整实测回归：

```bash
python3 validate_recordings.py --recordings-dir radio_logs
```

真实结果与本机耗时见 `validation/full_validation.json` 和 `VALIDATION_结果.md`。回放为固定内存块，原始大 IQ 不放进交付包；小型实测切片与原始帧参照已随附。
