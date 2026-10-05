#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage O single INFO/JAM lane RX: files, Pluto, and bounded streaming API.

PIONEER-compatible CRC/reassembly, metadata-rate DSP, independent RX timing,
CRC-gated CFO acquisition, per-packet DC/gain estimation. No TX profile import.
Python 3.10+, NumPy, SciPy. pyadi-iio is needed only for --pluto.
"""
from __future__ import annotations
import argparse
import collections
import dataclasses
import hashlib
import json
import math
import struct
import sys
import time
from pathlib import Path
from typing import Callable

import numpy as np
from scipy import signal

ACCESS_CODES = {'info': bytes.fromhex('2f6f4c74b914492e'),
                'jam': bytes.fromhex('16e8d377151c712d')}
AIR_HEADER = bytes.fromhex('000f000f')
KNOWN_LENGTHS = {6, 8, 10, 12, 24, 36}
KNOWN_COMMANDS = set(range(0x0A01, 0x0A07))
ROBOT_NAMES = ('hero', 'engineer', 'infantry_3', 'infantry_4', 'aerial', 'sentry')


@dataclasses.dataclass(frozen=True)
class TxNominalProfile:
    """TX model only. RX never reads this or validates Carson bandwidth."""
    sample_rate: float = 1e6
    sps: int = 52
    bandwidth_hz: float = 540000

    @property
    def sensitivity(self):
        return 2 * math.pi * (self.bandwidth_hz / 2 - self.sample_rate / self.sps) / self.sample_rate


@dataclasses.dataclass(frozen=True)
class RxConfig:
    sample_rate: float = 1e6
    sps_candidates: tuple[int, ...] = (47, 52)
    bt: float = .35
    wave: str = 'info'
    chunk_seconds: float = .5
    halo_seconds: float = .12
    acquisition_cutoff_hz: float = 260000
    locked_cutoff_hz: float = 60000
    cfo_hz: float | None = None
    cfo_tracking: bool = True
    cfo_alpha: float = .15
    access_score_min: float = .80
    header_bit_errors: int = 0
    loss_timeout_seconds: float = 2.0
    position_signed: bool = True

    def __post_init__(self):
        if not math.isfinite(self.sample_rate) or self.sample_rate <= 0:
            raise ValueError('sample_rate must be finite and positive')
        if not self.sps_candidates or any(s < 8 or s > 256 or int(s) != s for s in self.sps_candidates):
            raise ValueError('SPS candidates must be integers in 8..256 in the INPUT sample domain')
        if self.wave not in ACCESS_CODES or not .05 <= self.bt <= 2:
            raise ValueError('unsupported wave or BT')
        if not .04 <= self.chunk_seconds <= 2 or not .012 <= self.halo_seconds <= .5:
            raise ValueError('chunk_seconds must be .04..2; halo_seconds .012...5')
        if any(not 0 < f < self.sample_rate / 2 for f in (self.acquisition_cutoff_hz, self.locked_cutoff_hz)):
            raise ValueError('digital cutoffs must be inside input Nyquist')
        if self.cfo_hz is not None and (not math.isfinite(self.cfo_hz) or abs(self.cfo_hz) >= self.sample_rate / 2):
            raise ValueError('CFO must be finite and inside input Nyquist')
        if not 0 <= self.header_bit_errors <= 2 or not 0 < self.cfo_alpha <= 1:
            raise ValueError('invalid header tolerance or CFO tracking gain')


# Same reflected CRC algorithms and initial values as pinned PinyRadioV2.
def crc8(data):
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc >> 1) ^ 0x8C) & 0xFF if crc & 1 else crc >> 1
    return crc


def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def decode_payload(cmd, payload, signed=True):
    if cmd == 0x0A01:
        v = struct.unpack('<' + ('h' if signed else 'H') * 12, payload)
        return {name: list(v[2*i:2*i+2]) for i, name in enumerate(ROBOT_NAMES)}
    if cmd == 0x0A02:
        v = struct.unpack('<6H', payload)
        return dict(zip(('hero','engineer','infantry_3','infantry_4','reserved','sentry'), v))
    if cmd == 0x0A03:
        return dict(zip(('hero','infantry_3','infantry_4','aerial','sentry'),struct.unpack('<5H',payload)))
    if cmd == 0x0A04:
        a,b,c = struct.unpack('<HHI',payload)
        return dict(coins_remaining=a,coins_total=b,occupancy_status_bits=c)
    if cmd == 0x0A05:
        if len(payload) != 36:
            raise ValueError('0A05 length must be 36')
        buffs = {}
        for i,name in enumerate(('hero','engineer','infantry_3','infantry_4','sentry')):
            buffs[name] = dict(zip(('heal_pct','cooling','defense_pct','negative_defense_pct','attack_pct'),
                                  struct.unpack_from('<BHBBH',payload,7*i)))
        return dict(buffs=buffs,sentry_posture=payload[35])
    return dict(key_hex=payload.hex(),key_ascii=payload.decode('ascii',errors='replace'))


class PioneerAssembler:
    """PIONEER's byte pool/sliding A5/CRC logic, with source indices and gap reset."""
    def __init__(self, config):
        self.config = config
        self.buf = bytearray()
        self.locations = []
        self.last = None
        self.stats = collections.Counter()

    def reset(self):
        self.stats['discarded_partial_bytes'] += len(self.buf)
        self.buf.clear(); self.locations.clear(); self.last = None

    def feed(self, packet, sps):
        anchor = packet['source_access_sample']
        if self.last is not None and abs(anchor - self.last - 216*sps) > 2*sps:
            self.stats['packet_gaps'] += 1
            self.reset()
        self.last = anchor
        self.buf.extend(packet['payload'])
        self.locations.extend(anchor + (96+8*j)*sps for j in range(15))
        self.stats['physical_fragments'] += 1
        frames = []
        cursor = 0
        while len(self.buf)-cursor >= 9:
            pos = self.buf.find(b'\xa5',cursor)
            if pos < 0:
                self.stats['dropped_noise_bytes'] += len(self.buf)-cursor
                cursor = len(self.buf); break
            self.stats['dropped_noise_bytes'] += pos-cursor
            cursor = pos
            if len(self.buf)-pos < 9:
                break  # keep a trailing SOF/header until the next fragment
            length = int.from_bytes(self.buf[pos+1:pos+3],'little')
            if length not in KNOWN_LENGTHS:
                self.stats['bad_data_len'] += 1; cursor += 1; continue
            if crc8(self.buf[pos:pos+4]) != self.buf[pos+4]:
                self.stats['bad_header_crc'] += 1; cursor += 1; continue
            end = pos+9+length
            if end > len(self.buf):
                break
            raw = bytes(self.buf[pos:end])
            cmd = int.from_bytes(raw[5:7],'little')
            if cmd not in KNOWN_COMMANDS:
                self.stats['bad_cmd_id'] += 1; cursor += 1; continue
            self.stats['crc16_attempts'] += 1
            if crc16(raw[:-2]) != int.from_bytes(raw[-2:],'little'):
                self.stats['bad_frame_crc'] += 1; cursor += 1; continue
            first,last = self.locations[pos],self.locations[end-1]
            event = dict(frame_hex=raw.hex(),payload_hex=raw[7:-2].hex(),
                         cmd=f'0x{cmd:04X}',seq=raw[3],crc8_ok=True,crc16_ok=True,
                         source_first_payload_sample=first,source_last_payload_sample=last,
                         source_frame_end_sample_exclusive=last+7*sps+1,
                         time_seconds=first/self.config.sample_rate)
            try:
                event['decoded'] = decode_payload(cmd,raw[7:-2],self.config.position_signed)
            except (struct.error,ValueError) as exc:
                event['decode_error'] = str(exc)
            frames.append(event)
            self.stats['crc_frames'] += 1
            cursor = end
        if cursor:
            del self.buf[:cursor]; del self.locations[:cursor]
        if len(self.buf) > 4096:
            extra = len(self.buf)-4096
            self.stats['raw_stream_trim_bytes'] += extra
            del self.buf[:extra]; del self.locations[:extra]
        return frames


