"""Tiny FastAPI target used for the demo. /health is what the agent's verification step polls."""
import platform

import numpy as np
from fastapi import FastAPI

app = FastAPI(title="nomeshops-sample")


@app.get("/health")
def health():
    return {"ok": True, "python": platform.python_version(), "numpy": np.__version__}


@app.get("/")
def root():
    return {"service": "nomeshops-sample", "sum": float(np.arange(10).sum())}
