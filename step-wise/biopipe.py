"""Step-wise bioinformatics runner for local files and HDFS, one Spark job per step.

Each step (trim, map, unmapped, fastq, classify) is a separate unit of work per
sample. A step reads its inputs from shared storage, writes its outputs back, and
publishes a completion marker, so a rerun resumes at the first unfinished step.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import resource
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


def join_path(*parts: str) -> str:
    return "/".join(part.rstrip("/") if i == 0 else part.strip("/") for i, part in enumerate(parts))


def bytes_in(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def max_child_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


class LocalStorage:
    def get(self, source: str, target: Path) -> None:
        origin = Path(source)
        if not origin.exists():
            raise FileNotFoundError(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_dir():
            shutil.copytree(origin, target)
        else:
            shutil.copy2(origin, target)

    def put_once(self, source: Path, target: str) -> bool:
        destination = Path(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".partial")
        try:
            shutil.copy2(source, temporary)
            try:
                os.link(temporary, destination)
                return True
            except FileExistsError:
                return False
        finally:
            temporary.unlink(missing_ok=True)

    def exists(self, path: str) -> bool:
        return Path(path).is_file()

    def read_text(self, path: str) -> str:
        return Path(path).read_text()


class HdfsStorage:
    """Use the standard Hadoop FileSystem shell and the site's Hadoop config."""

    def __init__(self) -> None:
        if not shutil.which("hadoop"):
            raise RuntimeError("hadoop CLI is required for HDFS storage")

    @staticmethod
    def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["hadoop", "fs", *args], check=check, capture_output=True, text=True)

    def get(self, source: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        self._run("-get", source, str(target))

    def put_once(self, source: Path, target: str) -> bool:
        parent = target.rsplit("/", 1)[0]
        temporary = target + "." + uuid.uuid4().hex + ".partial"
        self._run("-mkdir", "-p", parent)
        self._run("-put", str(source), temporary)
        try:
            result = self._run("-mv", temporary, target, check=False)
            if result.returncode == 0:
                return True
            if self.exists(target):
                return False
            raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
        finally:
            self._run("-rm", "-f", temporary, check=False)

    def exists(self, path: str) -> bool:
        result = self._run("-test", "-e", path, check=False)
        # Exit 1 is "missing". Hadoop clients often print WARN/log4j noise on
        # stderr even then, so only other messages or exit codes are errors.
        noise = ("WARN", "log4j:", "SLF4J:")
        errors = [line for line in result.stderr.splitlines() if line.strip() and not any(token in line for token in noise)]
        if result.returncode not in (0, 1) or (result.returncode and errors):
            raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
        return result.returncode == 0

    def read_text(self, path: str) -> str:
        return self._run("-cat", path).stdout


def storage_for(config: dict):
    return LocalStorage() if config["storage"] == "local" else HdfsStorage()


def run_command(command: list[str], log: Path) -> None:
    with log.open("a") as stream:
        stream.write("$ " + shlex.join(command) + "\n")
        stream.flush()
        subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True)


