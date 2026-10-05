# SPDX-License-Identifier: GPL-3.0-or-later
"""GNU Radio 3.10 adapters. DSP/CRC are exactly the standalone RX implementation.

frame_out retains PIONEER's PMT (metadata dict . uint8 payload vector) format.
events additionally publishes JSON with full raw frames and source indices.
"""
import json
from pathlib import Path
import numpy as np
from gnuradio import gr
import pmt
from stage_o_patched_rx import RxConfig, StreamingReceiver, EventWriter
from stage_o_trace_transport import TraceQueue


class StageOFileSource(gr.sync_block):
    """Raw little-endian complex64 file with an explicit final-sample tag."""
    def __init__(self,filename):
        gr.sync_block.__init__(self,name='Stage O IQ File Source',in_sig=None,out_sig=[np.complex64])
        path=Path(filename)
        if path.stat().st_size%8:raise ValueError('file has incomplete complex64 sample')
        self.file=path.open('rb');self.remaining=path.stat().st_size//8
    def work(self,input_items,output_items):
        if self.remaining==0:return -1
        n=min(len(output_items[0]),self.remaining)
        x=np.fromfile(self.file,dtype='<c8',count=n)
        if len(x)!=n:raise OSError('IQ file truncated during replay')
        output_items[0][:n]=x;self.remaining-=n
        if self.remaining==0:
            self.add_item_tag(0,self.nitems_written(0)+n-1,pmt.intern('rx_eof'),pmt.PMT_T)
        return n
    def stop(self):
        self.file.close();return True


class StageOPatchedRxBlock(gr.basic_block):
    def __init__(self,sample_rate=1e6,sps='auto',bt=.35,cfo_hz=None,cutoff_hz=60000,
                 chunk_seconds=.5,cfo_tracking=True,output_dir='',wave='info'):
        gr.basic_block.__init__(self,name='Stage O Patched RX',in_sig=[np.complex64],
                               out_sig=[np.float32,np.float32])
        candidates=(47,52) if str(sps)=='auto' else (int(sps),)
        self.config=RxConfig(sample_rate=sample_rate,sps_candidates=candidates,bt=bt,
            cfo_hz=cfo_hz,locked_cutoff_hz=cutoff_hz,chunk_seconds=chunk_seconds,
            cfo_tracking=cfo_tracking,wave=wave)
        self.writer=EventWriter(output_dir) if output_dir else None
        self.message_port_register_out(pmt.intern('frame_out'))
        self.message_port_register_out(pmt.intern('events'))
        self.rx=StreamingReceiver(self.config,self._frame)
        self.pending=TraceQueue();self.eof=False;self.saved=False
        self.set_tag_propagation_policy(gr.TPP_DONT)

    def _frame(self,event):
        if self.writer:self.writer.frame(event)
        meta=pmt.make_dict()
        for key,value in [('cmd_id',int(event['cmd'],16)),('seq',event['seq']),
                          ('source_sample',event['source_first_payload_sample'])]:
            meta=pmt.dict_add(meta,pmt.intern(key),pmt.from_long(value))
        payload=bytes.fromhex(event['payload_hex'])
        self.message_port_pub(pmt.intern('frame_out'),pmt.cons(meta,pmt.init_u8vector(len(payload),list(payload))))
        self.message_port_pub(pmt.intern('events'),pmt.intern(json.dumps(event,ensure_ascii=False)))

    def forecast(self,noutput_items,ninputs):
        return [0 if self.pending or self.eof else 1]*ninputs

    def _queue(self,batches):
        if self.writer:
            for batch in batches:self.writer.batch(batch)
        self.pending.append(batches)

    def general_work(self,input_items,output_items):
        if not self.pending and not self.eof and len(input_items[0]):
            n=min(len(input_items[0]),65536)
            ending=any(t.key==pmt.intern('rx_eof') for t in self.get_tags_in_window(0,0,n))
            self._queue(self.rx.feed(input_items[0][:n]));self.consume_each(n)
            if ending:
                self._queue(self.rx.finish());self.eof=True
        capacity=min(map(len,output_items))
        norm,raw,tags=self.pending.pull(capacity)
        n=len(norm)
        output_items[0][:n]=norm;output_items[1][:n]=raw
        for offset,source_index in tags:
            self.add_item_tag(0,offset,pmt.intern('symbol_center'),pmt.from_long(source_index))
        if not n and self.eof:return -1
        return n

    def stop(self):
        # Live stop / ordinary file sources without EOF tags still drain CRCs.
        # Use StageOFileSource for complete trace output before scheduler EOF.
        if not self.rx.finished:self._queue(self.rx.finish())
        if self.writer and not self.saved:self.writer.finish(self.rx);self.saved=True
        return True


class TaggedSymbolSampler(gr.basic_block):
    """Samples normalized FM only at recovered centers; complex imaginary=0."""
    def __init__(self):
        gr.basic_block.__init__(self,name='Stage O Tagged Symbol Sampler',
                               in_sig=[np.float32],out_sig=[np.complex64])
        self.set_tag_propagation_policy(gr.TPP_DONT)
    def forecast(self,noutput_items,ninputs):return [1]*ninputs
    def general_work(self,input_items,output_items):
        n=len(input_items[0]);capacity=len(output_items[0])
        tags=sorted((t for t in self.get_tags_in_window(0,0,n) if t.key==pmt.intern('symbol_center')),key=lambda t:t.offset)
        if len(tags)>capacity:
            tags=tags[:capacity]
            consumed=int(tags[-1].offset-self.nitems_read(0))+1
        else:consumed=n
        for i,t in enumerate(tags):output_items[0][i]=complex(float(input_items[0][int(t.offset-self.nitems_read(0))]),0.)
        self.consume_each(consumed)
        return len(tags)
