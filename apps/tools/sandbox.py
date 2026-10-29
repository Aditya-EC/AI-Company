"""Isolated Python execution for agent-generated code.

Layers (defense in depth):
1. Separate process, isolated interpreter (-I), empty environment (no credentials), cwd = task workspace.
2. OS resource limits: CPU time, address space, file size, open files, process count.
3. Wall-clock timeout with process-group kill.
4. In-process audit hook (PEP 578) denying network, subprocesses, ctypes and filesystem access outside
   the workspace (read-only access to the Python stdlib is allowed).
5. Optional OS-level wrapper (VCOOS_SANDBOX_WRAPPER, e.g. bubblewrap or `unshare -n`) - strongly
   recommended in production, since an audit hook alone is not a hard security boundary.
"""
import os
import shlex
import signal
import subprocess
import sys
import sysconfig

BOOTSTRAP = r'''
import os, sys
WS = os.path.realpath(sys.argv[1])
READ_OK = tuple({os.path.realpath(p) for p in sys.argv[2:] if p})
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
BLOCKED_PREFIXES = ("socket.", "subprocess.", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.system",
                    "os.kill", "os.killpg", "ctypes.", "winreg.", "sys.addaudithook", "os.chroot", "os.setuid")
PATH_EVENTS = {"os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.symlink", "os.link",
               "os.truncate", "os.utime", "shutil.rmtree", "shutil.copyfile", "shutil.move", "os.listdir", "os.scandir", "os.chdir"}
code = sys.stdin.read()

def inside(p):
    p = os.path.realpath(os.fsdecode(p))
    return p == WS or p.startswith(WS + os.sep)

def writable(p):
    # Never let agent code touch git metadata (it could redirect server-side git operations).
    rel = os.path.relpath(os.path.realpath(os.fsdecode(p)), WS)
    return inside(p) and ".git" not in rel.split(os.sep)

def readable(p):
    p = os.path.realpath(os.fsdecode(p))
    return inside(p) or p.startswith(READ_OK)

def hook(event, args):
    if event.startswith(BLOCKED_PREFIXES):
        raise PermissionError(f"sandbox: {event} is not permitted")
    if event == "import" and args and args[0] in ("ctypes", "_ctypes", "socket", "_socket", "multiprocessing", "subprocess"):
        raise PermissionError(f"sandbox: import of {args[0]} is not permitted")
    if event == "open":
        path, mode, flags = args
        if isinstance(path, int) or path is None:
            return
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (isinstance(flags, int) and flags & WRITE_FLAGS)
        if (writing and not writable(path)) or not readable(path):
            raise PermissionError("sandbox: file access outside the workspace is not permitted")
    if event in PATH_EVENTS:
        for a in args:
            if isinstance(a, (str, bytes, os.PathLike)):
                ok = readable(a) if event in ("os.listdir", "os.scandir") else writable(a)
                if not ok:
                    raise PermissionError(f"sandbox: {event} outside the workspace is not permitted")

sys.addaudithook(hook)
del hook
_compiled = compile(code, "<agent-code>", "exec")
del code
exec(_compiled, {"__name__": "__main__"})
'''

OUTPUT_LIMIT = 64 * 1024


def _limits(cpu_seconds, memory_mb):
    def apply():
        import resource

        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        except (ValueError, OSError):
            pass
    return apply


def run_python(code, workspace, *, wall_seconds=20, cpu_seconds=10, memory_mb=256):
    paths = sysconfig.get_paths()
    read_ok = [paths.get("stdlib"), paths.get("platstdlib"), sys.base_prefix]
    cmd = [sys.executable, "-I", "-S", "-c", BOOTSTRAP, str(workspace), *[p for p in read_ok if p]]
    wrapper = os.environ.get("VCOOS_SANDBOX_WRAPPER", "").strip()
    if wrapper:
        cmd = shlex.split(wrapper) + cmd
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(workspace),
                                env={"PYTHONIOENCODING": "utf-8", "HOME": str(workspace)}, preexec_fn=_limits(cpu_seconds, memory_mb))
    except OSError as exc:
        return {"exit_code": -1, "stdout": "", "stderr": f"sandbox failed to start: {exc}", "timed_out": False}
    try:
        out, err = proc.communicate(code.encode(), timeout=wall_seconds)
        timed_out = False
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        timed_out = True
    return {
        "exit_code": proc.returncode,
        "stdout": out[:OUTPUT_LIMIT].decode(errors="replace"),
        "stderr": err[:OUTPUT_LIMIT].decode(errors="replace"),
        "timed_out": timed_out,
    }