@dataclass
class State:
    sample: dict
    config: dict
    scratch: Path
    storage: object
    log: Path
    paths: dict[str, Path] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    metrics: dict = field(default_factory=lambda: {"transfers": [], "commands": [], "stages": []})
    started: float = field(default_factory=time.monotonic)

    def command(self, args: list[str | Path | int | float]) -> None:
        command = [str(arg) for arg in args]
        started = time.monotonic()
        try:
            run_command(command, self.log)
        finally:
            self.metrics["commands"].append({
                "tool": Path(command[0]).name,
                "seconds": round(time.monotonic() - started, 3),
                "max_child_rss_bytes": max_child_rss_bytes(),
            })

    def fetch(self, source: str, group: str) -> Path:
        target = self.scratch / group / source.rstrip("/").rsplit("/", 1)[-1]
        started = time.monotonic()
        self.storage.get(source, target)
        size = bytes_in(target)
        self.metrics["transfers"].append({
            "direction": "in", "source": source, "bytes": size,
            "seconds": round(time.monotonic() - started, 3),
        })
        return target

    def fetch_resource(self, source: str, group: str) -> Path:
        """Fetch a versioned reference, through the node-local cache when configured."""
        cache = self.config.get("reference_cache")
        if not cache:
            return self.fetch(source, group)
        key = hashlib.sha256((self.config["resources_version"] + "\0" + source).encode()).hexdigest()[:20]
        entry = Path(cache) / key
        final = entry / source.rstrip("/").rsplit("/", 1)[-1]
        started = time.monotonic()
        hit = final.exists()
        if not hit:
            entry.parent.mkdir(parents=True, exist_ok=True)
            # One task per node populates an entry; others block, then reuse it.
            with open(entry.parent / (key + ".lock"), "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                hit = final.exists()
                if not hit:
                    staging = Path(tempfile.mkdtemp(prefix=key + ".", suffix=".partial", dir=entry.parent))
                    try:
                        self.storage.get(source, staging / final.name)
                        os.replace(staging, entry)
                    finally:
                        shutil.rmtree(staging, ignore_errors=True)
        self.metrics["transfers"].append({
            "direction": "in", "source": source, "bytes": 0 if hit else bytes_in(final),
            "seconds": round(time.monotonic() - started, 3), "cache_hit": hit,
        })
        return final

    def upload(self, source: Path, target: str) -> bool:
        started = time.monotonic()
        created = self.storage.put_once(source, target)
        if created:
            self.metrics["transfers"].append({
                "direction": "out", "target": target, "bytes": source.stat().st_size,
                "seconds": round(time.monotonic() - started, 3),
            })
        return created


STEPS = ("trim", "map", "unmapped", "fastq", "classify")


def sample_paired(sample: dict) -> bool:
    return bool(sample.get("r2"))


def step_trim(state: State, upstream: dict) -> dict[str, Path]:
    c, sample, resources = state.config, state.sample, state.config["resources"]
    t, p = c["trim"], state.paths
    p["r1"] = state.fetch(sample["r1"], "inputs/r1")
    if sample_paired(sample):
        p["r2"] = state.fetch(sample["r2"], "inputs/r2")
    if resources.get("trimmomatic_jar"):
        p["jar"] = state.fetch_resource(resources["trimmomatic_jar"], "resources/jar")
    adapter = "adapter_pair" if sample_paired(sample) else "adapter_single"
    p["adapter"] = state.fetch_resource(resources[adapter], "resources/adapter")
    prefix = state.scratch / "trim"
    prefix.mkdir()
    launcher = [c["tools"]["java"], "-jar", p["jar"]] if "jar" in p else [c["tools"]["trimmomatic"]]
    common = launcher + ["PE" if "r2" in p else "SE", "-" + t["phred"], "-threads", c["threads"]]
    filters = [
        "ILLUMINACLIP:" + str(p["adapter"]) + ":2:30:10" + (":2:keepBothReads" if "r2" in p else ""),
        "LEADING:" + str(t["leading"]),
        "TRAILING:" + str(t["trailing"]),
        "SLIDINGWINDOW:" + str(t["sliding_window"]),
        "MINLEN:" + str(t["min_len"]),
    ]
    outputs = {"trim_r1": prefix / "R1.fastq.gz"}
    if "r2" in p:
        outputs["trim_r2"] = prefix / "R2.fastq.gz"
        unpaired = [prefix / "unpaired_R1.fastq.gz", prefix / "unpaired_R2.fastq.gz"]
        state.command(common + [p["r1"], p["r2"], outputs["trim_r1"], unpaired[0], outputs["trim_r2"], unpaired[1]] + filters)
        if t.get("cutadapt_r2_bases", 0):
            cut = prefix / "cut_R2.fastq.gz"
            state.command([c["tools"]["cutadapt"], "-j", c["threads"], "-u", t["cutadapt_r2_bases"], "-o", cut, outputs["trim_r2"]])
            os.replace(cut, outputs["trim_r2"])
    else:
        state.command(common + [p["r1"], outputs["trim_r1"]] + filters)
    return outputs


def step_map(state: State, upstream: dict) -> dict[str, Path]:
    c, resources, m = state.config, state.config["resources"], state.config["mapping"]
    reads = [state.fetch(upstream[name], "inputs/trim") for name in ("trim_r1", "trim_r2") if name in upstream]
    reference_dir = state.fetch_resource(resources["human_reference_dir"], "resources/human")
    fasta = reference_dir / resources["human_fasta_name"]
    if not fasta.is_file():
        raise FileNotFoundError(fasta)
    sam = state.scratch / "mapped.sam"
    command = [c["tools"]["ngm"], "-i", m["min_identity"], "-R", m["min_residues"]]
    command += ["-r", fasta, "--silent-clip", "-o", sam, "-t", c["threads"], "--no-progress"]
    command += ["-1", reads[0], "-2", reads[1]] if len(reads) == 2 else ["-q", reads[0]]
    if m.get("very_fast"):
        command.append("--very-fast")
    state.command(command)
    return {"sam": sam}


def step_unmapped(state: State, upstream: dict) -> dict[str, Path]:
    c = state.config
    sam = state.fetch(upstream["sam"], "inputs/sam")
    bam = state.scratch / "unmapped.bam"
    state.command([c["tools"]["samtools"], "view", "-b", "-f", "4", "-@", c["threads"], "-o", bam, sam])
    return {"bam": bam}


def step_fastq(state: State, upstream: dict) -> dict[str, Path]:
    c = state.config
    bam = state.fetch(upstream["bam"], "inputs/bam")
    sorted_bam = state.scratch / "name_sorted.bam"
    state.command([c["tools"]["samtools"], "sort", "-n", "-@", c["threads"], "-o", sorted_bam, bam])
    outputs = {"fastq_r1": state.scratch / "nonhuman_R1.fastq.gz"}
    if sample_paired(state.sample):
        outputs["fastq_r2"] = state.scratch / "nonhuman_R2.fastq.gz"
        state.command([c["tools"]["samtools"], "fastq", "-@", c["threads"], "-1", outputs["fastq_r1"], "-2", outputs["fastq_r2"], "-0", os.devnull, "-s", os.devnull, sorted_bam])
    else:
        state.command([c["tools"]["samtools"], "fastq", "-@", c["threads"], "-0", outputs["fastq_r1"], sorted_bam])
    return outputs


def step_classify(state: State, upstream: dict) -> dict[str, Path]:
    c = state.config
    reads = [state.fetch(upstream[name], "inputs/fastq") for name in ("fastq_r1", "fastq_r2") if name in upstream]
    database = state.fetch_resource(c["resources"]["kraken_db"], "resources/kraken")
    if not database.is_dir():
        raise NotADirectoryError(database)
    report = state.scratch / "kraken_report.txt"
    outputs = {"report": report}
    command = [c["tools"]["kraken2"], "--db", database, "--threads", c["threads"], "--report", report]
    if c["publish"].get("kraken_output"):
        outputs["kraken_output"] = state.scratch / "kraken_output.txt"
    command += ["--output", outputs.get("kraken_output", os.devnull)]
    if c["kraken"].get("memory_mapping", True):
        command.append("--memory-mapping")
    command += ["--paired", *reads] if len(reads) == 2 else reads
    state.command(command)
    if not report.is_file():
        raise FileNotFoundError(report)
    return outputs


STEP_FUNCTIONS = {"trim": step_trim, "map": step_map, "unmapped": step_unmapped, "fastq": step_fastq, "classify": step_classify}


# Operational settings that do not change results, so changing them keeps markers valid.
NON_SCIENCE_KEYS = ("samples", "scratch_root", "output_root", "threads", "reference_cache")


def fingerprint(config: dict, sample: dict) -> str:
    execution = {key: value for key, value in config.items() if key not in NON_SCIENCE_KEYS}
    return hashlib.sha256(json.dumps({"execution": execution, "sample": sample}, sort_keys=True).encode()).hexdigest()


def step_marker(config: dict, sample: dict, step: str) -> str:
    return join_path(config["output_root"], sample["id"], "steps", fingerprint(config, sample), step + ".done.json")


def checked_marker(storage: object, marker: str, expected: str) -> dict | None:
    if not storage.exists(marker):
        return None
    done = json.loads(storage.read_text(marker))
    if done.get("fingerprint") != expected or not done.get("outputs"):
        raise RuntimeError("completion marker is invalid: " + marker)
    if not all(storage.exists(path) for path in done["outputs"].values()):
        raise RuntimeError("completion marker references missing outputs: " + marker)
    return done


def run_step(sample: dict, config: dict, step: str, storage: object | None = None) -> dict:
    """Run one step for one sample. Returns completed, skipped, duplicate or blocked."""
    index = STEPS.index(step)
    storage = storage or storage_for(config)
    digest = fingerprint(config, sample)
    marker = step_marker(config, sample, step)
    base = {"sample": sample["id"], "step": step}
    done = checked_marker(storage, marker, digest)
    if done:
        return {**base, "status": "skipped", "outputs": done["outputs"], "metrics": done["metrics"]}
    upstream: dict[str, str] = {}
    if index:
        previous = checked_marker(storage, step_marker(config, sample, STEPS[index - 1]), digest)
        if previous is None:
            return {**base, "status": "blocked", "error": "step " + STEPS[index - 1] + " is not finished"}
        upstream = previous["outputs"]
    attempt = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="biopipe-" + sample["id"] + "-" + step + "-", dir=config["scratch_root"]) as directory:
        scratch = Path(directory)
        state = State(sample, config, scratch, storage, scratch / "step.log")
        root = join_path(config["output_root"], sample["id"], "steps", digest, step, attempt)
        try:
            produced = STEP_FUNCTIONS[step](state, upstream)
            produced["log"] = state.log
            for name, source in produced.items():
                if not source.is_file():
                    raise FileNotFoundError(source)
        except Exception as error:
            failure = scratch / "failure.json"
            failure.write_text(json.dumps({
                "sample": sample["id"], "step": step, "attempt": attempt,
                "error": type(error).__name__ + ": " + str(error),
                "elapsed_seconds": round(time.monotonic() - state.started, 3),
                "metrics": state.metrics,
            }, sort_keys=True) + "\n")
            try:
                if state.log.is_file():
                    state.upload(state.log, join_path(root, "step.log"))
                state.upload(failure, join_path(root, "failure.json"))
            except Exception:
                # Preserve the tool/storage error; the caller still reports it.
                pass
            raise
        for name, source in produced.items():
            target = join_path(root, source.name)
            if not state.upload(source, target):
                raise RuntimeError("unexpected output collision: " + target)
            state.outputs[name] = target
        state.metrics["elapsed_seconds"] = round(time.monotonic() - state.started, 3)
        state.metrics["input_bytes"] = sum(item["bytes"] for item in state.metrics["transfers"] if item["direction"] == "in")
        state.metrics["output_bytes"] = sum(item["bytes"] for item in state.metrics["transfers"] if item["direction"] == "out")
        state.metrics["max_child_rss_bytes"] = max_child_rss_bytes()
        payload = {**base, "fingerprint": digest, "attempt": attempt, "outputs": state.outputs, "metrics": state.metrics}
        local_marker = scratch / "done.json"
        local_marker.write_text(json.dumps(payload, sort_keys=True) + "\n")
        # The marker is excluded from transfer totals: it carries them and is published last.
        if not storage.put_once(local_marker, marker):
            winner = checked_marker(storage, marker, digest)
            if winner is None:
                raise RuntimeError("completion marker race could not be resolved")
            return {**base, "status": "duplicate", "outputs": winner["outputs"], "metrics": winner["metrics"]}
        return {**base, "status": "completed", "outputs": state.outputs, "metrics": state.metrics}


