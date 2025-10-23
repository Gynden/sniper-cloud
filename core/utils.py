# -*- coding: utf-8 -*-
import time, json

def ts():
    return int(time.time())

def safe_dict(obj):
    try:
        json.dumps(obj)
        return obj
    except Exception:
        return {"repr": repr(obj)}
