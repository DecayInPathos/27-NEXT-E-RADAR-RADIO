#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Meaningful RX controls: real IQ, input partitioning, CRC rejection, mirrors.

GNU runtime is a separately executed host test, never emulated here.
"""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
import numpy as np
from scipy import signal
from stage_o_patched_rx import (RxConfig,TxNominalProfile,StreamingReceiver,
    PioneerAssembler,ACCESS_CODES,AIR_HEADER,crc8,crc16,gaussian_taps)
from stage_o_trace_transport import TraceQueue

ROOT=Path(__file__).resolve().parent


def raw_frames():
    examples={}
    for line in (ROOT/'fixtures/golden_short_frames.jsonl').read_text().splitlines():
        row=json.loads(line);examples.setdefault(row['cmd'],bytes.fromhex(row['frame_hex']))
    return [examples[f'0x{cmd:04X}'] for cmd in range(0x0A01,0x0A06)]


def run_iq(iq,config,read_size=65536):
    frames=[];batches=[]
    rx=StreamingReceiver(config,frames.append)
    for a in range(0,len(iq),read_size):batches.extend(rx.feed(iq[a:a+read_size]))
    batches.extend(rx.finish());assert rx.finish()==[]
    return rx,frames,batches


def synthetic_control(sps=47,cfo=25000,deviation=5300,mirror=False):
    stream=b''.join(raw_frames())
    assert len(stream)==135
    air=b''.join(ACCESS_CODES['info']+AIR_HEADER+stream[a:a+15] for a in range(0,len(stream),15))*3
    bits=np.unpackbits(np.frombuffer(air,np.uint8))*2.-1.
    nrz=np.repeat(bits,sps)
    shaped=signal.fftconvolve(nrz,gaussian_taps(sps,.35),mode='same')
    iq=np.r_[np.zeros(16000,np.complex64),500*np.exp(2j*np.pi*deviation*np.cumsum(shaped)/1e6),np.zeros(16000,np.complex64)]
    iq=np.asarray(iq*np.exp(2j*np.pi*cfo*np.arange(len(iq))/1e6),np.complex64)
    return np.conj(iq) if mirror else iq


class TestReceiver(unittest.TestCase):
    def test_rx_timing_does_not_change_or_validate_tx(self):
        tx=TxNominalProfile();before=tx.sensitivity
        for sps in (47,52):
            rx=RxConfig(sps_candidates=(sps,))
            self.assertEqual(rx.sample_rate,1e6)
            self.assertEqual(tx.sensitivity,before)
        self.assertNotEqual(TxNominalProfile(sps=47).sensitivity,before)

    def test_all_recorded_frames_against_original_pioneer_crc(self):
        tree=ast.parse((ROOT/'upstream/rm_2fsk_decoder.py').read_text())
        funcs=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('_crc8','_crc16')]
        namespace={};exec(compile(ast.Module(body=funcs,type_ignores=[]),'original_pioneer_crc','exec'),namespace)
        count=0
        for p in ROOT.glob('fixtures/golden_*_frames.jsonl'):
            for row in map(json.loads,p.read_text().splitlines()):
                b=bytes.fromhex(row['frame_hex'])
                self.assertEqual(crc8(b[:4]),namespace['_crc8'](b[:4]));self.assertEqual(crc8(b[:4]),b[4])
                self.assertEqual(crc16(b[:-2]),namespace['_crc16'](b[:-2]));self.assertEqual(crc16(b[:-2]),int.from_bytes(b[-2:],'little'))
                count+=1
        self.assertEqual(count,3879)

    def test_partial_header_at_fragment_tail_and_corrupted_crc(self):
        valid=raw_frames()[0];bad=bytearray(valid);bad[10]^=1
        data=b'\x11'*14+bytes(bad)+valid
        data+=b'\x00'*(-len(data)%15)
        parser=PioneerAssembler(RxConfig(sps_candidates=(47,)));out=[]
        for i,a in enumerate(range(0,len(data),15)):
            out.extend(parser.feed(dict(source_access_sample=i*216*47,payload=data[a:a+15]),47))
        self.assertEqual([f['frame_hex'] for f in out],[valid.hex()])
        self.assertEqual(parser.stats['bad_frame_crc'],1)

    def test_stream_chunk_invariance_and_gnu_trace_transport(self):
        iq=np.fromfile(ROOT/'fixtures/recorded_info_250ms.c64',dtype='<c8')
        config=RxConfig(sps_candidates=(47,),cfo_hz=541.1274933700939,cfo_tracking=False)
        first=None
        for size in (257,8192,65536):
            rx,frames,batches=run_iq(iq,config,size)
            self.assertGreaterEqual(len(frames),10)
            fingerprint=[f['frame_hex'] for f in frames]
            if first is None:first=fingerprint
            self.assertEqual(fingerprint,first)
            queue=TraceQueue();queue.append(batches);norm=[];raw=[];tags=[]
            while queue:
                a,b,c=queue.pull(997);norm.append(a);raw.append(b);tags.extend(c)
            self.assertTrue(np.array_equal(np.concatenate(norm),np.concatenate([b.normalized for b in batches])))
            self.assertTrue(np.array_equal(np.concatenate(raw),np.concatenate([b.raw_fm_hz for b in batches])))
            self.assertEqual([v[1] for v in tags],np.concatenate([b.symbol_indices for b in batches]).tolist())
            self.assertEqual(rx.stats['processed_samples'],len(iq))

    def test_automatic_cfo_sps_and_iq_mirror_controls(self):
        for sps,cfo,mirror in ((47,41000,False),(52,-11000,True)):
            with self.subTest(sps=sps,mirror=mirror):
                rx,frames,_=run_iq(synthetic_control(sps,cfo,mirror=mirror),RxConfig())
                self.assertEqual(rx.sps,sps)
                self.assertEqual(len(frames),15)
                self.assertEqual({f['payload_hex'] for f in frames},{b[7:-2].hex() for b in raw_frames()})
                self.assertLess(abs(rx.cfo-(-cfo if mirror else cfo)),1500)

    def test_wide_nominal_wave_not_forced_through_narrow_filter(self):
        rx,frames,_=run_iq(synthetic_control(47,0,deviation=248723),RxConfig())
        self.assertEqual(len(frames),15)
        self.assertEqual(rx.effective_cutoff_hz,260000)

    def test_noise_rejected_and_explicit_gap_discards_partial_payload(self):
        rng=np.random.default_rng(912)
        x=np.asarray(rng.normal(size=150000)+1j*rng.normal(size=150000),np.complex64)
        rx,frames,_=run_iq(x,RxConfig())
        self.assertFalse(frames);self.assertFalse(rx.locked)
        p=PioneerAssembler(RxConfig(sps_candidates=(47,)))
        frame=raw_frames()[0]
        self.assertFalse(p.feed(dict(source_access_sample=0,payload=frame[:15]),47))
        self.assertFalse(p.feed(dict(source_access_sample=3*216*47,payload=frame[15:30]),47))
        self.assertEqual(p.stats['packet_gaps'],1)
        with self.assertRaises(ValueError):StreamingReceiver(RxConfig()).feed(np.array([complex(float('nan'),0)]))


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(TestReceiver))
    artifact=dict(tests_run=result.testsRun,failures=len(result.failures),errors=len(result.errors),
        success=result.wasSuccessful(),gnu_runtime_installed=importlib.util.find_spec('gnuradio') is not None,
        gnu_runtime_executed=False,note='GNU scheduler/Qt GUI require separate stage_o_gr_replay.py execution; no emulation.')
    (ROOT/'validation').mkdir(exist_ok=True)
    (ROOT/'validation/unit_controls.json').write_text(json.dumps(artifact,indent=2)+'\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
