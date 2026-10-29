"""Run a workspace's unittest suite inside the sandbox and return evidence.

The pass/fail verdict needs BOTH a zero exit code and a result line carrying a per-run random
nonce, so a suite that calls os._exit(0) early, or prints a fake summary, does not count as
passing. Agent-written tests can still be weak or wrong, so test evidence complements human
review rather than replacing it.
"""
import re
import secrets

from . import sandbox

RUNNER = r'''
import sys, unittest
sys.path.insert(0, ".")
_suite = unittest.defaultTestLoader.discover(".", pattern="test*.py", top_level_dir=".")
_res = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(_suite)
print("VCOOS_RESULT_%(nonce)s tests=%%d failures=%%d ok=%%s" %% (_res.testsRun, len(_res.failures) + len(_res.errors), _res.wasSuccessful()))
sys.exit(0 if _res.wasSuccessful() and _res.testsRun > 0 else 1)
'''

COMMAND = "python -m unittest discover -p 'test*.py' (sandboxed)"


def run_tests(workspace, wall_seconds=60):
    nonce = secrets.token_hex(16)
    result = sandbox.run_python(RUNNER % {"nonce": nonce}, workspace, wall_seconds=wall_seconds, cpu_seconds=min(wall_seconds, 50))
    out = result["stdout"] + ("\n--- stderr ---\n" + result["stderr"] if result["stderr"] else "")
    m = re.search(rf"VCOOS_RESULT_{nonce} tests=(\d+) failures=(\d+) ok=(True|False)", result["stdout"])
    tests_run = int(m.group(1)) if m else None
    failures = int(m.group(2)) if m else None
    passed = bool(m) and m.group(3) == "True" and result["exit_code"] == 0 and (tests_run or 0) > 0 and not result["timed_out"]
    return {"passed": passed, "exit_code": result["exit_code"], "tests_run": tests_run, "failures": failures,
            "timed_out": result["timed_out"], "output": out[-20000:]}