def gaussian_taps(sps, bt):
    t = np.arange(-4*sps,4*sps+1,dtype=float)/sps
    sigma = np.sqrt(np.log(2))/(2*np.pi*bt)
    h = np.exp(-t*t/(2*sigma*sigma))
    return h/(h.sum()+1e-15)


class Detector:
    """Vectorized version of the verified Stage G phase/known-access fit method."""
    def __init__(self, config):
        self.config = config
        self.symbols = np.unpackbits(np.frombuffer(ACCESS_CODES[config.wave],np.uint8))*2.-1.
        self.mean = self.symbols.mean()
        self.centered = self.symbols-self.mean
        self.energy = np.dot(self.centered,self.centered)
        self.cache = {}

    def frontend(self, iq, start, sps, cfo, cutoff):
        key=(sps,cutoff)
        if key not in self.cache:
            self.cache[key]=(signal.firwin(257,cutoff,fs=self.config.sample_rate),gaussian_taps(sps,self.config.bt))
        taps,gauss=self.cache[key]
        x=iq
        if cfo:
            x=np.asarray(iq*np.exp(-2j*np.pi*cfo*(np.arange(len(iq),dtype=float)+start)/self.config.sample_rate),np.complex64)
        y=np.asarray(signal.fftconvolve(x,taps,mode='same'),np.complex64)
        raw=np.angle(y[1:]*np.conj(y[:-1]))
        # Phase is undefined at zero amplitude. Suppress numerical FFT residue
        # in true zero padding; this is a -120 dB amplitude guard, not a burst gate.
        magnitude=np.abs(y)
        floor=max(float(magnitude.max(initial=0))*1e-6,1e-20)
        raw=np.where((magnitude[:-1]>floor)&(magnitude[1:]>floor),raw,0.)
        mf=signal.fftconvolve(raw,gauss,mode='same')
        return raw,mf

    def fit_many(self, values):
        mean=values.mean(axis=-1)
        scale=np.einsum('...k,k->...',values,self.centered)/self.energy
        dc=mean-scale*self.mean
        valid=np.abs(scale)>1e-10
        safe=np.where(valid,scale,1.)
        norm=(values-dc[...,None])/safe[...,None]
        cov=np.einsum('...k,k->...',norm,self.centered)
        power=np.sum((norm-norm.mean(axis=-1)[...,None])**2,axis=-1)
        score=cov/np.sqrt(np.maximum(power*self.energy,1e-30))
        mse=np.mean((norm-self.symbols)**2,axis=-1)
        quality=np.where(valid,score-.10*mse,-np.inf)
        return scale,dc,score,quality

    def detect(self, iq, start, sps, cfo, cutoff):
        raw,mf=self.frontend(iq,start,sps,cfo,cutoff)
        best=None
        pattern=np.arange(64)*sps
        for offset in range(sps):
            soft=mf[offset::sps]
            if len(soft)<216:
                continue
            corr=signal.correlate(soft,self.symbols,mode='valid',method='fft')
            anchor=offset+int(np.argmax(np.abs(corr)))*sps
            scale,dc,score,q=self.fit_many(mf[anchor+pattern])
            if best is None or q>best[0]:
                best=(float(q),offset)
        if best is None:
            return [],raw,mf
        offset=best[1]
        corr=signal.correlate(mf[offset::sps],self.symbols,mode='valid',method='fft')
        peaks,_=signal.find_peaks(np.abs(corr),distance=64)
        if not len(peaks):
            return [],raw,mf
        starts=offset+peaks[:,None]*sps+np.arange(-8,9)[None,:]
        legal=(starts>=0)&(starts+63*sps<len(mf))
        indices=np.clip(starts[:,:,None]+pattern,0,len(mf)-1)
        scales,dcs,scores,qs=self.fit_many(mf[indices])
        qs=np.where(legal,qs,-np.inf)
        choice=np.argmax(qs,axis=1);rows=np.arange(len(peaks))
        chosen=starts[rows,choice]
        scales=scales[rows,choice];dcs=dcs[rows,choice];scores=scores[rows,choice]
        ok=(scores>=self.config.access_score_min)&(chosen+64*sps-3>=0)&(chosen+215*sps+3<len(mf))
        packets=[]
        body_centers=np.arange(64,216)*sps
        avg_offsets=np.arange(-3,4)
        for anchor,scale,dc,score in zip(chosen[ok],scales[ok],dcs[ok],scores[ok]):
            soft=(mf[anchor+body_centers[:,None]+avg_offsets].mean(axis=1)-dc)/scale
            body=np.packbits(soft>0).tobytes()
            errors=sum((a^b).bit_count() for a,b in zip(body[:4],AIR_HEADER))
            if errors<=self.config.header_bit_errors:
                packets.append(dict(source_access_sample=start+int(anchor),
                    payload=body[4:],air_header_hex=body[:4].hex(),access_score=float(score),
                    dc=float(dc),scale=float(scale),sps=sps,
                    sync_fitted_cfo_hz=cfo+float(dc)*self.config.sample_rate/(2*np.pi)))
        return sorted(packets,key=lambda p:p['source_access_sample']),raw,mf


