"""Portable, sample-affine bioinformatics runner for local files and HDFS."""

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

    def discard(self, *names: str) -> None:
        """Delete finished intermediate files from scratch; paths stay recorded."""
        if not self.config.get("cleanup_intermediates", True):
            return
        for name in names:
            path = self.paths.get(name)
            if path is not None and self.scratch in path.parents:
                path.unlink(missing_ok=True)

    def upload(self, source: Path, target: str) -> bool:
        started = time.monotonic()
        created = self.storage.put_once(source, target)
        if created:
            self.metrics["transfers"].append({
                "direction": "out", "target": target, "bytes": source.stat().st_size,
                "seconds": round(time.monotonic() - started, 3),
            })
        return created


class Pipe:
    def __init__(self, stages: tuple[Callable[[State], State], ...]):
        self.stages = stages

    def __or__(self, other: Callable[[State], State]) -> Pipe:
        return Pipe(self.stages + (other,))

    def __call__(self, state: State) -> State:
        for stage in self.stages:
            started = time.monotonic()
            succeeded = False
            try:
                state = stage(state)
                succeeded = True
            finally:
                state.metrics["stages"].append({
                    "name": stage.__name__,
                    "succeeded": succeeded,
                    "seconds": round(time.monotonic() - started, 3),
                    "observed_scratch_bytes": bytes_in(state.scratch),
                })
        return state


def stage_inputs(state: State) -> State:
    sample, resources = state.sample, state.config["resources"]
    state.paths["r1"] = state.fetch(sample["r1"], "inputs/r1")
    if sample.get("r2"):
        state.paths["r2"] = state.fetch(sample["r2"], "inputs/r2")
    if resources.get("trimmomatic_jar"):
        state.paths["jar"] = state.fetch_resource(resources["trimmomatic_jar"], "resources/jar")
    adapter = "adapter_pair" if sample.get("r2") else "adapter_single"
    state.paths["adapter"] = state.fetch_resource(resources[adapter], "resources/adapter")
    reference_dir = state.fetch_resource(resources["human_reference_dir"], "resources/human")
    state.paths["human_fasta"] = reference_dir / resources["human_fasta_name"]
    if not state.paths["human_fasta"].is_file():
        raise FileNotFoundError(state.paths["human_fasta"])
    state.paths["kraken_db"] = state.fetch_resource(resources["kraken_db"], "resources/kraken")
    if not state.paths["kraken_db"].is_dir():
        raise NotADirectoryError(state.paths["kraken_db"])
    return state


def trim(state: State) -> State:
    c, p = state.config, state.paths
    t = c["trim"]
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
    p["trim_r1"] = prefix / "R1.fastq.gz"
    if "r2" in p:
        p["trim_r2"] = prefix / "R2.fastq.gz"
        p["unpaired_r1"] = prefix / "unpaired_R1.fastq.gz"
        p["unpaired_r2"] = prefix / "unpaired_R2.fastq.gz"
        state.command(common + [p["r1"], p["r2"], p["trim_r1"], p["unpaired_r1"], p["trim_r2"], p["unpaired_r2"]] + filters)
        state.discard("r1", "r2", "unpaired_r1", "unpaired_r2")
        if t.get("cutadapt_r2_bases", 0):
            cut = prefix / "cut_R2.fastq.gz"
            state.command([c["tools"]["cutadapt"], "-j", c["threads"], "-u", t["cutadapt_r2_bases"], "-o", cut, p["trim_r2"]])
            os.replace(cut, p["trim_r2"])
    else:
        state.command(common + [p["r1"], p["trim_r1"]] + filters)
        state.discard("r1")
    return state


def map_human(state: State) -> State:
    c, p = state.config, state.paths
    m = c["mapping"]
    p["sam"] = state.scratch / "mapped.sam"
    command = [c["tools"]["ngm"], "-i", m["min_identity"], "-R", m["min_residues"]]
    command += ["-r", p["human_fasta"], "--silent-clip", "-o", p["sam"], "-t", c["threads"], "--no-progress"]
    command += ["-1", p["trim_r1"], "-2", p["trim_r2"]] if "trim_r2" in p else ["-q", p["trim_r1"]]
    if m.get("very_fast"):
        command.append("--very-fast")
    state.command(command)
    state.discard("trim_r1", "trim_r2")
    return state


def unmapped_fastq(state: State) -> State:
    c, p = state.config, state.paths
    p["bam"] = state.scratch / "unmapped.bam"
    p["sorted_bam"] = state.scratch / "name_sorted.bam"
    p["fastq_r1"] = state.scratch / "nonhuman_R1.fastq.gz"
    state.command([c["tools"]["samtools"], "view", "-b", "-f", "4", "-@", c["threads"], "-o", p["bam"], p["sam"]])
    state.discard("sam")
    state.command([c["tools"]["samtools"], "sort", "-n", "-@", c["threads"], "-o", p["sorted_bam"], p["bam"]])
    state.discard("bam")
    if "r2" in p:
        p["fastq_r2"] = state.scratch / "nonhuman_R2.fastq.gz"
        state.command([c["tools"]["samtools"], "fastq", "-@", c["threads"], "-1", p["fastq_r1"], "-2", p["fastq_r2"], "-0", os.devnull, "-s", os.devnull, p["sorted_bam"]])
    else:
        state.command([c["tools"]["samtools"], "fastq", "-@", c["threads"], "-0", p["fastq_r1"], p["sorted_bam"]])
    state.discard("sorted_bam")
    return state


