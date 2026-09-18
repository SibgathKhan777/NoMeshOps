"""Held-out generalization test: real pip failure logs pasted verbatim from public issue trackers.
These were NOT written for this project and come from other pip versions / OSes than the ones tested locally.
Sources: psycopg2#451, Unity ml-agents#6008, mysqlclient discussion#756 (see SOURCES below)."""
import json, os, sys
sys.path.insert(0, ".")
from app.nodes import signature, rules

SOURCES = {
 "psycopg2#451": "https://github.com/psycopg/psycopg2/issues/451",
 "ml-agents#6008": "https://github.com/Unity-Technologies/ml-agents/issues/6008",
 "mysqlclient#756": "https://github.com/PyMySQL/mysqlclient/discussions/756",
}

PSYCOPG2 = """Collecting psycopg2
  Downloading psycopg2-2.6.2.tar.gz (376kB)
    Complete output from command python setup.py egg_info:
    running egg_info
    creating pip-egg-info\\psycopg2.egg-info
    writing pip-egg-info\\psycopg2.egg-info\\PKG-INFO
    writing top-level names to pip-egg-info\\psycopg2.egg-info\\top_level.txt
    writing dependency_links to pip-egg-info\\psycopg2.egg-info\\dependency_links.txt
    writing manifest file 'pip-egg-info\\psycopg2.egg-info\\SOURCES.txt'
    warning: manifest_maker: standard file '-c' not found

    Error: pg_config executable not found.

    Please add the directory containing pg_config to the PATH
    or specify the full executable path with the option:

        python setup.py build_ext --pg-config /path/to/pg_config build ...

    or with the pg_config option in 'setup.cfg'."""

NUMPY = """Collecting numpy<2.0,>=1.13.3 (from mlagents==1.0.0)
  Using cached numpy-1.21.2.zip (10.3 MB)
  Installing build dependencies ... done
  Getting requirements to build wheel ... done
  Preparing metadata (pyproject.toml) ... done
Building wheel for numpy (pyproject.toml) ... error
error: subprocess-exited-with-error

x Building wheel for numpy (pyproject.toml) did not run successfully.
| exit code: 1
+-> [287 lines of output]
    setup.py:63: RuntimeWarning: NumPy 1.21.2 may not yet support Python 3.10.
      warnings.warn(
    Running from numpy source directory.
    Cythonizing sources
    TypeError: CCompiler_spawn() got an unexpected keyword argument 'env'
    [end of output]

note: This error originates from a subprocess, and is likely not a problem with pip.
ERROR: Failed building wheel for numpy
Successfully built mlagents
Failed to build numpy
ERROR: Could not build wheels for numpy, which is required to install pyproject.toml-based projects"""

MYSQLCLIENT = """Collecting mysqlclient
  Using cached mysqlclient-2.2.7.tar.gz (91 kB)
  Installing build dependencies ... done
  Getting requirements to build wheel ... done
  Preparing metadata (pyproject.toml) ... done
Building wheels for collected packages: mysqlclient
  Building wheel for mysqlclient (pyproject.toml) ... error
  error: subprocess-exited-with-error

  x Building wheel for mysqlclient (pyproject.toml) did not run successfully.
  | exit code: 1
  +-> [41 lines of output]
      Trying pkg-config --exists mysqlclient
      # Options for building extension module:
        extra_compile_args: ['-I/usr/include/mysql', '-std=c99']
      running bdist_wheel
      creating build/lib.linux-x86_64-cpython-311/MySQLdb
      src/MySQLdb/_mysql.c:52:10: fatal error: Python.h: No such file or directory
         52 | #include "Python.h"
            |          ^~~~~~~~~~
      compilation terminated.
      error: command '/usr/bin/x86_64-linux-gnu-gcc' failed with exit code 1
      [end of output]

  note: This error originates from a subprocess, and is likely not a problem with pip.
  ERROR: Failed building wheel for mysqlclient
Failed to build mysqlclient
ERROR: Failed to build installable wheels for some pyproject.toml based projects (mysqlclient)"""

UBUNTU = {"os":"ubuntu","os_version":"22.04","os_key":"ubuntu22","arch":"x86_64","python_version":"3.11.2","python_minor":"3.11",
          "has_pip":True,"has_venv":True,"has_git":True,"has_gcc":True,"package_manager":"apt-get","disk_free_mb":9000}

CASES = [
 ("psycopg2#451", PSYCOPG2, "BuildError", "psycopg2", "pg_config-missing", "libpq-dev"),
 ("ml-agents#6008", NUMPY, "BuildError", "numpy", None, None),
 ("mysqlclient#756", MYSQLCLIENT, "BuildError", "mysqlclient", "python-headers-missing", "python3-dev"),
]

rows = []
for name, log, exp_type, exp_pkg, exp_rule, fix_contains in CASES:
    info = signature.compute_signature(signature.classify("", log, "install"), UBUNTU)
    hit = rules.match_error_rule(log, UBUNTU)
    got_rule = hit.rule if hit else None
    type_ok = info.error_type == exp_type
    pkg_ok = info.package == exp_pkg
    rule_ok = got_rule == exp_rule
    fix_ok = (fix_contains is None) or (hit is not None and fix_contains in hit.fix_command)
    rows.append({"case": name, "source": SOURCES[name], "expected_type": exp_type, "actual_type": info.error_type,
                 "expected_pkg": exp_pkg, "actual_pkg": info.package, "actual_version": info.package_version,
                 "expected_rule": exp_rule, "actual_rule": got_rule, "fix": hit.fix_command[:75] if hit else None,
                 "signature": info.signature, "message": info.message[:70],
                 "type_ok": type_ok, "pkg_ok": pkg_ok, "rule_ok": rule_ok, "fix_ok": fix_ok})
    print(f"{name:18s} type={info.error_type:14s}({'ok' if type_ok else 'MISS'}) pkg={info.package or '-':14s}({'ok' if pkg_ok else 'MISS'}) "
          f"rule={str(got_rule):24s}({'ok' if rule_ok else 'MISS'}) fix={'ok' if fix_ok else 'MISS'}")
    print(f"{'':18s} msg={info.message[:95]!r}")

n = len(CASES)
summary = {"n": n, "type_accuracy": sum(r["type_ok"] for r in rows)/n, "package_accuracy": sum(r["pkg_ok"] for r in rows)/n,
           "rule_routing_accuracy": sum(r["rule_ok"] for r in rows)/n, "fix_correct": sum(r["fix_ok"] for r in rows)/n, "rows": rows}
os.makedirs("evals/results", exist_ok=True)
json.dump(summary, open("evals/results/realworld_logs.json", "w"), indent=2)
print("\nsummary:", {k: v for k, v in summary.items() if k != "rows"})
