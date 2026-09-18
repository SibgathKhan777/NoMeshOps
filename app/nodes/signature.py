"""Step 5: turn raw failure output into a structured error + a stable signature.

signature = hash(error_type, package, package_version, os_key, arch, python_minor, normalized_message)
family    = hash(error_type, package, normalized_message)   -- runtime-context-free
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict


@dataclass
class ErrorInfo:
    error_type: str
    package: str = ""
    package_version: str = ""
    message: str = ""
    normalized: str = ""
    stage: str = ""
    signature: str = ""
    family: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# Transient infrastructure failures. These are NOT defects in the project and must never be "learned":
# a retry is the remedy, and crediting whatever fix happened to precede the recovery poisons the knowledge base.
TRANSIENT = re.compile(
    r"ReadTimeoutError|ConnectTimeoutError|NewConnectionError|temporary failure in name resolution|"
    r"connection broken by|connection reset by peer|TLS/SSL connection has been closed|"
    r"503 Server Error|502 Server Error|429 Too Many Requests|Temporary failure resolving|"
    r"network is unreachable|Could not resolve host",
    re.IGNORECASE,
)

_PATTERNS: list[tuple[str, str, int | None]] = [
    # (error_type, regex, group index of package name or None)
    ("BuildError", r"pg_config executable not found", None),
    ("BuildError", r"Failed to build (?:installable wheels for some pyproject\.toml based projects \()?([A-Za-z0-9_.\-\[\]]+)", 1),
    ("BuildError", r"Failed building wheel for ([A-Za-z0-9_.\-\[\]]+)", 1),
    ("BuildError", r"error: subprocess-exited-with-error[\s\S]{0,400}?(?:Building wheel|Preparing metadata|Getting requirements to build wheel) for ([A-Za-z0-9_.\-\[\]]+)", 1),
    ("ResolutionError", r"No matching distribution found for ([A-Za-z0-9_.\-\[\]]+)", 1),
    ("ResolutionError", r"Could not find a version that satisfies the requirement ([A-Za-z0-9_.\-\[\]]+)", 1),
    ("ImportError", r"ModuleNotFoundError: No module named '([A-Za-z0-9_.]+)'", 1),
    ("ImportError", r"ImportError: ([^\n]+)", None),
    ("SyntaxError", r"SyntaxError: ([^\n]+)", None),
    ("GitError", r"fatal: ([^\n]+)", None),
    ("RuntimeMissing", r"python3: (?:command )?not found", None),
    ("UnreachableTarget", r"is not running \(|InvalidInstanceId|No such object|Instances not in a valid state|not connected to Systems Manager", None),
    ("PortConflict", r"PortBusy: [^\n]+", None),
    ("StartError", r"Address already in use", None),
    ("HealthCheckError", r"__HEALTH__=FAIL", None),
    ("PermissionError", r"Permission denied", None),
    ("PipError", r"ERROR: ([^\n]+)", None),
]


def normalize(msg: str) -> str:
    m = msg.lower()
    m = re.sub(r"/tmp/pip-[\w\-]+", "<tmp>", m)
    m = re.sub(r"/[\w\-./]+", "<path>", m)
    m = re.sub(r"0x[0-9a-f]+", "<hex>", m)
    m = re.sub(r"\b[0-9a-f]{7,}\b", "<hex>", m)
    m = re.sub(r"\d+(\.\d+)*", "<n>", m)
    m = re.sub(r"\s+", " ", m).strip()
    return m[:300]


def _clean_pkg(name: str) -> str:
    return re.sub(r"\[.*\]$", "", name).lower().replace("_", "-").strip(".,;:")


def classify(stdout: str, stderr: str, stage: str, deps: list[dict] | None = None) -> ErrorInfo:
    text = f"{stdout}\n{stderr}"
    info = ErrorInfo(error_type="UnknownError", stage=stage)
    m = TRANSIENT.search(text)
    if m:
        info.error_type = "TransientError"
        info.message = m.group(0)[:500]
        info.normalized = normalize(info.message)
        return info
    for etype, pattern, grp in _PATTERNS:
        m = re.search(pattern, text)
        if m:
            info.error_type = etype
            info.message = m.group(0)[:500]
            if grp:
                info.package = _clean_pkg(m.group(grp))
            elif etype == "BuildError" and "pg_config" in m.group(0):
                info.package = "psycopg2"
            break
    if info.error_type == "UnknownError":
        lines = [l for l in (stderr + "\n" + stdout).splitlines()
                 if l.strip() and not (l.startswith("__") and "__=" in l) and not l.startswith("__")]
        info.message = (lines[-1] if lines else f"{stage} failed")[:500]

    # Package version. Read it from the failure text first: the manifest scan is an orchestrator-side
    # capability, and letting it decide the version would make the SAME error hash differently depending on
    # whether the orchestrator could reach the repo, fragmenting the knowledge base.
    if info.package:
        pkg = re.escape(info.package)
        m = None
        for pat in (rf"Collecting {pkg}(?:\[[^\]]*\])?\s*([=<>!~]+[\w.*,<>=!~]+)",
                    rf"requirement {pkg}(?:\[[^\]]*\])?\s*([=<>!~]+[\w.*,<>=!~]+)",
                    rf"\b{pkg}(?:\[[^\]]*\])?(==[\w.*]+)"):
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                break
        if m:
            info.package_version = m.group(1).strip()
        elif deps:
            for d in deps:
                if d.get("name") == info.package:
                    info.package_version = d.get("specifier", "")
                    break
    info.normalized = normalize(info.message)
    return info


def compute_signature(info: ErrorInfo, fingerprint: dict) -> ErrorInfo:
    ctx = [
        info.error_type, info.package, info.package_version,
        fingerprint.get("os_key", ""), fingerprint.get("arch", ""), fingerprint.get("python_minor", ""),
        info.normalized,
    ]
    info.signature = hashlib.sha256("|".join(ctx).encode()).hexdigest()[:20]
    info.family = hashlib.sha256("|".join([info.error_type, info.package, info.normalized]).encode()).hexdigest()[:20]
    return info