@dataclasses.dataclass
class RxBatch:
    start_sample: int
    sps: int
    normalized: np.ndarray
    raw_fm_hz: np.ndarray
    symbol_indices: np.ndarray
    frames: list
    packets: list


class StreamingReceiver:
    """feed() accepts arbitrary contiguous chunks; finish() drains the final tail.

    Processing partitions do not depend on caller chunk sizes. At most a block
    plus left/right context is retained. Missing samples require mark_gap().
    """
    def __init__(self, config, on_frame: Callable | None=None):
        self.config=config;self.on_frame=on_frame
        self.detector=Detector(config);self.assembler=PioneerAssembler(config)
        self.chunk=round(config.chunk_seconds*config.sample_rate)
        self.halo=max(round(config.halo_seconds*config.sample_rate),216*max(config.sps_candidates)*10)
        self.buffer=np.empty(0,np.complex64);self.buffer_start=0;self.next_start=0;self.received=0
        self.sps=config.sps_candidates[0];self.cfo=config.cfo_hz or 0.
        self.effective_cutoff_hz=config.locked_cutoff_hz
        self.locked=config.cfo_hz is not None and len(config.sps_candidates)==1
        self.finished=False;self.last_crc_sample=0;self.last_physical=-10**12
        self.stats=collections.Counter();self.counts=collections.Counter()
        self.started=time.perf_counter();self.processing_ms=[];self.buffer_latency_ms=[]
        self.first_frame_wall=None;self.frame_hash=hashlib.sha256()
        self.normalized_hash=hashlib.sha256();self.raw_trace_hash=hashlib.sha256();self.centers_hash=hashlib.sha256()
        self.lock_events=[];self.last_diagnostic=None

    def _probe(self, packets,sps):
        assembler=PioneerAssembler(self.config)
        return sum(len(assembler.feed(p,sps)) for p in packets)

    def _process(self,a,b):
        t=time.perf_counter()
        left=max(self.buffer_start,a-self.halo);right=min(self.received,b+self.halo)
        iq=self.buffer[left-self.buffer_start:right-self.buffer_start]
        if not self.locked:
            choices=[]
            for sps in self.config.sps_candidates:
                result=self.detector.detect(iq,left,sps,self.config.cfo_hz or 0.,self.config.acquisition_cutoff_hz)
                crc=self._probe(result[0],sps)
                choices.append((crc,len(result[0]),sps,result))
            crc,_,sps,result=max(choices,key=lambda c:(c[0],c[1],-self.config.sps_candidates.index(c[2])))
            self.sps=sps
            if crc:
                self.cfo=float(np.median([p['sync_fitted_cfo_hz'] for p in result[0]])) if self.config.cfo_hz is None else self.config.cfo_hz
                aligned=self.detector.detect(iq,left,sps,self.cfo,self.config.locked_cutoff_hz)
                if self._probe(aligned[0],sps):
                    result=aligned
                    self.effective_cutoff_hz=self.config.locked_cutoff_hz
                else:
                    # Do not discard a verified wider FSK signal to force the
                    # narrow-recording filter on a nominal TX waveform.
                    result=self.detector.detect(iq,left,sps,self.cfo,self.config.acquisition_cutoff_hz)
                    self.effective_cutoff_hz=self.config.acquisition_cutoff_hz
                self.locked=True
                self.assembler.reset()
                self.stats['lock_acquisitions']+=1
                self.lock_events.append(dict(sample=a,sps=sps,cfo_hz=self.cfo))
                self.lock_events=self.lock_events[-32:]
        else:
            result=self.detector.detect(iq,left,self.sps,self.cfo,self.effective_cutoff_hz)
        packets,raw,mf=result
        frames=[];accepted=[]
        for p in packets:
            anchor=p['source_access_sample']
            if not a<=anchor<b or anchor-self.last_physical<self.sps:
                continue
            self.last_physical=anchor;self.stats['physical_fragments']+=1;accepted.append(p)
            frames.extend(self.assembler.feed(p,self.sps))
        if frames:
            self.last_crc_sample=b
            if self.first_frame_wall is None:
                self.first_frame_wall=time.perf_counter()-self.started
            if self.locked and self.config.cfo_tracking and accepted:
                target=float(np.median([p['sync_fitted_cfo_hz'] for p in accepted]))
                if abs(target-self.cfo)<20000:
                    self.cfo+=self.config.cfo_alpha*(target-self.cfo)
        elif self.locked and self.config.cfo_hz is None and b-self.last_crc_sample>self.config.loss_timeout_seconds*self.config.sample_rate:
            self.locked=False;self.assembler.reset();self.stats['lock_losses']+=1
        loc=np.arange(a-left,b-left)
        raw_trace=np.zeros(b-a,np.float32)
        valid=loc<len(raw);raw_trace[valid]=raw[loc[valid]]*self.config.sample_rate/(2*np.pi)
        normalized=np.zeros(b-a,np.float32);symbol_indices=np.empty(0,np.int64)
        if packets:
            anchors=np.array([p['source_access_sample']-left for p in packets])
            dcs=np.array([p['dc'] for p in packets]);scales=np.array([p['scale'] for p in packets])
            dc=np.interp(loc,anchors,dcs);scale=np.interp(loc,anchors,scales)
            # Local known-access scale tracks FM levels; no data high-pass.
            good=(loc<len(mf))&(np.abs(scale)>1e-10)
            norm=np.zeros(b-a);norm[good]=(mf[loc[good]]-dc[good])/scale[good]
            covered=np.zeros(b-a,bool);centers=[]
            for p in packets:
                lo=max(a,p['source_access_sample']);hi=min(b,p['source_access_sample']+216*self.sps)
                if hi>lo:covered[lo-a:hi-a]=True
                c=p['source_access_sample']+np.arange(216)*self.sps
                centers.extend(c[(c>=a)&(c<b)].tolist())
            normalized[covered]=norm[covered].astype(np.float32)
            symbol_indices=np.asarray(sorted(set(centers)),np.int64)
        elapsed=(time.perf_counter()-t)*1000
        self.processing_ms.append(elapsed);self.processing_ms=self.processing_ms[-4096:]
        for frame in frames:
            self.stats['crc_frames']+=1;self.counts[frame['cmd']]+=1
            self.frame_hash.update(bytes.fromhex(frame['frame_hex']))
            delay=(right-frame['source_frame_end_sample_exclusive'])/self.config.sample_rate*1000
            frame['sample_buffering_latency_ms']=max(0.,delay)
            frame['batch_processing_ms']=elapsed
            self.buffer_latency_ms.append(max(0.,delay));self.buffer_latency_ms=self.buffer_latency_ms[-8192:]
            if self.on_frame:self.on_frame(frame)
        self.stats['processed_samples']+=b-a
        batch=RxBatch(a,self.sps,normalized,raw_trace,symbol_indices,frames,accepted)
        self.normalized_hash.update(normalized.tobytes());self.raw_trace_hash.update(raw_trace.tobytes())
        self.centers_hash.update(symbol_indices.astype('<i8',copy=False).tobytes())
        if packets and self.last_diagnostic is None and symbol_indices.size:
            self.last_diagnostic=batch
        return batch

    def _drain(self,final=False):
        result=[]
        while self.next_start<self.received:
            b=min(self.next_start+self.chunk,self.received)
            if not final and b+self.halo>self.received:
                break
            result.append(self._process(self.next_start,b));self.next_start=b
            keep=max(self.buffer_start,self.next_start-self.halo)
            self.buffer=self.buffer[keep-self.buffer_start:].copy();self.buffer_start=keep
        return result

    def feed(self,iq):
        if self.finished:raise RuntimeError('receiver already finalized')
        x=np.asarray(iq,dtype=np.complex64).reshape(-1)
        if not np.isfinite(x).all():raise ValueError('IQ contains NaN/Inf; check dtype/endianness')
        batches=[]
        for a in range(0,len(x),self.chunk):
            part=x[a:a+self.chunk]
            self.buffer=np.concatenate((self.buffer,part));self.received+=len(part)
            batches.extend(self._drain())
        return batches

    def finish(self):
        if self.finished:return []
        batches=self._drain(final=True);self.finished=True
        return batches

    def mark_gap(self,missing_samples):
        """Explicit upstream loss notification; never silently concatenate gaps."""
        if missing_samples<0:raise ValueError('missing_samples must be nonnegative')
        batches=self.finish();self.finished=False
        self.received+=int(missing_samples);self.next_start=self.received;self.buffer_start=self.received
        self.buffer=np.empty(0,np.complex64);self.assembler.reset();self.locked=False
        self.stats['source_missing_samples']+=int(missing_samples)
        return batches

    def summary(self):
        wall=time.perf_counter()-self.started
        seconds=self.stats['processed_samples']/self.config.sample_rate
        attempts=self.assembler.stats['crc16_attempts'];good=self.stats['crc_frames']
        def percentiles(x):
            return dict(p50=float(np.median(x)),p95=float(np.percentile(x,95)),max=float(max(x))) if x else None
        try:
            import resource
            peak_rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024 if sys.platform!='darwin' else 1024*1024)
        except ImportError:
            peak_rss=None
        return dict(config=dataclasses.asdict(self.config),locked=self.locked,selected_sps=self.sps,
            effective_cutoff_hz=self.effective_cutoff_hz,peak_process_rss_mib=peak_rss,
            current_cfo_hz=self.cfo,symbol_rate_hz=self.config.sample_rate/self.sps,
            input_samples=self.received,processed_samples=self.stats['processed_samples'],iq_seconds=seconds,
            wall_seconds=wall,physical_fragments=self.stats['physical_fragments'],crc_frames=good,
            crc16_attempts=attempts,crc_success_percent=100*good/attempts if attempts else None,
            crc_rate_definition='passed CRC16 / all complete known-command frames with valid CRC8',
            frames_per_iq_second=good/seconds if seconds else 0.,
            frames_per_wall_second=good/wall if wall else 0.,real_time_factor=seconds/wall if wall else 0.,
            first_frame_wall_seconds=self.first_frame_wall,batch_processing_ms=percentiles(self.processing_ms),
            modeled_sample_buffering_latency_ms=percentiles(self.buffer_latency_ms),
            rf_to_host_latency_ms=None,rf_latency_note='No transmitter/hardware timestamp; not measurable from raw IQ',
            by_command=dict(self.counts),parser_stats=dict(self.assembler.stats),receiver_stats=dict(self.stats),
            ordered_frame_sha256=self.frame_hash.hexdigest(),lock_events=self.lock_events,
            normalized_trace_sha256=self.normalized_hash.hexdigest(),raw_fm_trace_sha256=self.raw_trace_hash.hexdigest(),
            symbol_center_indices_sha256=self.centers_hash.hexdigest())


