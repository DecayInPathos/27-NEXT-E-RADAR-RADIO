# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded GNU adapter output transport; importable without GNU Radio."""
from collections import deque
import numpy as np


class TraceQueue:
    def __init__(self):
        self.queue=deque();self.cursor=0;self.written=0

    def append(self,batches):
        self.queue.extend(batches)

    def __bool__(self):
        return bool(self.queue)

    def pull(self,count):
        normalized=[];raw=[];tags=[];total=0
        while self.queue and total<count:
            batch=self.queue[0]
            n=min(count-total,len(batch.normalized)-self.cursor)
            normalized.append(batch.normalized[self.cursor:self.cursor+n])
            raw.append(batch.raw_fm_hz[self.cursor:self.cursor+n])
            source_left=batch.start_sample+self.cursor
            centers=batch.symbol_indices
            selected=centers[(centers>=source_left)&(centers<source_left+n)]
            tags.extend((self.written+total+int(c-source_left),int(c)) for c in selected)
            self.cursor+=n;total+=n
            if self.cursor==len(batch.normalized):
                self.queue.popleft();self.cursor=0
        self.written+=total
        return (np.concatenate(normalized) if normalized else np.empty(0,np.float32),
                np.concatenate(raw) if raw else np.empty(0,np.float32),tags)
