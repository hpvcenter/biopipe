# HopsFS executor access probe

The scripts here test whether a Hopsworks PySpark executor can read the project's
HopsFS mount. They use project `asd` on the test cluster at `10.113.6.130`.
The probe uploads its own approximately 2 KB script to the `Resources` dataset,
starts one PySpark job with one executor and one task, and reads only that script.
No bioinformatics inputs or reference databases are used.

Run from the repository root with:

```sh
.venv/bin/python poc/run_hopsfs_probe.py
```

The launcher reads `.api_key` locally. Both `.api_key` and `.venv/` are ignored by
Git. The latest logs can be fetched without launching a job with:

```sh
.venv/bin/python poc/run_hopsfs_probe.py --logs
```

## Result on 2026-10-23

The test cluster reported backend version 5.3.0. The local SDK was 5.2.1.
Execution 1066 completed and reported:

```json
{"mount_exists": true, "mount_is_mountpoint": false, "mount_entries": [], "resources_exists": false, "probe_file_exists": false, "projects_exists": false, "legacy_projects_exists": false}
```

Therefore the expected `/hopsfs/Resources` path was not available inside the
Spark executor. The `/hopsfs` directory itself was empty and was not a mount point.

Execution 1067 repeated that result and tried to read the uploaded script using
Spark's Hadoop path `/Projects/asd/Resources/hopsfs_executor_probe.py`. That
read raised `Py4JJavaError`; the probe captured only the beginning of the error,
so its underlying cause remains unknown. This does not establish that all
Spark HopsFS access is broken.

For pipeline v2, treat mounted paths as an optional input mechanism. A fused
per-sample Spark task can still reduce intermediate HopsFS traffic by staging
the input once through a working HopsFS API, keeping intermediate files on
executor local scratch, and publishing final outputs once.

## SDK and documentation review

The `hopsworks-api` checkout at the repository root (commit
`688a85b6c7822d3715dcee56c35428e27348ecde`) explicitly says in
`python/hsfs/storage_connector.py` that Spark does not mount HopsFS. Its
provided-credentials guide says the SDK downloads a wallet from HopsFS when a
Spark runtime cannot read it through `/hopsfs`. The SDK job configuration
exposes Spark driver and executor resources, but no HopsFS mount switch.

The current [PySpark job guide](https://docs.hopsworks.ai/latest/user_guides/projects/jobs/pyspark_job/)
recommends reading `/Projects/<project>/...` through Spark's filesystem API.
The [Python job guide](https://docs.hopsworks.ai/latest/user_guides/projects/jobs/python_job/)
describes `/hopsfs/...` as a conditional local mount. These are different job
types and access paths. The failed `sc.binaryFiles` probe above still needs its
full Java error to diagnose the cluster's Spark HopsFS configuration.