def process_sample(sample: dict, config: dict, steps: tuple[str, ...] = STEPS) -> list[dict]:
    """Run steps one after another for one sample, without Spark. Stops at the first failure."""
    storage = storage_for(config)
    results = []
    for step in steps:
        results.append(run_step(sample, config, step, storage))
        if results[-1]["status"] == "blocked":
            break
    return results


def parse_steps(text: str | None) -> tuple[str, ...]:
    if not text:
        return STEPS
    chosen = tuple(item.strip() for item in text.split(",") if item.strip())
    unknown = [item for item in chosen if item not in STEPS]
    if unknown or list(chosen) != [step for step in STEPS if step in chosen]:
        raise ValueError("steps must be a subset of " + ",".join(STEPS) + " in pipeline order")
    return chosen


def validate_config(config: dict) -> dict:
    if config.get("storage") not in ("local", "hdfs"):
        raise ValueError("storage must be local or hdfs")
    required = ("output_root", "scratch_root", "resources_version", "resources", "tools", "trim", "mapping", "kraken", "publish", "samples", "threads")
    for name in required:
        if name not in config:
            raise ValueError("missing configuration field: " + name)
    if type(config["threads"]) is not int or config["threads"] < 1:
        raise ValueError("threads must be a positive integer")
    if not config["samples"]:
        raise ValueError("samples must contain at least one sample")
    for name in ("scratch_root", "output_root", "resources_version"):
        if not isinstance(config[name], str) or not config[name]:
            raise ValueError(name + " must be a nonempty string")
    if not Path(config["scratch_root"]).is_absolute():
        raise ValueError("scratch_root must be an absolute local path")
    cache = config.get("reference_cache")
    if cache is not None and (not isinstance(cache, str) or not Path(cache).is_absolute()):
        raise ValueError("reference_cache must be an absolute local path")
    if config["storage"] == "local" and not Path(config["output_root"]).is_absolute():
        raise ValueError("local output_root must be an absolute path")
    if config["storage"] == "hdfs" and not config["output_root"].startswith("hdfs://"):
        raise ValueError("HDFS output_root must use hdfs://")
    for name in ("adapter_pair", "adapter_single", "human_reference_dir", "human_fasta_name", "kraken_db"):
        if not config["resources"].get(name):
            raise ValueError("missing resource: " + name)
    for name in ("ngm", "samtools", "kraken2"):
        if not config["tools"].get(name):
            raise ValueError("missing tool: " + name)
    if config["resources"].get("trimmomatic_jar"):
        if not config["tools"].get("java"):
            raise ValueError("java is required with trimmomatic_jar")
    elif not config["tools"].get("trimmomatic"):
        raise ValueError("trimmomatic tool is required without trimmomatic_jar")
    if config["trim"].get("cutadapt_r2_bases", 0) and not config["tools"].get("cutadapt"):
        raise ValueError("cutadapt tool is required when cutadapt_r2_bases is set")
    seen = set()
    for sample in config["samples"]:
        sample_id = sample.get("id")
        if not isinstance(sample_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", sample_id) or sample_id in (".", ".."):
            raise ValueError("sample id must be a safe path component")
        if sample_id in seen:
            raise ValueError("duplicate sample id: " + sample_id)
        seen.add(sample_id)
        if not sample.get("r1") or sample.get("r2") == sample["r1"]:
            raise ValueError("sample needs r1 and an optional distinct r2: " + sample_id)
        if not isinstance(sample.get("revision"), str) or not sample["revision"]:
            raise ValueError("sample revision is required: " + sample_id)
        if config["storage"] == "hdfs" and not all(
            path.startswith("hdfs://") for path in (sample["r1"], sample.get("r2") or sample["r1"])
        ):
            raise ValueError("HDFS sample paths must use hdfs://")
    if config["storage"] == "hdfs":
        for name, path in config["resources"].items():
            if path is not None and name != "human_fasta_name" and not path.startswith("hdfs://"):
                raise ValueError("HDFS resource paths must use hdfs://: " + name)
    return config

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Local JSON configuration file")
    parser.add_argument("--mode", choices=("validate", "run"), default="validate")
    parser.add_argument("--steps", help="Comma separated steps in order (default: all of " + ",".join(STEPS) + ")")
    parser.add_argument("--sample-index", type=int, help="One sample index")
    args = parser.parse_args()
    config = validate_config(json.loads(Path(args.config).read_text()))
    steps = parse_steps(args.steps)
    samples = config["samples"]
    if args.sample_index is not None:
        if args.sample_index < 0 or args.sample_index >= len(samples):
            parser.error("sample index is outside the configured sample list")
        samples = [samples[args.sample_index]]
    if args.mode == "validate":
        print(json.dumps({"samples": len(samples), "storage": config["storage"], "steps": list(steps), "threads_per_task": config["threads"]}))
        return
    for sample in samples:
        for result in process_sample(sample, config, steps):
            print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