class FileIQSource:
    def __init__(self,path,read_samples=65536,start_sample=0,limit_samples=None):
        self.file=Path(path).open('rb');self.file.seek(start_sample*8)
        self.read_samples=read_samples;self.remaining=limit_samples
    def __iter__(self):
        while self.remaining is None or self.remaining>0:
            count=self.read_samples if self.remaining is None else min(self.read_samples,self.remaining)
            x=np.fromfile(self.file,dtype='<c8',count=count)
            if not len(x):break
            if self.remaining is not None:self.remaining-=len(x)
            yield x
    def close(self):self.file.close()


class PlutoIQSource:
    """Synchronous IIO RX source. It never opens or transmits a TX buffer."""
    def __init__(self,uri,sample_rate,lo,gain,rf_bandwidth,read_samples=65536):
        try:import adi
        except ImportError as exc:raise RuntimeError('Pluto mode requires pyadi-iio and system libiio') from exc
        self.sdr=adi.Pluto(uri=uri)
        self.sdr.rx_enabled_channels=[0]
        self.sdr.sample_rate=int(sample_rate);self.sdr.rx_lo=int(lo)
        self.sdr.rx_rf_bandwidth=int(rf_bandwidth);self.sdr.rx_buffer_size=read_samples
        self.sdr.gain_control_mode_chan0='manual';self.sdr.rx_hardwaregain_chan0=float(gain)
        actual=float(self.sdr.sample_rate)
        if abs(actual-sample_rate)>1:raise RuntimeError(f'Pluto returned Fs={actual}; requested {sample_rate}')
    def __iter__(self):
        while True:yield np.asarray(self.sdr.rx(),np.complex64)
    def close(self):self.sdr.rx_destroy_buffer()


