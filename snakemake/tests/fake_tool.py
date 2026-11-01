"""Fake command-line tools for exercising real Snakemake jobs without databases."""

import json
import os
import shutil
import sys
import time
from pathlib import Path


name, args = sys.argv[1], sys.argv[2:]
if name == "java":
    name, args = "trimmomatic", args[2:]
sample = next((sample for sample in ("paired", "single") if "biopipe-" + sample + "-" in " ".join(args)), "unknown")


def event(action):
    target = os.environ.get("BIOPIPE_TEST_EVENTS")
    if target:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, (json.dumps({"tool": name, "sample": sample, "action": action, "time": time.monotonic(), "args": args}) + "\n").encode())
        finally:
            os.close(descriptor)


event("start")
failure = os.environ.get("BIOPIPE_TEST_FAILURE")
if failure and Path(failure).exists() and Path(failure).read_text().strip() == name + ":" + sample:
    sys.exit("injected failure: " + name + ":" + sample)

if name == "trimmomatic":
    time.sleep(float(os.environ.get("BIOPIPE_TEST_DELAY", "0")))
    start = args.index("-threads") + 2
    outputs = args[start + 2:start + 6] if "PE" in args else [args[start + 1]]
elif name in ("ngm", "cutadapt"):
    outputs = [args[args.index("-o") + 1]]
elif name == "samtools":
    outputs = ([args[args.index(flag) + 1] for flag in ("-1", "-2", "-0") if flag in args]
               if args[0] == "fastq" else [args[args.index("-o") + 1]])
elif name == "kraken2":
    outputs = [args[args.index(flag) + 1] for flag in ("--report", "--output")]
else:
    sys.exit("unknown tool: " + name)

for item in outputs:
    if item != os.devnull:
        if name == "cutadapt":
            shutil.copyfile(args[-1], item)
        else:
            Path(item).write_bytes(("fake " + name + " output\n").encode())
event("end")
