#!/usr/bin/env python3
"""Per-core CPU stress test: find a faulty core by the same kind of load that crashed us.

Cross-platform (Linux + Windows). Background: this project suffered relentless, seemingly-random
segfaults (across pip, numpy, networkx, GDAL, even the stdlib json scanner). The kernel log showed
every fault on one physical core with corrupted instruction pointers — a hardware bit-flip on a
defective core, on a board with no ECC memory to catch it. This tool finds such a core.

How it works
------------
For each logical CPU it launches an isolated subprocess pinned to *only* that CPU (single-threaded
BLAS, so all work stays on the pinned core) and runs a heavy, fully deterministic workload in a loop
for a few seconds. A healthy core returns the exact same checksum every iteration and matches every
other healthy core. A faulty core is caught three independent ways:

  1. CRASH      — the subprocess dies from a fault (Linux: SIGSEGV/SIGILL/SIGBUS; Windows: an
                  access-violation / illegal-instruction NTSTATUS exit code). Attributed to that CPU.
  2. CORRUPTION — the workload's checksum changes between iterations on the same core, or disagrees
                  with the majority of other cores. Either means silently wrong arithmetic/memory.
  3. HANG       — the subprocess exceeds its time budget and is killed.

Because the fault is intermittent, increase --duration (and/or rerun) to raise the odds of catching
it. A clean pass is reassuring but not a guarantee; a single failure is conclusive.

Requirements
------------
Python 3 + numpy, and (recommended) psutil for CPU pinning. On Windows psutil is required for
affinity (`pip install psutil numpy`); on Linux it falls back to os.sched_setaffinity if psutil is
absent. Windows processor groups >64 CPUs are not handled (rare).

Usage
-----
    python core_stress_test.py                  # all CPUs, 20s each
    python core_stress_test.py --duration 120   # longer = more likely to catch an intermittent fault
    python core_stress_test.py --cpus 12,13     # specific logical CPUs
    python core_stress_test.py --array-mb 256   # bigger working set (more memory stress)
"""

from __future__ import annotations

import argparse
import hashlib
import os
import signal
import subprocess
import sys
import time

IS_WINDOWS = os.name == "nt"

# Worker exit codes (kept small and distinct from OS fault codes).
EXIT_OK = 0
EXIT_SELF_INCONSISTENT = 3   # checksum changed between iterations on this core
EXIT_MEMORY_MISMATCH = 4     # memory pattern read back wrong
EXIT_SETUP_ERROR = 5         # could not pin to the CPU (excluded from the verdict)

# Windows NTSTATUS fault codes that mean "the process crashed".
_NTSTATUS_FAULTS = {
    0xC0000005: "ACCESS_VIOLATION",
    0xC000001D: "ILLEGAL_INSTRUCTION",
    0xC00000FD: "STACK_OVERFLOW",
    0xC0000094: "INTEGER_DIVIDE_BY_ZERO",
    0xC0000409: "STACK_BUFFER_OVERRUN",
    0xC0000374: "HEAP_CORRUPTION",
    0x80000003: "BREAKPOINT",
}


# --------------------------------------------------------------------------------------------
# CPU pinning (cross-platform)
# --------------------------------------------------------------------------------------------
def pin_to_cpu(cpu: int) -> None:
    """Pin the current process to a single logical CPU. Raises on failure."""
    try:
        import psutil

        psutil.Process().cpu_affinity([cpu])
        return
    except ImportError:
        pass
    if hasattr(os, "sched_setaffinity"):          # Linux without psutil
        os.sched_setaffinity(0, {cpu})
        return
    if IS_WINDOWS:                                  # last-resort Windows fallback
        import ctypes

        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if not ctypes.windll.kernel32.SetProcessAffinityMask(handle, ctypes.c_size_t(1 << cpu)):
            raise OSError("SetProcessAffinityMask failed")
        return
    raise OSError("no CPU affinity mechanism available (install psutil)")


def list_cpus() -> list[int]:
    if hasattr(os, "sched_getaffinity"):
        return sorted(os.sched_getaffinity(0))
    try:
        import psutil

        return list(range(psutil.cpu_count(logical=True) or os.cpu_count() or 1))
    except ImportError:
        return list(range(os.cpu_count() or 1))


