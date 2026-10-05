#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual GNU Radio scheduler replay, optionally Qt GUI eye and symbol plane."""
import argparse
import importlib.util
import hashlib
import json
import sys
from pathlib import Path
from stage_o_patched_rx import read_metadata


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--iq',required=True,type=Path);p.add_argument('--output',type=Path,default=Path('stage_o_gr_output'))
    p.add_argument('--sps',default='auto');p.add_argument('--sample-rate',type=float)
    p.add_argument('--cfo-hz',type=float);p.add_argument('--no-track',action='store_true')
    p.add_argument('--cutoff-hz',type=float,default=60000);p.add_argument('--chunk-seconds',type=float,default=.5)
    p.add_argument('--gui',action='store_true');p.add_argument('--reference-summary',type=Path)
    p.add_argument('--verify-traces',action='store_true',help='Write both float32 output streams and verify hashes (8 bytes per IQ sample total)')
    args=p.parse_args()
    if importlib.util.find_spec('gnuradio') is None:
        p.exit(3,'GNU Radio is not installed in this Python environment. On Ubuntu use the system Python with GNU Radio 3.10.\n')
    from gnuradio import gr,blocks
    from stage_o_gnuradio import StageOFileSource,StageOPatchedRxBlock,TaggedSymbolSampler
    fs=args.sample_rate or float(read_metadata(args.iq).get('sample_rate',1e6))
    tb=gr.top_block('Stage O identical-core replay')
    source=StageOFileSource(str(args.iq))
    rx=StageOPatchedRxBlock(fs,args.sps,cfo_hz=args.cfo_hz,cutoff_hz=args.cutoff_hz,
        chunk_seconds=args.chunk_seconds,cfo_tracking=not args.no_track,output_dir=str(args.output))
    if args.verify_traces:
        trace_sinks=[]
        for port,name in enumerate(('normalized.f32','raw_fm_hz.f32')):
            sink=blocks.file_sink(gr.sizeof_float,str(args.output/name),False)
            trace_sinks.append(sink);tb.connect((rx,port),sink)
    if args.gui:
        from gnuradio import qtgui
        from PyQt5 import QtWidgets,QtCore
        import sip
        app=QtWidgets.QApplication(sys.argv);window=QtWidgets.QWidget();layout=QtWidgets.QVBoxLayout(window)
        throttle=blocks.throttle(gr.sizeof_gr_complex,fs,True)
        tb.connect(source,throttle,rx)
        sps=47 if args.sps=='auto' else int(args.sps)
        eye=qtgui.eye_sink_f(2*sps*64,fs,1)
        eye.set_samp_per_symbol(sps);eye.set_y_axis(-2.5,2.5)
        eye.set_trigger_mode(qtgui.TRIG_MODE_TAG,qtgui.TRIG_SLOPE_POS,0.,sps/fs,0,'symbol_center')
        raw=qtgui.time_sink_f(4096,fs,'Raw discriminator (Hz)',1)
        constellation=qtgui.const_sink_c(1024,'Normalized decision symbols',1)
        constellation.set_x_axis(-2.5,2.5);constellation.set_y_axis(-.25,.25)
        sampler=TaggedSymbolSampler()
        tb.connect((rx,0),eye);tb.connect((rx,1),raw);tb.connect((rx,0),sampler,constellation)
        for sink in (eye,raw,constellation):layout.addWidget(sip.wrapinstance(sink.qwidget(),QtWidgets.QWidget))
        window.setWindowTitle('Stage O patched RX / identical core');window.resize(1000,850);window.show()
        def update_eye():
            eye.set_samp_per_symbol(rx.rx.sps)
            eye.set_trigger_mode(qtgui.TRIG_MODE_TAG,qtgui.TRIG_SLOPE_POS,0.,rx.rx.sps/fs,0,'symbol_center')
        timer=QtCore.QTimer();timer.timeout.connect(update_eye);timer.start(500)
        tb.start()
        try:app.exec_()
        finally:tb.stop();tb.wait()
    else:
        tb.connect(source,rx)
        tb.connect((rx,0),blocks.null_sink(gr.sizeof_float))
        tb.connect((rx,1),blocks.null_sink(gr.sizeof_float))
        try:tb.run()
        except KeyboardInterrupt:tb.stop();tb.wait()
    rx.stop()  # idempotent: explicitly finalize even if scheduler already did
    result=json.loads((args.output/'summary.json').read_text())
    trace_mismatches={}
    if args.verify_traces:
        for sink in trace_sinks:sink.close()
        for filename,key in [('normalized.f32','normalized_trace_sha256'),('raw_fm_hz.f32','raw_fm_trace_sha256')]:
            h=hashlib.sha256();trace_path=args.output/filename
            with trace_path.open('rb') as f:
                for chunk in iter(lambda:f.read(8*1024*1024),b''):h.update(chunk)
            if h.hexdigest()!=result[key] or trace_path.stat().st_size!=result['processed_samples']*4:
                trace_mismatches[filename]=dict(actual_sha256=h.hexdigest(),expected_sha256=result[key],
                    actual_bytes=trace_path.stat().st_size,expected_bytes=result['processed_samples']*4)
    if args.reference_summary:
        reference=json.loads(args.reference_summary.read_text())
        keys=['crc_frames','physical_fragments','ordered_frame_sha256','processed_samples',
              'normalized_trace_sha256','raw_fm_trace_sha256','symbol_center_indices_sha256']
        mismatches={k:[reference[k],result[k]] for k in keys if reference[k]!=result[k]}
        verdict=dict(gnu_radio_runtime_executed=True,exact_match=not mismatches and not trace_mismatches,
            mismatches=mismatches,output_trace_mismatches=trace_mismatches,actual_output_traces_verified=args.verify_traces)
        (args.output/'gnu_comparison.json').write_text(json.dumps(verdict,indent=2)+'\n')
        print('GNU_COMPARISON',json.dumps(verdict))
        if mismatches or trace_mismatches:p.exit(1,'GNU/standalone byte or output trace mismatch\n')
    elif trace_mismatches:
        p.exit(1,'GNU output stream/receiver trace hash mismatch\n')


if __name__=='__main__':main()
