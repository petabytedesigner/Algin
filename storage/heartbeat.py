"""Atomic process status, not a trading authorization."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import secrets
from utils.time_tools import iso_utc,utc_now

class HeartbeatError(RuntimeError): pass
STATES=frozenset(("ACTIVE","CYCLE_COMPLETE","BLOCKED"))

class Heartbeat:
    def __init__(self,path=".local/heartbeat.json"):
        self.path=Path(path)
    def write(self,state,*,reason_code="NONE",cycle_id=""):
        if state not in STATES: raise HeartbeatError("Invalid heartbeat state")
        for value in (reason_code,cycle_id):
            if not isinstance(value,str) or len(value)>128 or not all(
                c.isalnum() or c in "_.:-" for c in value
            ): raise HeartbeatError("Invalid heartbeat identifier")
        data={"version":1,"state":state,"reason_code":reason_code,
              "cycle_id":cycle_id,"timestamp":iso_utc(utc_now()),
              "pid":os.getpid()}
        raw=json.dumps(data,sort_keys=True,separators=(",",":")).encode()
        envelope={"data":data,"sha256":hashlib.sha256(raw).hexdigest()}
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.path.is_symlink(): raise HeartbeatError("Heartbeat symlink forbidden")
        tmp=self.path.with_name(self.path.name+"."+secrets.token_hex(8)+".tmp")
        try:
            fd=os.open(str(tmp),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,"wb") as stream:
                stream.write(json.dumps(envelope,sort_keys=True).encode())
                stream.flush();os.fsync(stream.fileno())
            os.replace(tmp,self.path)
            if hasattr(os,"O_DIRECTORY"):
                d=os.open(str(self.path.parent),os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(d)
                finally:os.close(d)
        except OSError as exc:
            raise HeartbeatError("Heartbeat durability failure") from exc
        finally:
            tmp.unlink(missing_ok=True)
        return data
