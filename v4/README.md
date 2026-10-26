# Biopipe v4: Spark adapter for any cluster

v4 is the v3 sample executor (`biopipe.py`: trimming, host removal, Kraken2,
local scratch, atomic completion markers) plus `spark_runner.py`, which runs
**one Spark task per sample**. It uses no Hopsworks API and works wherever
Spark can schedule Python tasks (standalone, YARN, Kubernetes, local).
v2's Spark path depends on `hops.hdfs`; v3 has no Spark. v4 is neither.

## Run

```sh
spark-submit --conf spark.task.cpus=4 v4/spark_runner.py --config cfg.json --mode run
# local smoke test
python v4/spark_runner.py --config cfg.json --mode run --master 'local[2]'
```

`spark.task.cpus` must equal the config's `threads` (checked at start), so
Spark never schedules more tool threads than cores. Speculation is disabled.
Under cluster deploy mode ship the config with `--files` and pass its basename.

Each sample prints one JSON line (`completed`, `skipped`, `duplicate` or
`failed`). The exit code is nonzero if any sample failed; other samples still
run. Rerunning is safe: finished samples are skipped through their marker.

## Docker (optional)

The [Dockerfile](Dockerfile) installs the pinned tools (Trimmomatic, NextGenMap,
samtools, Kraken2, cutadapt), Java 17 and PySpark 3.5.3, so users need nothing
else installed. The default entrypoint is `spark_runner.py`.

```sh
docker build -t biopipe-v4 v4                      # local files, Spark local mode
docker build --target hdfs -t biopipe-v4:hdfs v4   # adds the Hadoop client for hdfs://

docker run --rm --user root \
  -v $PWD/my-config:/config:ro -v /path/to/data:/data -v /fast/scratch:/scratch \
  biopipe-v4 --config /config/config.json --mode run --master 'local[4]'
```

Config paths are container paths (`/data/...`, `/scratch`). The data and
scratch mounts must be writable by the container user (`--user root` is the
simplest; otherwise `chown` them to the image's `mambauser`). For `hdfs`,
mount the site's `core-site.xml` and `hdfs-site.xml` into `/opt/hadoop/etc/hadoop`.
Building the `hdfs` target pulls `ghcr.io/apache/hadoop:3.5.0`.
The same image can serve as an executor image on clusters whose Spark workers
run in containers you control, but it is not a drop-in Spark-on-Kubernetes image
(that needs Spark's own entrypoint); this is untested.

Verified: both targets build; tool versions and `--mode validate` run in the
default image; a two-sample Spark `local[2]` run completed inside it with fake
tool scripts. The real tools were not run on real data. Spark prints a harmless
`ps: command not found` warning.

## Executor requirements

- The tools from `environment.yml` on `PATH` (or absolute paths in `tools`):
  ship a conda-pack archive (`--archives`), a container image, or install them.
- `storage: hdfs` needs the `hadoop` CLI and client config on every executor;
  `storage: local` needs a shared POSIX path.
- `scratch_root` must exist locally on executors with room for a sample, the
  human reference and the Kraken DB (see v3 README for sizing).

## Changes from v3

- **Node-local reference cache.** Set `reference_cache` to an absolute local
  directory shared by tasks on a node. The first task to need an adapter file,
  human reference or Kraken DB fetches it under a file lock and publishes it by
  rename; later tasks reuse it (`cache_hit` in the transfer metrics, 0 bytes).
  Entries are keyed by `resources_version` + source path and are never deleted
  automatically. Put a prebuilt NextGenMap index in the reference directory:
  tasks share it read-only and must not each build one beside the FASTA.
- **Scratch cleanup.** Inputs, trimmed reads, SAM, BAM and name-sorted BAM are
  deleted as soon as the next stage has consumed them
  (`"cleanup_intermediates": false` keeps them for debugging).
- **Fingerprint** ignores `threads`, `reference_cache` and
  `cleanup_intermediates`, so tuning them does not redo finished samples.
  `threads: true` is rejected.

- `HdfsStorage.exists` treats exit code 1 as "missing" even when Hadoop prints
  `WARN`/log4j noise on stderr; other codes or messages still raise.
- Tests: `tests/test_spark.py` runs a real `local[2]` Spark job against fake
  tool executables (skipped without pyspark).

## Not yet done

- Not run on a real YARN/Kubernetes/HDFS cluster or with the real tools.
- The reference cache is per node, not shared across nodes, and has no eviction.
- Unselected orphan attempt directories are not garbage-collected.
- Task retries after executor loss rerun the sample from scratch; there is no
  step-level resume.
