"""Standalone Snakemake sample processing, adapted from single-task/biopipe.py.

Source commit: d3c2ecd0ea7fd2374ca056f5d445001c8a02f604
Keep scientific commands/configuration aligned with the other Biopipe versions.
Snakemake owns completion and reruns; this module never skips completed samples.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


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


def output_paths(config: dict, sample: dict) -> dict[str, Path]:
    root = Path(config["output_root"]) / sample["id"]
    outputs = {"report": root / "kraken_report.txt", "metrics": root / "metrics.json"}
    if config["publish"].get("fastq"):
        outputs["fastq_r1"] = root / "nonhuman_R1.fastq.gz"
        if sample.get("r2"):
            outputs["fastq_r2"] = root / "nonhuman_R2.fastq.gz"
    if config["publish"].get("kraken_output"):
        outputs["kraken_output"] = root / "kraken_output.txt"
    return outputs


def input_paths(config: dict, sample: dict) -> list[str]:
    """Track reference files, including prebuilt NGM indexes, rather than directories."""
    resources = config["resources"]
    human = Path(resources["human_reference_dir"])
    fasta = human / resources["human_fasta_name"]
    paths = [sample["r1"], resources["adapter_pair" if sample.get("r2") else "adapter_single"], str(fasta)]
    if sample.get("r2"):
        paths.append(sample["r2"])
    if resources.get("trimmomatic_jar"):
        paths.append(resources["trimmomatic_jar"])
    paths.extend(str(path) for path in sorted(human.glob(fasta.name + "*.ngm")) if path.is_file())
    paths.extend(str(Path(resources["kraken_db"]) / name) for name in ("hash.k2d", "opts.k2d", "taxo.k2d"))
    return paths


def science_settings(config: dict) -> dict:
    return {key: config[key] for key in ("resources_version", "resources", "tools", "trim", "mapping", "kraken", "publish")}


def atomic_copy(source: Path, target: Path) -> None:
    """Copy beside the destination and rename, including across scratch filesystems."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".partial", dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def process_sample(sample: dict, config: dict, threads: int | None = None, pipeline: Pipe = PIPELINE) -> dict:
    """Always execute; Snakemake alone decides which samples need work."""
    config = dict(config)
    if threads is not None:
        if type(threads) is not int or threads < 1:
            raise ValueError("allocated threads must be a positive integer")
        config["threads"] = threads
    outputs = output_paths(config, sample)
    root = outputs["report"].parent
    root.mkdir(parents=True, exist_ok=True)
    Path(config["scratch_root"]).mkdir(parents=True, exist_ok=True)
    log = root / "pipeline.log"
    log.write_text("")
    failure_path = root / "failure.json"
    failure_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="biopipe-" + sample["id"] + "-", dir=config["scratch_root"]) as directory:
        state = State(sample, config, Path(directory), LocalStorage(), log)
        published = []
        try:
            pipeline(state)
            # Check every result before publishing any of them.
            for name in outputs:
                if name != "metrics" and not state.paths[name].is_file():
                    raise FileNotFoundError(state.paths[name])
            for name, target in outputs.items():
                if name == "metrics":
                    continue
                started = time.monotonic()
                source = state.paths[name]
                atomic_copy(source, target)
                published.append(target)
                state.metrics["transfers"].append({
                    "direction": "out", "target": str(target), "bytes": source.stat().st_size,
                    "seconds": round(time.monotonic() - started, 3),
                })
            state.metrics.update({
                "elapsed_seconds": round(time.monotonic() - state.started, 3),
                "input_bytes": sum(item["bytes"] for item in state.metrics["transfers"] if item["direction"] == "in"),
                "output_bytes": sum(item["bytes"] for item in state.metrics["transfers"] if item["direction"] == "out"),
                "max_child_rss_bytes": max_child_rss_bytes(),
            })
            payload = {
                "sample": sample["id"], "status": "completed", "threads": config["threads"],
                "revision": sample["revision"], "resources_version": config["resources_version"],
                "outputs": {key: str(value) for key, value in outputs.items()}, "metrics": state.metrics,
            }
            metrics = state.scratch / "metrics.json"
            metrics.write_text(json.dumps(payload, sort_keys=True) + "\n")
            atomic_copy(metrics, outputs["metrics"])
            return payload
        except Exception as error:
            for target in published:
                target.unlink(missing_ok=True)
            # Logs live outside scratch and remain available after job failure.
            with log.open("a") as stream:
                stream.write("\nFAILED: " + type(error).__name__ + ": " + str(error) + "\n")
            failure_path.write_text(json.dumps({
                "sample": sample["id"], "error": type(error).__name__ + ": " + str(error),
                "metrics": state.metrics,
            }, sort_keys=True) + "\n")
            raise


def validate_config(config: dict) -> dict:
    if config.get("storage") != "local":
        raise ValueError("the standalone Snakemake version supports storage: local only")
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
    for name in ("adapter_pair", "adapter_single", "human_reference_dir", "human_fasta_name", "kraken_db"):
        if not config["resources"].get(name):
            raise ValueError("missing resource: " + name)
    fasta_name = config["resources"]["human_fasta_name"]
    if not isinstance(fasta_name, str) or Path(fasta_name).name != fasta_name or fasta_name in (".", ".."):
        raise ValueError("human_fasta_name must be a filename within human_reference_dir")
    for name, path in config["resources"].items():
        if path is not None and name != "human_fasta_name" and (not isinstance(path, str) or not Path(path).is_absolute()):
            raise ValueError("resource must use an absolute local path: " + name)
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
        for name in ("r1", "r2"):
            if sample.get(name) and (not isinstance(sample[name], str) or not Path(sample[name]).is_absolute()):
                raise ValueError("sample reads must use absolute local paths: " + sample_id)
    scheduling = config.get("scheduling", {})
    for name in ("mem_mb", "disk_mb"):
        if type(scheduling.get(name, 1)) is not int or scheduling.get(name, 1) < 1:
            raise ValueError("scheduling." + name + " must be a positive integer")
    if "cleanup_intermediates" in config and type(config["cleanup_intermediates"]) is not bool:
        raise ValueError("cleanup_intermediates must be a boolean")
    for name, value in config["publish"].items():
        if type(value) is not bool:
            raise ValueError("publish." + name + " must be a boolean")
    # Refuse output layouts that could overwrite reads or reference/cache contents.
    protected = [Path(sample[name]).resolve() for sample in config["samples"] for name in ("r1", "r2") if sample.get(name)]
    protected += [Path(value).resolve() for name, value in config["resources"].items() if value is not None and name != "human_fasta_name"]
    if cache:
        protected.append(Path(cache).resolve())
    source_directories = [Path(config["resources"][name]).resolve() for name in ("human_reference_dir", "kraken_db")]
    for local_root in (config["scratch_root"], cache):
        if local_root:
            local = Path(local_root).resolve()
            if any(local == source or source in local.parents for source in source_directories):
                raise ValueError("scratch and cache must be outside reference source directories")
    for sample in config["samples"]:
        root = (Path(config["output_root"]) / sample["id"]).resolve()
        for path in protected:
            if root == path or root in path.parents or path in root.parents:
                raise ValueError("sample output directory overlaps inputs, references or cache: " + str(root))
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate standalone Biopipe configuration without reading input data")
    parser.add_argument("--config", required=True)
    parser.add_argument("--mode", choices=("validate",), default="validate")
    args = parser.parse_args()
    config = validate_config(json.loads(Path(args.config).read_text()))
    print(json.dumps({"samples": len(config["samples"]), "storage": config["storage"], "threads_per_sample": config["threads"]}))


if __name__ == "__main__":
    main()
