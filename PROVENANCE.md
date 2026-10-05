# 来源与改动范围

- PIONEER：`Kamie1103/PinyRadioV2`，提交 `460c674795b95d7e164f559bdf53f785e20765e6`。本包 `upstream/rm_2fsk_decoder.py` 是此前读取并验证的未修改原版，Git blob SHA 为 `dc222cee9401a74066228a4cc503295ae22e784b`；仓库 GPL 许可证完整保留于 `LICENSE`。
- 接收已知同步码拟合思路来自用户 Stage G 接收参考；本版按实例重写，避免模块全局 SPS 和 TX profile 导入校验。Gaussian 系数与此前成功的实测包装流程一致。
- `stage_o_patched_rx.py` 中 CRC 算法、A5 滑动重组逻辑与 PIONEER 相容，新增参数解耦、向量化接收、连续块上下文、断链处理、统计、文件与 Pluto 输入。
- `stage_o_gnuradio.py` 保留 PIONEER `frame_out` 消息格式，但流输出改为诊断软电平与原鉴频 Hz。原二进制 bit 输出接口、原双路的动态 setter 不是本 block 的接口；使用随附完整入口或按指南接到 1 MS/s INFO lane。
- 原上传 Stage F/G/H ZIP、原 Stage O 仓库及冻结参考源码未被覆盖。交付为独立接收后端、可执行单机脚本和 GNU 回放/验收模块。
- 实测 fixture 为用户录波切片；`fixtures/golden_*_frames.jsonl` 为此前真实回放参考。它们仅用于验收；正式接收流程不读取或替换为预期载荷。
- GNU适配 API、GUI构造和 GRC 参数类型参照 GNU Radio maint-3.10 官方源码/文档；平台未安装该运行时，执行状态如 `validation/` 所列。
