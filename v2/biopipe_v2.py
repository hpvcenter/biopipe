"""One Spark task per sample, with all bioinformatics stages in that task.

No HopsFS POSIX mount is assumed. The Hopsworks transfer backend uses the same
``hops.hdfs`` API as the v1 notebooks. All stage intermediates stay in scratch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


def remote_join(*parts: str) -> str:
    return "/".join(part.strip("/") if i else part.rstrip("/") for i, part in enumerate(parts))


class LocalStorage:
    def get(self, source: str, target: Path) -> Path:
        origin = Path(source)
        if not origin.exists():
            raise FileNotFoundError(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_dir():
            shutil.copytree(origin, target)
        else:
            shutil.copy2(origin, target)
        return target

    def put(self, source: Path, target: str) -> None:
        destination = Path(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".partial")
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)

    def read_text(self, path: str) -> str:
        return Path(path).read_text()

    def exists(self, path: str) -> bool:
        return Path(path).exists()

    def delete(self, path: str) -> None:
        Path(path).unlink(missing_ok=True)


class HopsStorage:
    """Transfer files in a Spark worker using the existing Hopsworks API.

    ``copy_to_local`` writes into the process working directory, so each task
    uses its own scratch directory. Spark Python workers are separate processes.
    """

    def __init__(self) -> None:
        from hops import hdfs  # Available in the Hopsworks PySpark environment.

        self.hdfs = hdfs

    def get(self, source: str, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.name != source.rstrip("/").rsplit("/", 1)[-1]:
            raise ValueError("HopsFS download target must retain the source basename")
        old_cwd = Path.cwd()
        try:
            os.chdir(target.parent)
            self.hdfs.copy_to_local(source, overwrite=True)
        finally:
            os.chdir(old_cwd)
        if not target.exists():
            raise FileNotFoundError(f"HopsFS transfer did not create {target}")
        return target

    def put(self, source: Path, target: str) -> None:
        if source.name != target.rsplit("/", 1)[-1]:
            raise ValueError("HopsFS upload source must match the target basename")
        parent = target.rsplit("/", 1)[0]
        if not self.hdfs.exists(parent):
            self.hdfs.mkdir(parent)
        self.hdfs.copy_to_hdfs(str(source), parent, overwrite=True)

    def read_text(self, path: str) -> str:
        data = self.hdfs.load(path)
        return data.decode("utf-8") if isinstance(data, bytes) else data

    def exists(self, path: str) -> bool:
        return self.hdfs.exists(path)

    def delete(self, path: str) -> None:
        if self.hdfs.exists(path):
            self.hdfs.delete(self.hdfs.get_plain_path(path))


class HopsworksApiStorage:
    """Use the Hopsworks dataset REST API from a standalone Kubernetes pod."""

    def __init__(self) -> None:
        import hopsworks

        self.api = hopsworks.login(engine="python").get_dataset_api()

    def get(self, source: str, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        metadata = self.api.get(source)
        if metadata["attributes"].get("dir", False):
            target.mkdir()
            offset = 0
            while True:
                children = self.api.list(source, offset=offset, limit=1000)
                for child in children:
                    self.get(child, target / child.rstrip("/").rsplit("/", 1)[-1])
                offset += len(children)
                if len(children) < 1000:
                    break
        else:
            self.api.download(source, local_path=str(target))
        return target

    def put(self, source: Path, target: str) -> None:
        if source.name != target.rsplit("/", 1)[-1]:
            raise ValueError("Hopsworks upload source must match target basename")
        parent = target.rsplit("/", 1)[0]
        if not self.api.exists(parent):
            self.api.mkdir(parent)
        self.api.upload(str(source), parent, overwrite=True)

    def read_text(self, path: str) -> str:
        with tempfile.TemporaryDirectory(prefix="biopipe-marker-") as directory:
            return Path(self.api.download(path, local_path=directory)).read_text()

    def exists(self, path: str) -> bool:
        return self.api.exists(path)

    def delete(self, path: str) -> None:
        if self.api.exists(path):
            self.api.remove(path)


def storage_for(config: dict):
    if config["storage"] == "local":
        return LocalStorage()
    if config["storage"] == "hops":
        return HopsStorage()
    return HopsworksApiStorage()


def run_command(command: list[str], log: Path) -> None:
    with log.open("a") as stream:
        stream.write("$ " + " ".join(command) + "\n")
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
    published: dict[str, str] = field(default_factory=dict)

    def command(self, args: list[str]) -> None:
        run_command([str(arg) for arg in args], self.log)

    def fetch(self, source: str, group: str) -> Path:
        target = self.scratch / group / source.rstrip("/").rsplit("/", 1)[-1]
        return self.storage.get(source, target)


class Pipe:
    def __init__(self, stages: tuple[Callable[[State], State], ...]):
        self.stages = stages

    def __or__(self, other: Callable[[State], State]) -> Pipe:
        return Pipe(self.stages + (other,))

    def __call__(self, state: State) -> State:
        for stage in self.stages:
            state = stage(state)
        return state


def stage_inputs(state: State) -> State:
    sample, resources = state.sample, state.config["resources"]
    state.paths["r1"] = state.fetch(sample["r1"], "inputs/r1")
    if sample.get("r2"):
        state.paths["r2"] = state.fetch(sample["r2"], "inputs/r2")
    if resources.get("trimmomatic_jar"):
        state.paths["jar"] = state.fetch(resources["trimmomatic_jar"], "resources/jar")
    adapter = "adapter_pair" if sample.get("r2") else "adapter_single"
    state.paths["adapter"] = state.fetch(resources[adapter], "resources/adapter")
    reference_dir = state.fetch(resources["human_reference_dir"], "resources/human")
    state.paths["human_fasta"] = reference_dir / resources["human_fasta_name"]
    if not state.paths["human_fasta"].is_file():
        raise FileNotFoundError(state.paths["human_fasta"])
    state.paths["kraken_db"] = state.fetch(resources["kraken_db"], "resources/kraken")
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
        if t.get("cutadapt_r2_bases", 0):
            cut = prefix / "cut_R2.fastq.gz"
            state.command([c["tools"]["cutadapt"], "-j", c["threads"], "-u", t["cutadapt_r2_bases"], "-o", cut, p["trim_r2"]])
            os.replace(cut, p["trim_r2"])
    else:
        state.command(common + [p["r1"], p["trim_r1"]] + filters)
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
    return state


def unmapped_fastq(state: State) -> State:
    c, p = state.config, state.paths
    p["bam"] = state.scratch / "unmapped.bam"
    p["sorted_bam"] = state.scratch / "name_sorted.bam"
    p["fastq_r1"] = state.scratch / "nonhuman_R1.fastq.gz"
    state.command([c["tools"]["samtools"], "view", "-b", "-f", "4", "-@", c["threads"], "-o", p["bam"], p["sam"]])
    state.command([c["tools"]["samtools"], "sort", "-n", "-@", c["threads"], "-o", p["sorted_bam"], p["bam"]])
    if "r2" in p:
        p["fastq_r2"] = state.scratch / "nonhuman_R2.fastq.gz"
        state.command([c["tools"]["samtools"], "fastq", "-@", c["threads"], "-1", p["fastq_r1"], "-2", p["fastq_r2"], "-0", os.devnull, "-s", os.devnull, p["sorted_bam"]])
    else:
        state.command([c["tools"]["samtools"], "fastq", "-@", c["threads"], "-0", p["fastq_r1"], p["sorted_bam"]])
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


def fingerprint(config: dict, sample: dict) -> str:
    payload = {"config": {k: v for k, v in config.items() if k not in ("scratch_root", "partitions", "samples")}, "sample": sample}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def publish(state: State) -> State:
    c, p = state.config, state.paths
    root = remote_join(c["output_root"], state.sample["id"])
    wanted = {"report": p["report"], "log": state.log}
    if c["publish"].get("fastq"):
        wanted["fastq_r1"] = p["fastq_r1"]
        if "fastq_r2" in p:
            wanted["fastq_r2"] = p["fastq_r2"]
    if c["publish"].get("kraken_output"):
        wanted["kraken_output"] = p["kraken_output"]
    for name, source in wanted.items():
        if not source.is_file():
            raise FileNotFoundError(source)
        destination = remote_join(root, source.name)
        state.storage.put(source, destination)
        state.published[name] = destination
    marker = state.scratch / "done.json"
    marker.write_text(json.dumps({"sample": state.sample["id"], "fingerprint": fingerprint(c, state.sample), "outputs": state.published}, sort_keys=True) + "\n")
    state.storage.put(marker, remote_join(root, marker.name))
    return state


PIPELINE = Pipe((stage_inputs,)) | trim | map_human | unmapped_fastq | classify | publish


def process_sample(sample: dict, config: dict, force: bool = False, pipeline: Pipe = PIPELINE) -> dict:
    storage = storage_for(config)
    marker = remote_join(config["output_root"], sample["id"], "done.json")
    if force:
        storage.delete(marker)
    if not force and storage.exists(marker):
        try:
            done = json.loads(storage.read_text(marker))
            if done.get("fingerprint") == fingerprint(config, sample) and all(
                storage.exists(path) for path in done["outputs"].values()
            ):
                return {"sample": sample["id"], "status": "skipped", "outputs": done["outputs"]}
        except (ValueError, KeyError, OSError):
            pass
    with tempfile.TemporaryDirectory(prefix="biopipe-" + sample["id"] + "-", dir=config.get("scratch_root")) as temp:
        state = State(sample, config, Path(temp), storage, Path(temp) / "pipeline.log")
        result = pipeline(state)
        return {"sample": sample["id"], "status": "completed", "outputs": result.published}


def validate_config(config: dict) -> dict:
    if config.get("storage") not in ("local", "hops", "hopsworks_api"):
        raise ValueError("storage must be 'local', 'hops', or 'hopsworks_api'")
    for field_name in ("output_root", "resources", "tools", "trim", "mapping", "kraken", "publish", "samples", "threads"):
        if field_name not in config:
            raise ValueError("missing configuration field: " + field_name)
    if not isinstance(config["threads"], int) or config["threads"] < 1:
        raise ValueError("threads must be a positive integer")
    if not config["samples"]:
        raise ValueError("samples must contain at least one sample")
    if not isinstance(config.get("output_root"), str) or not config["output_root"]:
        raise ValueError("output_root must be a path")
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
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Local JSON configuration file, attached to the Spark driver")
    parser.add_argument("--mode", choices=("validate", "local", "spark"), default="validate")
    parser.add_argument("--sample-index", type=int, help="Run only this sample index, for a Kubernetes Indexed Job")
    parser.add_argument("--force", action="store_true", help="Reprocess samples with a matching done marker")
    args = parser.parse_args()
    config = validate_config(json.loads(Path(args.config).read_text()))
    samples = config["samples"]
    if args.sample_index is not None:
        if args.sample_index < 0 or args.sample_index >= len(samples):
            parser.error("sample index is outside the configured sample list")
        samples = [samples[args.sample_index]]
    if args.mode == "validate":
        print(json.dumps({"samples": len(samples), "storage": config["storage"], "threads_per_task": config["threads"]}))
        return
    if args.mode == "local":
        results = [process_sample(sample, config, args.force) for sample in samples]
    else:
        from pyspark import SparkContext

        sc = SparkContext.getOrCreate()
        partitions = min(len(samples), config.get("partitions", len(samples)))
        if not isinstance(partitions, int) or partitions < 1:
            raise ValueError("partitions must be a positive integer")
        results = sc.parallelize(samples, partitions).map(lambda sample: process_sample(sample, config, args.force)).collect()
    for result in results:
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
