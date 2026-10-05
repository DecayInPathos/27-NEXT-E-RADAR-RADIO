#!/usr/bin/env python
# -*- coding: utf-8 -*-

import numpy as np
from gnuradio import gr
import os
import pmt
import struct

_PHYSICAL_HEADER_BYTES = b"\x00\x0F\x00\x0F"
_PHYSICAL_HEADER_BITS = tuple(int(bit) for bit in np.unpackbits(np.frombuffer(_PHYSICAL_HEADER_BYTES, dtype=np.uint8)))
_BYTE_BIT_COUNTS = bytes(int(value).bit_count() for value in range(256))
_MAX_PHYSICAL_HEADER_BIT_ERRORS = 2
_KNOWN_DATA_LENGTHS = {6, 8, 10, 12, 24, 36}
_KNOWN_CMD_IDS = {0x0A01, 0x0A02, 0x0A03, 0x0A04, 0x0A05, 0x0A06}
_MAX_RAW_STREAM_BYTES = 4096
_PMT_FRAME_OUT = pmt.intern("frame_out")
_PMT_CMD_ID = pmt.intern("cmd_id")
_PMT_PACKET_START = pmt.intern("packet_start")

# --- CRC 8 校验 (Header 校验) ---
def _crc8(data):
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x01:
                crc = ((crc >> 1) ^ 0x8C) & 0xFF
            else:
                crc = (crc >> 1) & 0xFF
    return crc & 0xFF

# --- CRC 16 校验 (Payload 校验) ---
def _crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b & 0xFF
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
            crc &= 0xFFFF
    return crc & 0xFFFF

def _bit_errors(bits, reference_bits):
    return sum(1 for bit, ref in zip(bits, reference_bits) if int(bit) != int(ref))


def _byte_errors(data, reference):
    return sum(_BYTE_BIT_COUNTS[left ^ right] for left, right in zip(data, reference[: len(data)]))