# --------------------------------------------------------------------------------------------
# Topology: map each logical CPU to its physical-core sibling set (best effort, never fatal)
# --------------------------------------------------------------------------------------------
def build_topology(cpus: list[int]) -> tuple[dict[int, set[int]], dict[int, str]]:
    """Return (siblings_map, label_map). siblings_map[cpu] = logical CPUs sharing its physical core."""
    try:
        if IS_WINDOWS:
            return _topology_windows()
        return _topology_linux(cpus)
    except Exception:
        pass
    # Fallback: treat every logical CPU as its own core (still correct, just no sibling grouping).
    return {c: {c} for c in cpus}, {c: "core ?" for c in cpus}


def _topology_linux(cpus: list[int]) -> tuple[dict[int, set[int]], dict[int, str]]:
    siblings: dict[int, set[int]] = {}
    labels: dict[int, str] = {}
    for c in cpus:
        base = f"/sys/devices/system/cpu/cpu{c}/topology"
        try:
            with open(f"{base}/thread_siblings_list") as f:
                sib = _parse_cpu_list(f.read().strip())
        except OSError:
            sib = {c}
        siblings[c] = sib
        try:
            with open(f"{base}/core_id") as f:
                labels[c] = f"core {int(f.read().strip())}"
        except OSError:
            labels[c] = "core ?"
    return siblings, labels


def _parse_cpu_list(text: str) -> set[int]:
    out: set[int] = set()
    for part in text.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            out.update(range(int(lo), int(hi) + 1))
        elif part:
            out.add(int(part))
    return out


def _topology_windows() -> tuple[dict[int, set[int]], dict[int, str]]:
    """Map logical CPUs to physical cores via GetLogicalProcessorInformationEx (RelationProcessorCore)."""
    import ctypes
    from ctypes import wintypes

    RelationProcessorCore = 0
    kernel32 = ctypes.windll.kernel32

    length = wintypes.DWORD(0)
    kernel32.GetLogicalProcessorInformationEx(RelationProcessorCore, None, ctypes.byref(length))
    buf = (ctypes.c_byte * length.value)()
    if not kernel32.GetLogicalProcessorInformationEx(RelationProcessorCore, buf, ctypes.byref(length)):
        raise OSError("GetLogicalProcessorInformationEx failed")

    siblings: dict[int, set[int]] = {}
    labels: dict[int, str] = {}
    offset = 0
    core_index = 0
    base = ctypes.addressof(buf)
    while offset < length.value:
        # SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX: DWORD Relationship; DWORD Size; union...
        relationship = ctypes.c_uint32.from_address(base + offset).value
        size = ctypes.c_uint32.from_address(base + offset + 4).value
        if relationship == RelationProcessorCore:
            # PROCESSOR_RELATIONSHIP at offset+8: BYTE Flags; BYTE EfficiencyClass; BYTE Reserved[20];
            # WORD GroupCount; GROUP_AFFINITY GroupMask[]  (GROUP_AFFINITY: KAFFINITY Mask; WORD Group; WORD Reserved[3])
            pr = base + offset + 8
            group_count = ctypes.c_uint16.from_address(pr + 22).value
            ga = pr + 24
            core_cpus: set[int] = set()
            for g in range(group_count):
                mask = ctypes.c_size_t.from_address(ga + g * 16).value
                group = ctypes.c_uint16.from_address(ga + g * 16 + ctypes.sizeof(ctypes.c_size_t)).value
                for bit in range(64):
                    if mask & (1 << bit):
                        core_cpus.add(group * 64 + bit)
            for c in core_cpus:
                siblings[c] = set(core_cpus)
                labels[c] = f"core {core_index}"
            core_index += 1
        offset += size
    return siblings, labels