def read_metadata(iq_path,metadata_path=None):
    path=Path(metadata_path) if metadata_path else Path(iq_path).with_suffix('.json')
    meta=json.loads(path.read_text()) if path.exists() else {}
    if meta.get('dtype','complex64') not in ('complex64','<c8','c64'):
        raise ValueError('this receiver expects raw complex64 IQ')
    if meta.get('byteorder','little') not in ('little','<','native'):
        raise ValueError('expected little-endian .c64')
    return meta


class EventWriter:
    def __init__(self,output,verbose=False):
        self.path=Path(output);self.path.mkdir(parents=True,exist_ok=True)
        self.frames=(self.path/'frames.jsonl').open('w',encoding='utf-8')
        self.packets=(self.path/'physical.jsonl').open('w',encoding='utf-8')
        self.verbose=verbose;self.printed=0
    def frame(self,event):
        self.frames.write(json.dumps(event,ensure_ascii=False,separators=(',',':'))+'\n')
        if self.verbose or self.printed<5:
            print(f"FRAME {event['cmd']} seq={event['seq']} t={event['time_seconds']:.6f}s CRC8/16=OK {event['decoded'] if 'decoded' in event else event.get('decode_error')}",flush=True)
            self.printed+=1
    def batch(self,batch):
        for p in batch.packets:
            row=dict(p);row['payload_hex']=row.pop('payload').hex()
            self.packets.write(json.dumps(row,separators=(',',':'))+'\n')
    def finish(self,receiver):
        self.frames.close();self.packets.close()
        result=receiver.summary()
        (self.path/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print_stats(result,final=True)
        return result


def print_stats(summary,final=False):
    rate=summary['crc_success_percent']
    success=f'{rate:.2f}%' if rate is not None else 'N/A (0 candidates)'
    latency=summary['batch_processing_ms'];process=f"{latency['p95']:.2f}ms" if latency else 'N/A'
    buffering=summary['modeled_sample_buffering_latency_ms']
    buffer_text=f"{buffering['p95']:.2f}ms" if buffering else 'N/A'
    print(f"{'FINAL' if final else 'STAT'} IQ={summary['iq_seconds']:.3f}s wall={summary['wall_seconds']:.3f}s "
          f"SPS={summary['selected_sps']} CFO={summary['current_cfo_hz']:+.2f}Hz "
          f"physical={summary['physical_fragments']} CRC={summary['crc_frames']} success={success} "
          f"air_fps={summary['frames_per_iq_second']:.2f} decode_fps={summary['frames_per_wall_second']:.2f} "
          f"speed={summary['real_time_factor']:.2f}x processing_p95={process} sample_buffer_p95={buffer_text}",flush=True)


def save_diagnostic(receiver,output):
    batch=receiver.last_diagnostic
    if batch is None:return
    sps=batch.sps
    n=min(len(batch.normalized),100000)
    centers=batch.symbol_indices-batch.start_sample
    centers=centers[(centers>=sps)&(centers+sps<n)][:256]
    eyes=np.array([batch.normalized[c-sps:c+sps+1] for c in centers])
    symbols=batch.normalized[centers]
    path=Path(output)
    np.savez_compressed(path/'eye_symbols.npz',normalized=batch.normalized[:n],raw_fm_hz=batch.raw_fm_hz[:n],
        eyes=eyes,symbols=symbols,sps=sps,sample_rate=receiver.config.sample_rate)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(1,2,figsize=(10,3.5))
    if len(eyes):axes[0].plot(np.arange(-sps,sps+1)/sps,eyes.T,color='#2166ac',alpha=.12)
    axes[0].set(xlabel='Symbols from recovered center',ylabel='Normalized discriminator',title='Matched-filter eye (actual packet centers)')
    axes[1].scatter(symbols,np.zeros(len(symbols)),s=8,alpha=.3)
    axes[1].set(xlabel='Normalized soft symbol',ylabel='Imaginary = 0',title='Decision-plane symbols (not RF IQ constellation)',ylim=(-.3,.3))
    fig.tight_layout();fig.savefig(path/'eye_symbols.png',dpi=160);plt.close(fig)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--iq',type=Path);source.add_argument('--pluto',metavar='IIO_URI')
    p.add_argument('--metadata',type=Path);p.add_argument('--output',type=Path,default=Path('stage_o_output'))
    p.add_argument('--sample-rate',type=float);p.add_argument('--sps',default='auto',help='auto=47,52 or one explicit integer')
    p.add_argument('--bt',type=float);p.add_argument('--wave',choices=tuple(ACCESS_CODES),default='info')
    p.add_argument('--cfo-hz',type=float,help='fixed initial CFO; omit for CRC-gated acquisition')
    p.add_argument('--no-track',action='store_true');p.add_argument('--cutoff-hz',type=float,default=60000)
    p.add_argument('--acquisition-cutoff-hz',type=float,default=260000)
    p.add_argument('--chunk-seconds',type=float,default=.5);p.add_argument('--read-samples',type=int,default=65536)
    p.add_argument('--seconds',type=float,help='offline IQ duration limit or live wall-time limit')
    p.add_argument('--lo',type=float,default=433200000);p.add_argument('--gain',type=float,default=50)
    p.add_argument('--rf-bandwidth',type=float,default=540000)
    p.add_argument('--unsigned-positions',action='store_true');p.add_argument('--verbose-frames',action='store_true')
    p.add_argument('--plots',action='store_true')
    args=p.parse_args()
    meta=read_metadata(args.iq,args.metadata) if args.iq else {}
    fs=args.sample_rate or float(meta.get('sample_rate',meta.get('sample_rate_hz',1e6)))
    sps=(47,52) if args.sps=='auto' else (int(args.sps),)
    config=RxConfig(sample_rate=fs,sps_candidates=sps,bt=args.bt or float(meta.get('bt',.35)),wave=args.wave,
        chunk_seconds=args.chunk_seconds,cfo_hz=args.cfo_hz,cfo_tracking=not args.no_track,
        locked_cutoff_hz=args.cutoff_hz,acquisition_cutoff_hz=args.acquisition_cutoff_hz,
        position_signed=not args.unsigned_positions)
    if args.read_samples<=0 or args.seconds is not None and args.seconds<=0:p.error('positive read size/duration required')
    if args.iq and args.iq.stat().st_size%8:p.error('file length is not a whole number of complex64 samples')
    writer=EventWriter(args.output,args.verbose_frames)
    rx=StreamingReceiver(config,writer.frame)
    input_source=FileIQSource(args.iq,args.read_samples,limit_samples=round(args.seconds*fs) if args.seconds else None) if args.iq else PlutoIQSource(args.pluto,fs,args.lo,args.gain,args.rf_bandwidth,args.read_samples)
    print(f'RX mode={"file" if args.iq else "Pluto"} Fs={fs:g} SPS candidates={sps}; TX nominal parameters are independent',flush=True)
    start=time.perf_counter();last=start
    try:
        for x in input_source:
            for batch in rx.feed(x):writer.batch(batch)
            now=time.perf_counter()
            if now-last>=2:print_stats(rx.summary());last=now
            if args.pluto and args.seconds and now-start>=args.seconds:break
    except KeyboardInterrupt:
        print('Stopping RX and draining captured tail',flush=True)
    finally:
        for batch in rx.finish():writer.batch(batch)
        input_source.close();writer.finish(rx)
    if args.plots:save_diagnostic(rx,args.output)


if __name__=='__main__':
    main()
