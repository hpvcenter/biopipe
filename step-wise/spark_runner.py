"""Run each Biopipe step as its own Spark job, with one task per sample.

No Hopsworks API is used. Between steps the data lives on shared storage (local
POSIX path or HDFS through the ``hadoop fs`` CLI). Every step publishes a marker,
so a rerun, or a later ``--steps`` invocation, resumes at the first unfinished step.

Executors need the tools, the ``hadoop`` CLI for ``storage: hdfs``, and the
``scratch_root`` directory.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import biopipe

DONE = ("completed", "skipped", "duplicate")


def run_sample_step(index: int, config: dict, step: str) -> dict:
    """Executed inside a Spark task. Failures become results, not task errors."""
    import biopipe as executor_biopipe  # Shipped to executors with addPyFile.

    sample = config["samples"][index]
    try:
        return executor_biopipe.run_step(sample, config, step)
    except Exception as error:  # Other samples must still run; the driver reports.
        return {"sample": sample["id"], "step": step, "status": "failed", "error": type(error).__name__ + ": " + str(error)}


def partition_runner(config: dict, step: str):
    def run(indexes):
        for index in indexes:
            yield run_sample_step(index, config, step)
    return run


def run_spark(config: dict, steps: tuple[str, ...] = biopipe.STEPS, master: str | None = None, app_name: str = "biopipe-steps") -> dict:
    from pyspark.sql import SparkSession

    builder = SparkSession.builder.appName(app_name)
    if master:
        builder = builder.master(master)
    builder = builder.config("spark.task.cpus", str(config["threads"])).config("spark.speculation", "false")
    spark = builder.getOrCreate()
    results: list[dict] = []
    summary: list[dict] = []
    try:
        context = spark.sparkContext
        task_cpus = int(context.getConf().get("spark.task.cpus", "1"))
        if task_cpus != config["threads"]:
            raise ValueError("spark.task.cpus (%d) must equal config threads (%d)" % (task_cpus, config["threads"]))
        context.addPyFile(biopipe.__file__)
        context.addPyFile(__file__)
        index_of = {sample["id"]: index for index, sample in enumerate(config["samples"])}
        active = list(range(len(config["samples"])))
        for step in steps:
            if not active:
                break
            context.setJobDescription("biopipe step: " + step)
            started = time.monotonic()
            # A separate Spark job per step: every sample finishes a step before the next step starts.
            step_results = context.parallelize(active, len(active)).mapPartitions(partition_runner(config, step)).collect()
            seconds = round(time.monotonic() - started, 3)
            counts = {status: sum(1 for item in step_results if item["status"] == status) for status in (*DONE, "blocked", "failed")}
            summary.append({"step": step, "samples": len(active), "seconds": seconds, **{key: value for key, value in counts.items() if value}})
            results.extend(step_results)
            active = [index_of[item["sample"]] for item in step_results if item["status"] in DONE]
    finally:
        spark.stop()
    results.sort(key=lambda item: (biopipe.STEPS.index(item["step"]), item["sample"]))
    return {"results": results, "summary": summary}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="JSON configuration readable by the driver")
    parser.add_argument("--master", help="Spark master URL; omit under spark-submit")
    parser.add_argument("--mode", choices=("validate", "run"), default="validate")
    parser.add_argument("--steps", help="Comma separated steps in order (default: " + ",".join(biopipe.STEPS) + ")")
    args = parser.parse_args()
    config = biopipe.validate_config(json.loads(Path(args.config).read_text()))
    steps = biopipe.parse_steps(args.steps)
    if args.mode == "validate":
        print(json.dumps({"samples": len(config["samples"]), "storage": config["storage"], "steps": list(steps), "threads_per_task": config["threads"]}))
        return 0
    outcome = run_spark(config, steps, args.master)
    for result in outcome["results"]:
        print(json.dumps(result, sort_keys=True))
    print(json.dumps({"summary": outcome["summary"]}, sort_keys=True))
    return 1 if any(item["status"] in ("failed", "blocked") for item in outcome["results"]) else 0


if __name__ == "__main__":
    sys.exit(main())