# --------------------------------------------------------------------------------------------
# Worker: pinned to one CPU, runs the deterministic workload and self-checks
# --------------------------------------------------------------------------------------------
def run_worker(cpu: int, duration: float, array_mb: int) -> int:
    import numpy as np

    try:
        pin_to_cpu(cpu)
    except Exception as exc:
        print(f"[worker cpu{cpu}] cannot pin: {exc}", flush=True)
        return EXIT_SETUP_ERROR

    n_u64 = max(1, (array_mb * 1024 * 1024) // 8)  # uint64 elements for integer/memory workload

    # ---- integer + memory workload: identical across cores so healthy cores agree ----
    LCG_MUL = np.uint64(6364136223846793005)
    LCG_ADD = np.uint64(1442695040888963407)

    def integer_checksum() -> int:
        a = (np.arange(n_u64, dtype=np.uint64) * np.uint64(2654435761)) + np.uint64(0x9E3779B9)
        for _ in range(12):
            a = a * LCG_MUL + LCG_ADD          # ALU-heavy, full-array read+write (uint64 wraps mod 2**64)
            a ^= (a >> np.uint64(29))          # mixing
        return int(np.bitwise_xor.reduce(a))

    # ---- float / BLAS workload: mirrors the numpy-heavy pipeline load ----
    base = np.random.default_rng(0xC0FFEE).standard_normal((256, 256))

    def float_checksum() -> str:
        m = base.copy()
        for _ in range(16):
            m = m @ m.T
            mx = np.abs(m).max()
            if mx > 0:
                m = m / mx                     # keep bounded; bit-exact single-threaded
        return hashlib.sha256(np.ascontiguousarray(m).tobytes()).hexdigest()

    # ---- memory pattern verify: catch sticky/flipped bits in the working set ----
    pattern = np.uint64(0xA5A5A5A5A5A5A5A5)
    membuf = np.empty(n_u64, dtype=np.uint64)
    mem_xor = np.uint64(0x9E3779B97F4A7C15)
    expected_mem = pattern ^ mem_xor

    def memory_ok() -> bool:
        membuf[:] = pattern
        np.bitwise_xor(membuf, mem_xor, out=membuf)  # in-place, no name rebind
        return bool(np.all(membuf == expected_mem))

    ref_int = integer_checksum()
    ref_float = float_checksum()
    if not memory_ok():
        print(f"[worker cpu{cpu}] FAIL: memory pattern mismatch (pass 0)", flush=True)
        return EXIT_MEMORY_MISMATCH

    deadline = time.monotonic() + duration
    iters = 0
    while time.monotonic() < deadline:
        iters += 1
        if integer_checksum() != ref_int:
            print(f"[worker cpu{cpu}] FAIL: integer checksum changed at iter {iters}", flush=True)
            return EXIT_SELF_INCONSISTENT
        if float_checksum() != ref_float:
            print(f"[worker cpu{cpu}] FAIL: float/BLAS checksum changed at iter {iters}", flush=True)
            return EXIT_SELF_INCONSISTENT
        if not memory_ok():
            print(f"[worker cpu{cpu}] FAIL: memory mismatch at iter {iters}", flush=True)
            return EXIT_MEMORY_MISMATCH

    print(f"RESULT cpu={cpu} iters={iters} int={ref_int} float={ref_float}", flush=True)
    return EXIT_OK


# --------------------------------------------------------------------------------------------
# Parent: drive one subprocess per CPU, classify outcomes, cross-check, summarize
# --------------------------------------------------------------------------------------------
def classify_returncode(rc: int) -> tuple[str, str]:
    """Map a subprocess return code to (category, label). category in OK/WORKER/SETUP/CRASH/OTHER.

    Platform-agnostic. A Windows crash arrives as an NTSTATUS code in either its unsigned
    (e.g. 0xC0000005) or signed-negative (e.g. -1073741819) form — both normalize via &0xFFFFFFFF,
    so we test that BEFORE the POSIX-signal branch (which would otherwise grab the negative form).
    """
    if rc == EXIT_OK:
        return "OK", "ok"
    if rc in (EXIT_SELF_INCONSISTENT, EXIT_MEMORY_MISMATCH):
        return "WORKER", {EXIT_SELF_INCONSISTENT: "MISCALCULATION",
                          EXIT_MEMORY_MISMATCH: "MEMORY ERROR"}[rc]
    if rc == EXIT_SETUP_ERROR:
        return "SETUP", "could not pin (skipped)"

    u = rc & 0xFFFFFFFF  # normalize signed/unsigned 32-bit
    if u in _NTSTATUS_FAULTS:
        return "CRASH", _NTSTATUS_FAULTS[u]
    if (u & 0xF0000000) == 0xC0000000:  # NTSTATUS error severity = a Windows fault
        return "CRASH", f"NTSTATUS 0x{u:08X}"
    if rc < 0:  # POSIX: killed by signal -rc (signals are small, 1..~64)
        try:
            return "CRASH", signal.Signals(-rc).name
        except ValueError:
            return "CRASH", f"signal {-rc}"
    return "OTHER", f"exit {rc}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Find a faulty CPU core via per-core stress.")
    ap.add_argument("--duration", type=float, default=20.0, help="seconds of load per CPU (default 20)")
    ap.add_argument("--array-mb", type=int, default=128, help="working-set size per CPU in MiB (default 128)")
    ap.add_argument("--cpus", type=str, default="", help="comma-separated logical CPUs (default: all)")
    ap.add_argument("--_worker", type=int, default=-1, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args._worker >= 0:
        return run_worker(args._worker, args.duration, args.array_mb)

    cpus = [int(c) for c in args.cpus.split(",") if c.strip()] if args.cpus else list_cpus()
    siblings_map, label_map = build_topology(cpus)
    budget = args.duration + 30.0  # per-CPU hard timeout (also catches hangs)

    print(f"Platform: {'Windows' if IS_WINDOWS else os.name}.  Testing {len(cpus)} logical CPU(s): {cpus}")
    print(f"  per-CPU load: {args.duration:.0f}s, working set {args.array_mb} MiB, "
          f"single-threaded so work stays on the pinned core.\n")

    results: dict[int, dict] = {}
    for cpu in cpus:
        label = label_map.get(cpu, "core ?")
        print(f"  cpu{cpu:<3} ({label}) ... ", end="", flush=True)

        env = dict(os.environ)
        for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            env[var] = "1"

        cmd = [sys.executable, os.path.abspath(__file__),
               "--_worker", str(cpu),
               "--duration", str(args.duration),
               "--array-mb", str(args.array_mb)]
        t0 = time.monotonic()
        try:
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=budget)
        except subprocess.TimeoutExpired:
            print("HANG (timed out) -> SUSPECT")
            results[cpu] = {"status": "HANG", "int": None, "float": None}
            continue

        elapsed = time.monotonic() - t0
        category, label_text = classify_returncode(proc.returncode)
        out = proc.stdout.strip()

        if category == "OK":
            ints = floats = None
            for line in out.splitlines():
                if line.startswith("RESULT "):
                    kv = dict(p.split("=", 1) for p in line.split()[1:])
                    ints, floats = kv.get("int"), kv.get("float")
            print(f"ok ({elapsed:.0f}s)")
            results[cpu] = {"status": "OK", "int": ints, "float": floats}
        elif category == "CRASH":
            print(f"CRASH ({label_text}) after {elapsed:.0f}s -> FAULTY")
            results[cpu] = {"status": f"CRASH ({label_text})", "int": None, "float": None}
        elif category == "WORKER":
            print(f"{label_text} -> FAULTY")
            if out:
                print("      " + out.replace("\n", "\n      "))
            results[cpu] = {"status": label_text, "int": None, "float": None}
        elif category == "SETUP":
            print(label_text)
            results[cpu] = {"status": "SKIPPED", "int": None, "float": None}
        else:
            print(f"{label_text} -> SUSPECT")
            if out:
                print("      " + out.replace("\n", "\n      "))
            results[cpu] = {"status": label_text, "int": None, "float": None}

    # Cross-core majority vote: healthy cores agree; a minority that differs computed silently-wrong
    # results even without crashing.
    def majority(key: str):
        vals = [r[key] for r in results.values() if r["status"] == "OK" and r[key] is not None]
        return max(set(vals), key=vals.count) if vals else None

    maj_int, maj_float = majority("int"), majority("float")
    if maj_int is not None:
        for r in results.values():
            if r["status"] == "OK" and (r["int"] != maj_int or r["float"] != maj_float):
                r["status"] = "DISAGREES (wrong result vs majority)"

    # ---- summary ----
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    bad = {cpu: r for cpu, r in results.items()
           if r["status"] not in ("OK", "SKIPPED")}
    skipped = [cpu for cpu, r in results.items() if r["status"] == "SKIPPED"]
    if skipped:
        print(f"Skipped (could not pin): {skipped}")
    if not bad:
        print(f"All tested CPU(s) passed. No faulty core detected in this run.")
        print("Intermittent faults can still hide — rerun with a larger --duration to be surer.")
        return 0

    print("Suspect / faulty CPUs:")
    exclude: set[int] = set()
    for cpu, r in sorted(bad.items()):
        sibs = siblings_map.get(cpu, {cpu})
        exclude |= sibs
        print(f"  cpu{cpu:<3} ({label_map.get(cpu, 'core ?')})  ->  {r['status']}")

    excl = ",".join(map(str, sorted(exclude)))
    print(f"\nLogical CPUs to exclude (faulty cores + hyperthread siblings): {excl}")
    print(f"\n  Run the pipeline avoiding them:   CAP_EXCLUDE_CPUS={excl} python main.py")
    if IS_WINDOWS:
        mask = sum(1 << c for c in list_cpus() if c not in exclude)
        print(f"  Launch any program off them:      start /affinity {mask:X} <program>")
    else:
        print(f"  Offline them at the OS level:     sudo chcpu -d {excl}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
