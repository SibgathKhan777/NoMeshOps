"""Step 3: deterministic compatibility rules. Pure Python, no network, no AWS, no LLM.

Two rule families live here:
  * PRE-DEPLOY predictions: compare the project scan against the machine fingerprint.
  * POST-FAILURE patterns: map a known error text to a known fix command.

Fix commands run on the target as root, inside the freshly cloned project directory,
with the project's virtualenv first on PATH, immediately before `pip install`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field

from packaging.specifiers import SpecifierSet, InvalidSpecifier
from packaging.version import Version, InvalidVersion

# --------------------------------------------------------------------------- helpers

# Bootstrap package sets differ by manager, not just "apt-get vs everyone else":
# Alpine names its Python packages py3-*, not python3-*, and venv ships inside the python3
# package itself there (no separate python3-venv exists), whereas Debian/Ubuntu split it out.
PYTHON_BOOTSTRAP = {
    "apt-get": "python3 python3-pip python3-venv",
    "dnf": "python3 python3-pip",
    "yum": "python3 python3-pip",
    "apk": "python3 py3-pip",
}
PIP_VENV_BOOTSTRAP = {
    "apt-get": "python3-pip python3-venv",
    "dnf": "python3-pip",
    "yum": "python3-pip",
    "apk": "py3-pip",
}


def _python_bootstrap_pkgs(pm: str) -> str:
    return PYTHON_BOOTSTRAP.get(pm, "python3 python3-pip")


def _pip_venv_pkgs(pm: str) -> str:
    return PIP_VENV_BOOTSTRAP.get(pm, "python3-pip")


PKG_MANAGER_INSTALL = {
    "apt-get": "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq {pkgs}",
    "dnf": "dnf install -y -q {pkgs}",
    "yum": "yum install -y -q {pkgs}",
    "apk": "apk add --no-cache {pkgs}",
}

# System libraries needed to build common source-only Python packages.
SYSDEPS: dict[str, dict[str, str]] = {
    "psycopg2": {"apt-get": "libpq-dev python3-dev gcc", "dnf": "postgresql-devel python3-devel gcc",
                 "yum": "postgresql-devel python3-devel gcc", "apk": "postgresql-dev python3-dev gcc musl-dev"},
    "mysqlclient": {"apt-get": "default-libmysqlclient-dev python3-dev gcc pkg-config",
                    "dnf": "mariadb-connector-c-devel python3-devel gcc pkgconf-pkg-config",
                    "yum": "mariadb-devel python3-devel gcc pkgconfig",
                    "apk": "mariadb-connector-c-dev python3-dev gcc musl-dev"},
    "lxml": {"apt-get": "libxml2-dev libxslt1-dev python3-dev gcc", "dnf": "libxml2-devel libxslt-devel python3-devel gcc",
             "yum": "libxml2-devel libxslt-devel python3-devel gcc", "apk": "libxml2-dev libxslt-dev python3-dev gcc musl-dev"},
    "uwsgi": {"apt-get": "python3-dev gcc", "dnf": "python3-devel gcc", "yum": "python3-devel gcc", "apk": "python3-dev gcc musl-dev linux-headers"},
    "pycairo": {"apt-get": "libcairo2-dev pkg-config python3-dev gcc", "dnf": "cairo-devel pkgconf-pkg-config python3-devel gcc",
                "yum": "cairo-devel pkgconfig python3-devel gcc", "apk": "cairo-dev pkgconf python3-dev gcc musl-dev"},
    "pillow": {"apt-get": "libjpeg-dev zlib1g-dev python3-dev gcc", "dnf": "libjpeg-turbo-devel zlib-devel python3-devel gcc",
               "yum": "libjpeg-turbo-devel zlib-devel python3-devel gcc", "apk": "jpeg-dev zlib-dev python3-dev gcc musl-dev"},
}

# Packages whose newer major versions need a minimum Python. (package, version-spec, min-python)
PY_MIN_RULES: list[tuple[str, str, str]] = [
    ("numpy", ">=2.1", "3.10"),
    ("numpy", ">=1.25,<2.1", "3.9"),
    ("pandas", ">=2.1", "3.9"),
    ("scipy", ">=1.14", "3.10"),
    ("django", ">=5.0", "3.10"),
    ("django", ">=4.2,<5.0", "3.8"),
    ("fastapi", ">=0.100", "3.7"),
    ("pydantic", ">=2.0", "3.7"),
    ("sqlalchemy", ">=2.0", "3.7"),
    ("langchain", ">=0.2", "3.8"),
    ("pillow", ">=10.0", "3.8"),
    ("matplotlib", ">=3.8", "3.9"),
]


@dataclass
class PredictedIssue:
    rule: str
    severity: str            # "blocker" | "warning" | "info"
    message: str
    package: str = ""
    fix_command: str | None = None
    fix_description: str = ""
    stage: str = "pre"       # "pre" = before clone (system bootstrap) | "post" = after venv, before pip install

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RuleFix:
    rule: str
    fix_command: str
    description: str
    package: str = ""
    stage: str = "pre"

    def to_dict(self) -> dict:
        return asdict(self)


def _install_cmd(pkg_manager: str, pkgs: str) -> str | None:
    tpl = PKG_MANAGER_INSTALL.get(pkg_manager)
    return tpl.format(pkgs=pkgs) if tpl else None


def _sysdep_fix(package: str, pkg_manager: str) -> str | None:
    libs = SYSDEPS.get(package, {}).get(pkg_manager)
    return _install_cmd(pkg_manager, libs) if libs else None


def _pinned_or_lower_bound(specifier: str) -> Version | None:
    """Return the version a requirement effectively targets (== pin or lowest >= bound)."""
    try:
        ss = SpecifierSet(specifier)
    except InvalidSpecifier:
        return None
    for spec in ss:
        if spec.operator in ("==", "===", "~="):
            try:
                return Version(spec.version.rstrip(".*"))
            except InvalidVersion:
                return None
    lows = []
    for spec in ss:
        if spec.operator in (">=", ">"):
            try:
                lows.append(Version(spec.version))
            except InvalidVersion:
                pass
    return max(lows) if lows else None


# --------------------------------------------------------------------------- pre-deploy

def check_compatibility(scan: dict, fingerprint: dict) -> list[PredictedIssue]:
    """Compare project scan vs machine fingerprint. Runs in milliseconds, touches nothing."""
    issues: list[PredictedIssue] = []
    py = fingerprint.get("python_version") or ""
    py_minor = fingerprint.get("python_minor") or ""
    pm = fingerprint.get("package_manager") or ""
    deps = {d["name"]: d for d in scan.get("dependencies", [])}

    # 1. Python present at all?
    if not py:
        fix = _install_cmd(pm, _python_bootstrap_pkgs(pm))
        issues.append(PredictedIssue(
            rule="python-missing", severity="blocker",
            message="python3 is not installed on the target",
            fix_command=fix, fix_description="Install python3 with the system package manager",
        ))

    # 1b. git present? (clone happens before anything else)
    if not fingerprint.get("has_git"):
        issues.append(PredictedIssue(
            rule="git-missing", severity="blocker",
            message="git is not installed on the target (clone will fail)",
            fix_command=_install_cmd(pm, "git"), fix_description="Install git with the system package manager",
        ))

    # 2. requires-python vs installed python
    req = scan.get("python_requires") or ""
    if req and py:
        try:
            if not SpecifierSet(req).contains(Version(py), prereleases=True):
                issues.append(PredictedIssue(
                    rule="python-version-mismatch", severity="blocker",
                    message=f"project requires python {req} but target has {py}",
                    fix_description="Install a matching Python version or relax requires-python",
                ))
        except (InvalidSpecifier, InvalidVersion):
            pass

    # 3. pip / venv availability (covered by the python-missing fix when python itself is absent)
    if py and (not fingerprint.get("has_pip") or not fingerprint.get("has_venv")):
        pkgs = _pip_venv_pkgs(pm)
        issues.append(PredictedIssue(
            rule="pip-or-venv-missing", severity="blocker",
            message="pip or venv module missing on the target",
            fix_command=_install_cmd(pm, pkgs), fix_description="Install pip/venv",
        ))

    # 4. package minimum-python table
    if py and py_minor:
        for pkg, spec, min_py in PY_MIN_RULES:
            d = deps.get(pkg)
            if not d:
                continue
            target = _pinned_or_lower_bound(d.get("specifier", ""))
            if target is None:
                continue
            if target in SpecifierSet(spec) and Version(py_minor) < Version(min_py):
                issues.append(PredictedIssue(
                    rule="package-needs-newer-python", severity="blocker", package=pkg,
                    message=f"{pkg}{d.get('specifier','')} needs python>={min_py}; target has {py_minor}",
                    fix_description=f"Pin {pkg} to a release that supports python {py_minor}, or upgrade python",
                ))

    # 5. source-build system dependencies
    for pkg in SYSDEPS:
        if pkg in deps:
            fix = _sysdep_fix(pkg, pm)
            needs_toolchain = not fingerprint.get("has_gcc")
            issues.append(PredictedIssue(
                rule="system-dependency", severity="warning" if not needs_toolchain else "blocker", package=pkg,
                message=f"{pkg} builds from source and needs system libraries"
                        + (" (no gcc on target)" if needs_toolchain else ""),
                fix_command=fix, fix_description=f"Install build deps for {pkg} via {pm or 'package manager'}",
            ))

    # 6. Dockerfile present but no docker on the box (informational: we use the pip path)
    if scan.get("has_dockerfile") and not fingerprint.get("has_docker"):
        issues.append(PredictedIssue(
            rule="dockerfile-without-docker", severity="info",
            message="project has a Dockerfile but docker is not installed; deploying with pip/venv instead",
        ))

    # 7. low disk
    if fingerprint.get("disk_free_mb") and fingerprint["disk_free_mb"] < 1024:
        issues.append(PredictedIssue(
            rule="low-disk", severity="warning",
            message=f"only {fingerprint['disk_free_mb']} MB free on /; large installs may fail",
            fix_command="rm -rf /tmp/pip-* ~/.cache/pip 2>/dev/null; true", fix_description="Clear pip caches",
        ))
    return issues


# --------------------------------------------------------------------------- post-failure

_ERROR_RULES: list[tuple[str, str, str, str]] = [
    # (rule name, regex on combined output, sysdep package or '', description)
    ("pg_config-missing", r"pg_config executable not found", "psycopg2", "psycopg2 needs libpq headers"),
    ("mysql_config-missing", r"mysql_config not found|OSError: mysql_config", "mysqlclient", "mysqlclient needs MariaDB/MySQL headers"),
    ("libxml-missing", r"libxml/xmlversion\.h|xslt-config", "lxml", "lxml needs libxml2/libxslt headers"),
    ("cairo-missing", r"cairo\.h|No package 'cairo' found", "pycairo", "pycairo needs cairo headers"),
    ("jpeg-missing", r"jpeglib\.h|The headers or library files could not be found for jpeg", "pillow", "Pillow needs libjpeg headers"),
]


def match_error_rule(output: str, fingerprint: dict) -> RuleFix | None:
    """Deterministic post-failure lookup. First matching pattern wins."""
    pm = fingerprint.get("package_manager") or ""
    for rule, pattern, sysdep, desc in _ERROR_RULES:
        if re.search(pattern, output, re.IGNORECASE):
            cmd = _sysdep_fix(sysdep, pm)
            if cmd:
                return RuleFix(rule=rule, fix_command=cmd, description=desc, package=sysdep)

    if re.search(r"Python\.h: No such file", output):
        cmd = _install_cmd(pm, "python3-dev gcc" if pm == "apt-get" else "python3-devel gcc")
        if cmd:
            return RuleFix(rule="python-headers-missing", fix_command=cmd, description="C extension needs Python headers")
    if re.search(r"gcc: command not found|unable to execute 'gcc'|command 'gcc' failed|error: command 'cc' failed|cc: not found", output):
        cmd = _install_cmd(pm, "gcc python3-dev" if pm == "apt-get" else "gcc python3-devel")
        if cmd:
            return RuleFix(rule="compiler-missing", fix_command=cmd, description="Source build needs a C compiler")
    if re.search(r"git: (?:command )?not found", output):
        cmd = _install_cmd(pm, "git")
        if cmd:
            return RuleFix(rule="git-missing", fix_command=cmd, description="git missing on target")
    if re.search(r"No module named ['\"]?(pip|venv|ensurepip)", output) or "ensurepip is not available" in output:
        cmd = _install_cmd(pm, _pip_venv_pkgs(pm))
        if cmd:
            return RuleFix(rule="pip-or-venv-missing", fix_command=cmd, description="pip/venv missing")
    if re.search(r"ffi\.h: No such file", output):
        cmd = _install_cmd(pm, "libffi-dev" if pm == "apt-get" else "libffi-devel")
        if cmd:
            return RuleFix(rule="libffi-missing", fix_command=cmd, description="cffi needs libffi headers")
    if re.search(r"can't find Rust compiler|cargo.*not found", output, re.IGNORECASE):
        return RuleFix(rule="rust-needed-prefer-wheels", fix_command="pip install --upgrade pip setuptools wheel",
                       description="Newer pip resolves prebuilt wheels instead of building with Rust", stage="post")
    if re.search(r"No space left on device", output):
        return RuleFix(rule="disk-full", fix_command="rm -rf /tmp/pip-* ~/.cache/pip /root/.cache/pip 2>/dev/null; pip cache purge 2>/dev/null; true",
                       description="Free pip caches")
    if re.search(r"externally-managed-environment", output):
        return RuleFix(rule="pep668", fix_command="export PIP_BREAK_SYSTEM_PACKAGES=1",
                       description="Allow pip outside venv (fallback)", stage="post")
    return None