class rm_2fsk_decoder(gr.sync_block):
    def __init__(self, mode='local', output_filename='decoded.txt', 
                 debug_filename='', symbols_filename=''):
        gr.sync_block.__init__(self, name="rm_2fsk_decoder", in_sig=[np.float32], out_sig=None)
        
        self.mode = mode
        self.output_filename = output_filename
        self.message_port_register_out(_PMT_FRAME_OUT)
        
        # --- 状态机与拼包池 ---
        self.raw_byte_stream = bytearray() # 存储剥壳后的所有 Payload 字节
        self.fragment_candidates = []
        self.max_fragment_candidates = 16
        self._reset_stats()

        self._output_file = None
        if self.output_filename:
            output_dir = os.path.dirname(os.path.abspath(self.output_filename))
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            self._output_file = open(self.output_filename, "a", encoding="utf-8", buffering=1)

    def _reset_stats(self):
        self._stats = {
            "input_bits": 0,
            "packet_start_tags": 0,
            "candidate_pruned": 0,
            "fragment_header_reject": 0,
            "fragments_ok": 0,
            "raw_stream_trim_bytes": 0,
            "dropped_noise_bytes": 0,
            "bad_data_len": 0,
            "bad_header_crc": 0,
            "bad_cmd_id": 0,
            "bad_frame_crc": 0,
            "frames_ok": 0,
        }

    def snapshot_stats(self, reset=True):
        stats = dict(self._stats)
        stats["raw_stream_bytes"] = len(self.raw_byte_stream)
        stats["active_fragment_candidates"] = len(self.fragment_candidates)
        if reset:
            self._reset_stats()
        return stats

    def work(self, input_items, output_items):
        in0 = input_items[0]
        self._stats["input_bits"] += int(len(in0))
        
        # 获取标签：packet_start 指向 Access Code 结束处
        tags = self.get_tags_in_window(0, 0, len(in0))
        tag_indices = {
            tag.offset - self.nitems_read(0)
            for tag in tags
            if tag.key == _PMT_PACKET_START
        }
        self._stats["packet_start_tags"] += int(len(tag_indices))

        for i, val in enumerate(in0):
            bit = 1 if val > 0.5 else 0
            
            # 1. 发现新物理包（分片）
            if i in tag_indices:
                self.fragment_candidates.append([])
                if len(self.fragment_candidates) > self.max_fragment_candidates:
                    self._stats["candidate_pruned"] += len(self.fragment_candidates) - self.max_fragment_candidates
                    self.fragment_candidates = self.fragment_candidates[-self.max_fragment_candidates:]

            active_candidates = []
            for candidate in self.fragment_candidates:
                candidate.append(bit)
                if len(candidate) == 32 and _bit_errors(candidate, _PHYSICAL_HEADER_BITS) > _MAX_PHYSICAL_HEADER_BIT_ERRORS:
                    continue
                if len(candidate) >= 152:
                    self._unwrap_fragment(candidate)
                else:
                    active_candidates.append(candidate)
            self.fragment_candidates = active_candidates

        return len(in0)

    def _unwrap_fragment(self, bit_collector):
        """剥离物理层 Header，将 15 字节果肉存入流水线"""
        # 将 152 比特转为 19 字节
        fragment_bytes = np.packbits(bit_collector)
        if len(fragment_bytes) >= 19:
            h = fragment_bytes
            # 物理 Header 校验：确保长度字段符合 15 字节规则
            if _byte_errors(h[:4], _PHYSICAL_HEADER_BYTES) <= _MAX_PHYSICAL_HEADER_BIT_ERRORS:
                # 提取 15 字节 Payload 放入大池子
                self.raw_byte_stream.extend(h[4:19])
                if len(self.raw_byte_stream) > _MAX_RAW_STREAM_BYTES:
                    self._stats["raw_stream_trim_bytes"] += len(self.raw_byte_stream) - _MAX_RAW_STREAM_BYTES
                    del self.raw_byte_stream[:-_MAX_RAW_STREAM_BYTES]
                self._stats["fragments_ok"] += 1
                # 尝试从池子中解析长帧
                self._parse_logic_frames()
            else:
                self._stats["fragment_header_reject"] += 1

    def _parse_logic_frames(self):
        """核心：在重组池中通过 0xA5 滑动窗口搜寻完整链路帧"""
        while len(self.raw_byte_stream) >= 9:
            # 寻找帧头 0xA5
            if self.raw_byte_stream[0] != 0xA5:
                next_sof = self.raw_byte_stream.find(b"\xA5", 1)
                if next_sof < 0:
                    self._stats["dropped_noise_bytes"] += len(self.raw_byte_stream)
                    self.raw_byte_stream.clear()
                    break
                self._stats["dropped_noise_bytes"] += next_sof
                del self.raw_byte_stream[:next_sof]
                continue
            
            # 读取长度
            data_len = self.raw_byte_stream[1] | (self.raw_byte_stream[2] << 8)
            if data_len not in _KNOWN_DATA_LENGTHS:
                self._stats["bad_data_len"] += 1
                del self.raw_byte_stream[:1]
                continue

            if _crc8(self.raw_byte_stream[:4]) != self.raw_byte_stream[4]:
                self._stats["bad_header_crc"] += 1
                del self.raw_byte_stream[:1]
                continue

            # 全帧长度 = 5(Header) + 2(ID) + Data + 2(CRC16)
            full_len = 9 + data_len
            
            # 重要：如果池子里的数据还没凑够一整帧，说明分片还没收齐，退出等待！
            if len(self.raw_byte_stream) < full_len:
                break
                
            frame = self.raw_byte_stream[:full_len]
            cmd_id = frame[5] | (frame[6] << 8)
            if cmd_id not in _KNOWN_CMD_IDS:
                self._stats["bad_cmd_id"] += 1
                del self.raw_byte_stream[:1]
                continue
            
            # 校验 CRC16 (全帧)
            recv_c16 = frame[-2] | (frame[-1] << 8)
            if _crc16(frame[:-2]) == recv_c16:
                self._process_valid_frame(frame, data_len)
                self._stats["frames_ok"] += 1
                # 成功解出一帧，从池子中移除
                del self.raw_byte_stream[:full_len]
                continue # 继续找池子里剩下的数据
            
            # 校验失败，剔除错位帧头
            self._stats["bad_frame_crc"] += 1
            del self.raw_byte_stream[:1]

    def _process_valid_frame(self, frame, data_len):
        """
        严格按照 RoboMaster 2026 V1.3.0 协议手册进行比特级解析
        """
        cmd_id = frame[5] | (frame[6] << 8)
        data_bytes = bytes(frame[7 : 7 + data_len])
        hex_data_str = " ".join([f"{b:02X}" for b in data_bytes])
        translation = ""

        try:
            # --- 0x0A01: 对方机器人位置坐标 (24 Bytes) ---
            # 12个 int16, 单位 cm
            if cmd_id == 0x0A01:
                v = struct.unpack('<hhhhhhhhhhhh', data_bytes[:24])
                translation = (f"[POS] Hero:({v[0]},{v[1]}) Eng:({v[2]},{v[3]}) "
                               f"Inf3:({v[4]},{v[5]}) Inf4:({v[6]},{v[7]}) "
                               f"Air:({v[8]},{v[9]}) Sentry:({v[10]},{v[11]})")

            # --- 0x0A02: 对方机器人血量信息 (12 Bytes) ---
            # 6个 uint16
            elif cmd_id == 0x0A02:
                v = struct.unpack('<HHHHHH', data_bytes[:12])
                translation = (f"[HP] Hero:{v[0]} Eng:{v[1]} Inf3:{v[2]} "
                               f"Inf4:{v[3]} Reserved:{v[4]} Sentry:{v[5]}")

            # --- 0x0A03: 对方机器人剩余发弹量 (10 Bytes) ---
            # 5个 uint16
            elif cmd_id == 0x0A03:
                v = struct.unpack('<HHHHH', data_bytes[:10])
                translation = (f"[AMMO] Hero17:{v[0]} Inf3_17:{v[1]} "
                               f"Inf4_17:{v[2]} Air17:{v[3]} Sentry17:{v[4]}")

            # --- 0x0A04: 对方队伍金币及宏观状态 (8 Bytes) ---
            # uint16 + uint16 + uint32
            elif cmd_id == 0x0A04:
                rem_g, tot_g, macro = struct.unpack('<HHI', data_bytes[:8])
                translation = (f"[STATS] Gold:{rem_g}/{tot_g} | "
                               f"SupplyZone:{macro & 0x01} "
                               f"CenterHighland:{(macro >> 1) & 0x03} "
                               f"TrapezoidHighland:{(macro >> 3) & 0x01} "
                               f"FortressBuff:{(macro >> 4) & 0x03} "
                               f"OutpostBuff:{(macro >> 6) & 0x03} "
                               f"BaseBuff:{(macro >> 8) & 0x01} "
                               f"TerrainCards:"
                               f"nearOppFront={(macro >> 9) & 0x01},"
                               f"nearOppBack={(macro >> 10) & 0x01},"
                               f"nearOwnFront={(macro >> 11) & 0x01},"
                               f"nearOwnBack={(macro >> 12) & 0x01},"
                               f"oppHighlandUpper={(macro >> 13) & 0x01},"
                               f"oppRampBack={(macro >> 14) & 0x01},"
                               f"oppRoadUpper={(macro >> 15) & 0x01} "
                               f"MacroBits:0x{macro:08X}")

            # --- 0x0A05: 对方机器人增益效果 (36 Bytes) ---
            # 英雄/工程/3号步兵/4号步兵/哨兵各 7 字节，最后 1 字节为哨兵姿态。
            elif cmd_id == 0x0A05:
                res = []
                names = ["Hero", "Eng", "Inf3", "Inf4", "Sentry"]
                for i, name in enumerate(names):
                    regen, cooling, defense, debuff, attack = struct.unpack_from("<BHBBH", data_bytes, i * 7)
                    res.append(
                        f"{name}[Regen:{regen}% Cool:{cooling} Def:{defense}% "
                        f"Debuff:{debuff}% Atk:{attack}%]"
                    )
                translation = "[BUFF] " + " | ".join(res) + f" | SentryPosture:{data_bytes[35]}"

            # --- 0x0A06: 密钥 (用于干扰波/自定义) ---
            elif cmd_id == 0x0A06:
                key_str = "".join([chr(b) if 32 <= b <= 126 else "." for b in data_bytes])
                translation = f"[KEY] {key_str}"

            else:
                translation = f"[UNKNOWN] ID:0x{cmd_id:04X}"

        except Exception as e:
            translation = f"[PARSE ERROR] {str(e)}"

        # 最终输出完整的日志行
        log_line = f"CmdID: 0x{cmd_id:04X} | Len: {data_len:02d} | Data: [{hex_data_str}] | {translation}"

        # 写入文件（追加模式）
        if self._output_file is not None:
            self._output_file.write(log_line + "\n")

        # 同时发送到 GRC 消息端口
        meta = pmt.make_dict()
        meta = pmt.dict_add(meta, _PMT_CMD_ID, pmt.from_long(cmd_id))
        self.message_port_pub(_PMT_FRAME_OUT, pmt.cons(meta, pmt.init_u8vector(len(data_bytes), bytearray(data_bytes))))

    def stop(self):
        if self._output_file is not None:
            self._output_file.close()
            self._output_file = None
        return True

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass
