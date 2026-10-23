"""Upload and run the minimal mount probe on project asd.

Run with the repo-local .venv. The API key is read from .api_key and never printed.
"""

import argparse
from pathlib import Path

import hopsworks


ROOT = Path(__file__).resolve().parents[1]
JOB_NAME = "biopipe_hopsfs_mount_probe"


def show_logs(job, key):
    executions = job.get_executions()
    if not executions:
        print("No executions found")
        return
    execution = max(executions, key=lambda item: item.id)
    print("Latest execution ID:", execution.id, "state:", execution.state)
    for path in execution.download_logs():
        if not path:
            continue
        lines = Path(path).read_text(errors="replace").replace(key, "[redacted]").splitlines()
        matches = [line for line in lines if "BIOPIPE_HOPSFS_PROBE=" in line]
        if matches:
            print("\n".join(matches))
        elif Path(path).name.startswith("stderr"):
            errors = [line for line in lines if "ERROR" in line or "Exception" in line]
            if errors:
                print("Log errors:", "\n".join(errors[-10:]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--logs", action="store_true", help="Read the latest probe logs without launching a job")
    options = parser.parse_args()
    project = hopsworks.login(
        host="10.113.6.130",
        project="asd",
        api_key_file=str(ROOT / ".api_key"),
        hostname_verification=False,
        engine="python",
    )
    jobs_api = project.get_job_api()
    if options.logs:
        job = jobs_api.get_job(JOB_NAME)
        if job is None:
            raise SystemExit("Probe job has not been created")
        show_logs(job, (ROOT / ".api_key").read_text().strip())
        return

    dataset_api = project.get_dataset_api()
    remote_path = dataset_api.upload(
        str(ROOT / "poc" / "hopsfs_executor_probe.py"), "Resources", overwrite=True
    )
    print("Uploaded probe to", remote_path)

    config = jobs_api.get_configuration("PYSPARK")
    config["appPath"] = remote_path
    config["spark.executor.instances"] = 1
    config["spark.executor.cores"] = 1
    config["spark.executor.memory"] = 2048
    config["spark.driver.memory"] = 1024
    config["spark.dynamicAllocation.enabled"] = False

    name = JOB_NAME
    job = jobs_api.get_job(name)
    if job is None:
        job = jobs_api.create_job(name, config)
    else:
        job.config = config
        job.save()
    print("Starting one-task job", name)
    try:
        execution = job.run(await_termination=True)
    except Exception:
        show_logs(job, (ROOT / ".api_key").read_text().strip())
        raise
    print("Execution ID:", execution.id, "success:", execution.success)
    out_path, err_path = execution.download_logs()
    for path in (out_path, err_path):
        text = Path(path).read_text(errors="replace")
        for line in text.splitlines():
            if "BIOPIPE_HOPSFS_PROBE=" in line:
                print(line[line.index("BIOPIPE_HOPSFS_PROBE="):])
    if not execution.success:
        raise SystemExit("Probe job failed; inspect its Hopsworks execution logs")


if __name__ == "__main__":
    main()
