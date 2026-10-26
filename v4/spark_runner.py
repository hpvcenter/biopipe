"""Run the v4 sample executor as one Spark task per sample on any Spark cluster.

No Hopsworks API is used. Spark only schedules tasks; files move through the
storage backend in ``biopipe.py`` (local paths or the ``hadoop fs`` CLI), and the
bioinformatics tools run as ordinary subprocesses on executor-local scratch.

Executors need the tools, the ``hadoop`` CLI (for ``storage: hdfs``), and the
``scratch_root`` directory. Ship tools with a conda-pack archive, a container
image, or install them on the nodes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import biopipe


def run_sample(index: int, config: dict) -> dict:
    """Executed inside a Spark task. Failures become results, not task errors."""
    import biopipe as executor_biopipe  # Shipped to executors with addPyFile.

    sample = config["samples"][index]
    try:
        return executor_biopipe.process_sample(sample, config)
    except Exception as error:  # Other samples must still run; the driver reports.
        return {"sample": sample["id"], "status": "failed", "error": type(error).__name__ + ": " + str(error)}


def partition_runner(config: dict):
    def run(indexes):
        for index in indexes:
            yield run_sample(index, config)
    return run


def run_spark(config: dict, master: str | None = None, app_name: str = "biopipe") -> list[dict]:
    from pyspark.sql import SparkSession

    builder = SparkSession.builder.appName(app_name)
    if master:
        builder = builder.master(master)
    # One task runs one sample and its tools use `threads` cores.
    builder = builder.config("spark.task.cpus", str(config["threads"])).config("spark.speculation", "false")
    spark = builder.getOrCreate()
    try:
        context = spark.sparkContext
        task_cpus = int(context.getConf().get("spark.task.cpus", "1"))
        if task_cpus != config["threads"]:
            raise ValueError("spark.task.cpus (%d) must equal config threads (%d)" % (task_cpus, config["threads"]))
        context.addPyFile(biopipe.__file__)
        context.addPyFile(__file__)
        samples = len(config["samples"])
        results = context.parallelize(range(samples), samples).mapPartitions(partition_runner(config)).collect()
    finally:
        spark.stop()
    return sorted(results, key=lambda item: item["sample"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="JSON configuration readable by the driver")
    parser.add_argument("--master", help="Spark master URL; omit under spark-submit")
    parser.add_argument("--mode", choices=("validate", "run"), default="validate")
    args = parser.parse_args()
    config = biopipe.validate_config(json.loads(Path(args.config).read_text()))
    if args.mode == "validate":
        print(json.dumps({"samples": len(config["samples"]), "storage": config["storage"], "threads_per_task": config["threads"]}))
        return 0
    results = run_spark(config, args.master)
    for result in results:
        print(json.dumps(result, sort_keys=True))
    return 1 if any(result["status"] == "failed" for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