def classify(state: State) -> State:
    c, p = state.config, state.paths
    p["report"] = state.scratch / "kraken_report.txt"
    p["kraken_output"] = state.scratch / "kraken_output.txt"
    command = [c["tools"]["kraken2"], "--db", p["kraken_db"], "--threads", c["threads"], "--report", p["report"]]
    command += ["--output", p["kraken_output"] if c["publish"].get("kraken_output") else os.devnull]
    if c["kraken"].get("memory_mapping", True):
        command.append("--memory-mapping")
    if "fastq_r2" in p:
        command += ["--paired", p["fastq_r1"], p["fastq_r2"]]
    else:
        command.append(p["fastq_r1"])
    state.command(command)
    if not p["report"].is_file():
        raise FileNotFoundError(p["report"])
    return state


PIPELINE = Pipe((stage_inputs,)) | trim | map_human | unmapped_fastq | classify


# Operational settings that do not change results, so changing them keeps markers valid.
NON_SCIENCE_KEYS = ("samples", "scratch_root", "output_root", "threads", "reference_cache", "cleanup_intermediates")


def fingerprint(config: dict, sample: dict) -> str:
    execution = {key: value for key, value in config.items() if key not in NON_SCIENCE_KEYS}
    return hashlib.sha256(json.dumps({"execution": execution, "sample": sample}, sort_keys=True).encode()).hexdigest()


def complete_marker(config: dict, sample: dict) -> str:
    return join_path(config["output_root"], sample["id"], "done", fingerprint(config, sample) + ".json")


def checked_marker(storage: object, marker: str, expected: str) -> dict | None:
    if not storage.exists(marker):
        return None
    done = json.loads(storage.read_text(marker))
    if done.get("fingerprint") != expected or not done.get("outputs"):
        raise RuntimeError("completion marker is invalid: " + marker)
    if not all(storage.exists(path) for path in done["outputs"].values()):
        raise RuntimeError("completion marker references missing outputs: " + marker)
    return done


def process_sample(sample: dict, config: dict, pipeline: Pipe = PIPELINE) -> dict:
    storage = storage_for(config)
    digest = fingerprint(config, sample)
    marker = complete_marker(config, sample)
    done = checked_marker(storage, marker, digest)
    if done:
        return {"sample": sample["id"], "status": "skipped", "outputs": done["outputs"], "metrics": done["metrics"]}
    attempt = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="biopipe-" + sample["id"] + "-", dir=config["scratch_root"]) as directory:
        scratch = Path(directory)
        state = State(sample, config, scratch, storage, scratch / "pipeline.log")
        root = join_path(config["output_root"], sample["id"], digest, attempt)
        try:
            pipeline(state)
        except Exception as error:
            failure = scratch / "failure.json"
            failure.write_text(json.dumps({
                "sample": sample["id"], "attempt": attempt,
                "error": type(error).__name__ + ": " + str(error),
                "elapsed_seconds": round(time.monotonic() - state.started, 3),
                "metrics": state.metrics,
            }, sort_keys=True) + "\n")
            try:
                if state.log.is_file():
                    state.upload(state.log, join_path(root, "pipeline.log"))
                state.upload(failure, join_path(root, "failure.json"))
            except Exception:
                # Preserve the tool/storage error; Kubernetes still captures its traceback.
                pass
            raise
        wanted = {"report": state.paths["report"], "log": state.log}
        if config["publish"].get("fastq"):
            wanted["fastq_r1"] = state.paths["fastq_r1"]
            if "fastq_r2" in state.paths:
                wanted["fastq_r2"] = state.paths["fastq_r2"]
        if config["publish"].get("kraken_output"):
            wanted["kraken_output"] = state.paths["kraken_output"]
        for name, source in wanted.items():
            if not source.is_file():
                raise FileNotFoundError(source)
            target = join_path(root, source.name)
            if not state.upload(source, target):
                raise RuntimeError("unexpected output collision: " + target)
            state.outputs[name] = target
        state.metrics["elapsed_seconds"] = round(time.monotonic() - state.started, 3)
        state.metrics["input_bytes"] = sum(item["bytes"] for item in state.metrics["transfers"] if item["direction"] == "in")
        state.metrics["output_bytes"] = sum(item["bytes"] for item in state.metrics["transfers"] if item["direction"] == "out")
        state.metrics["max_child_rss_bytes"] = max_child_rss_bytes()
        payload = {"sample": sample["id"], "fingerprint": digest, "attempt": attempt, "outputs": state.outputs, "metrics": state.metrics}
        local_marker = scratch / "done.json"
        local_marker.write_text(json.dumps(payload, sort_keys=True) + "\n")
        # The marker is intentionally excluded from transfer totals: it is
        # serialized from the metrics it carries and is published last.
        if not storage.put_once(local_marker, marker):
            winner = checked_marker(storage, marker, digest)
            if winner is None:
                raise RuntimeError("completion marker race could not be resolved")
            return {"sample": sample["id"], "status": "duplicate", "outputs": winner["outputs"], "metrics": winner["metrics"]}
        return {"sample": sample["id"], "status": "completed", "outputs": state.outputs, "metrics": state.metrics}


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
    parser.add_argument("--sample-index", type=int, help="One sample index, e.g. JOB_COMPLETION_INDEX")
    args = parser.parse_args()
    config = validate_config(json.loads(Path(args.config).read_text()))
    samples = config["samples"]
    if args.sample_index is not None:
        if args.sample_index < 0 or args.sample_index >= len(samples):
            parser.error("sample index is outside the configured sample list")
        samples = [samples[args.sample_index]]
    if args.mode == "validate":
        print(json.dumps({"samples": len(samples), "storage": config["storage"], "threads_per_sample": config["threads"]}))
        return
    for sample in samples:
        result = process_sample(sample, config)
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
