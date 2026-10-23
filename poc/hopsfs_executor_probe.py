"""One-task HopsFS mount probe for a Hopsworks PySpark job."""

import json
import os
import subprocess
import sys

from pyspark import SparkContext


PROBE_PATH = "/hopsfs/Resources/hopsfs_executor_probe.py"


def inspect_mount(_):
    result = {
        "mount_exists": os.path.isdir("/hopsfs"),
        "mount_is_mountpoint": os.path.ismount("/hopsfs"),
        "resources_exists": os.path.isdir("/hopsfs/Resources"),
        "probe_file_exists": os.path.isfile(PROBE_PATH),
        "projects_exists": os.path.isdir("/hopsfs/Projects"),
        "legacy_projects_exists": os.path.isdir("/Projects/asd/Resources"),
    }
    if result["mount_exists"]:
        try:
            result["mount_entries"] = sorted(os.listdir("/hopsfs"))[:10]
        except OSError as exc:
            result["mount_list_error"] = type(exc).__name__
    if result["probe_file_exists"]:
        try:
            with open(PROBE_PATH, "rb") as stream:
                result["python_read_bytes"] = len(stream.read(64))
            child = subprocess.run(
                [sys.executable, "-c", "import sys; print(len(open(sys.argv[1], 'rb').read(64)))", PROBE_PATH],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            result["child_read_bytes"] = int(child.stdout.strip())
        except Exception as exc:
            result["read_error"] = type(exc).__name__ + ": " + str(exc)[:160]
    return result


if __name__ == "__main__":
    sc = SparkContext.getOrCreate()
    result = sc.parallelize([0], 1).map(inspect_mount).collect()[0]
    try:
        lengths = sc.binaryFiles(
            "/Projects/asd/Resources/hopsfs_executor_probe.py", minPartitions=1
        ).map(lambda item: len(item[1].read())).collect()
        result["spark_hopsfs_read_bytes"] = sum(lengths)
    except Exception as exc:
        result["spark_hopsfs_error"] = type(exc).__name__ + ": " + str(exc)[:160]
    print("BIOPIPE_HOPSFS_PROBE=" + json.dumps(result, sort_keys=True))
