#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sequential bounded-memory regression on the supplied two real INFO IQ files."""
import argparse
import collections
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
from stage_o_patched_rx import crc8,crc16

ROOT=Path(__file__).resolve().parent
LONG='aux_info_usb_20260804_163352_456673.c64'
SHORT='match_20260802_032624_557875.c64'


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--recordings-dir',required=True,type=Path)
    p.add_argument('--output',type=Path,default=ROOT/'validation')
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    cases=[('long_fixed',LONG,['--sps','47','--cfo-hz','32760.823911734893','--no-track','--plots']),
           ('long_auto',LONG,['--plots']),('short_auto',SHORT,['--plots']),
           ('short_sps52',SHORT,['--sps','52','--cfo-hz','541.1274933700939','--no-track']),
           ('short_partitioned',SHORT,['--sps','47','--cfo-hz','541.1274933700939','--no-track','--read-samples','131071'])]
    results=[]
    for name,file,extra in cases:
        source=args.recordings_dir/file
        if not source.exists():p.error(f'missing recording: {source}')
        out=args.output/name;started=time.perf_counter()
        with (args.output/(name+'.log')).open('w') as log:
            subprocess.run([sys.executable,str(ROOT/'stage_o_patched_rx.py'),'--iq',str(source),
                '--output',str(out),*extra],check=True,stdout=log,stderr=subprocess.STDOUT)
        row=json.loads((out/'summary.json').read_text());row['case']=name
        row['subprocess_wall_seconds']=time.perf_counter()-started
        rows=[json.loads(line) for line in (out/'frames.jsonl').open()]
        for event in rows:
            raw=bytes.fromhex(event['frame_hex'])
            assert crc8(raw[:4])==raw[4] and crc16(raw[:-2])==int.from_bytes(raw[-2:],'little')
        if name=='long_fixed':
            assert row['physical_fragments']==6695 and row['crc_frames']==3662
            golden=[json.loads(line)['frame_hex'] for line in (ROOT/'fixtures/golden_long_frames.jsonl').open()]
            assert [v['frame_hex'] for v in rows]==golden
            row['ordered_frames_match_previous_reference']=True
        if name in ('short_auto','short_partitioned'):
            assert row['physical_fragments']==392 and row['crc_frames']==217
            golden=[json.loads(line)['frame_hex'] for line in (ROOT/'fixtures/golden_short_frames.jsonl').open()]
            assert [v['frame_hex'] for v in rows]==golden
            row['ordered_frames_match_previous_reference']=True
        if name=='short_sps52':assert row['crc_frames']==0
        results.append(row)
        print(name,row['physical_fragments'],row['crc_frames'],f"{row['wall_seconds']:.3f}s",flush=True)
    result=dict(success=True,cases=results,
        gnu_radio_runtime_installed=importlib.util.find_spec('gnuradio') is not None,
        gnu_radio_runtime_executed=False,pluto_hardware_executed=False,
        note='Actual GNU scheduler and output-trace verification: run validate_gnu_on_host.sh; hardware requires attached Pluto.')
    (args.output/'full_validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':main()
