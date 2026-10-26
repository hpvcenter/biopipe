# Biopipe step-wise variant: one Spark job per step, no Hopsworks

This variant runs the same tools as the [single-task variant](../single-task/README.md) (Trimmomatic, NextGenMap, samtools, Kraken2), but each
pipeline **step is its own Spark job**, with one task per sample. This is the
execution pattern of the first Hopsworks version (one job per step, files in
shared storage between steps), rebuilt without any Hopsworks API.

| | Single-task variant | Step-wise variant |
|---|---|---|
| Unit of Spark work | one task = whole pipeline for one sample | one job per step, one task per sample |
| Intermediate files | local scratch only | shared storage (local path or HDFS) between steps |
| Resume after failure | whole sample | first unfinished step |
| Data transfer | inputs in once, results out once | every step reads its inputs and writes its outputs |
| Per-step timing | inside each sample's metrics | also per Spark job in the driver summary |

Use the single-task variant when you want the least data movement. Use the step-wise variant when you want step-level
resume, per-step timing on a cluster, or a like-for-like comparison with the
original per-step design (the two can be benchmarked against each other).

## Steps

| Step | Tools | Reads | Writes |
|---|---|---|---|
| `trim` | Trimmomatic (+ optional cutadapt) | sample FASTQ, adapters | `R1/R2.fastq.gz` |
| `map` | NextGenMap vs the human reference | trimmed reads | `mapped.sam` |
| `unmapped` | `samtools view -b -f 4` | SAM | `unmapped.bam` |
| `fastq` | `samtools sort -n`, `samtools fastq` | BAM | `nonhuman_R1/R2.fastq.gz` |
| `classify` | Kraken2 | non-human FASTQ, Kraken DB | `kraken_report.txt` (+ `kraken_output.txt` if `publish.kraken_output`) |

Every step also writes its command log (`step.log`). Input is FASTQ; BAM input and
anything after the Kraken2 report (BIOM conversion, statistics) are outside this variant.

## Run

```sh
# all steps, one Spark job each
spark-submit --conf spark.task.cpus=4 step-wise/spark_runner.py --config cfg.json --mode run
# a subset, e.g. in separate invocations (different resources per step)
spark-submit ... step-wise/spark_runner.py --config cfg.json --mode run --steps trim,map
# local smoke test
python step-wise/spark_runner.py --config cfg.json --mode run --master 'local[2]'
# without Spark, one sample at a time
python step-wise/biopipe.py --config cfg.json --mode run --sample-index 0
```

The config uses the same JSON format as the single-task variant (`example.local.json`, `example.hdfs.json`).
`spark.task.cpus` must equal `threads` (checked). The driver prints one JSON line per
sample and step, then a summary with the wall time of each step's Spark job:

```json
{"summary": [{"step": "trim", "samples": 20, "seconds": 812.4, "completed": 20}, ...]}
```

A sample that fails a step is dropped from later steps in that run; the others
continue. The exit code is nonzero if any sample failed or was blocked.

## Layout and reruns

```
<output_root>/<sample>/steps/<fingerprint>/<step>.done.json      # marker, written last
<output_root>/<sample>/steps/<fingerprint>/<step>/<attempt>/...  # files of one attempt
```

A step publishes its marker only after all its outputs exist. A step whose
marker exists is skipped; a step whose upstream marker is missing is `blocked`.
A marker pointing at a missing file is an error and is not silently reused.
The fingerprint ignores `threads`, `reference_cache`, `scratch_root` and
`output_root`.

## Docker

Same image layout as the single-task variant (`Dockerfile`, targets `local` and `hdfs`; see its README
for the run command and mounts):

```sh
docker build -t biopipe-stepwise step-wise
docker run --rm --user root -v my-config:/config:ro -v data:/data -v scratch:/scratch \
  biopipe-stepwise --config /config/config.json --mode run --master 'local[4]'
```

## Spark on Kubernetes (no Hopsworks)

The `spark-k8s` Docker target is Apache Spark's own image plus the tools, so Spark can
start its driver and executor pods from it:

```sh
docker build --target spark-k8s -t REGISTRY/biopipe-stepwise:TAG step-wise && docker push REGISTRY/biopipe-stepwise:TAG
spark-submit --master k8s://https://API_SERVER:6443 --deploy-mode cluster \
  --conf spark.kubernetes.namespace=NS \
  --conf spark.kubernetes.authenticate.driver.serviceAccountName=SA \
  --conf spark.kubernetes.container.image=REGISTRY/biopipe-stepwise:TAG \
  --conf spark.kubernetes.container.image.pullSecrets=PULL_SECRET \
  --conf spark.executor.instances=2 --conf spark.executor.cores=1 --conf spark.executor.memory=2048m \
  --conf spark.task.cpus=1 --conf spark.dynamicAllocation.enabled=false \
  --conf spark.kubernetes.driver.volumes.persistentVolumeClaim.data.options.claimName=PVC \
  --conf spark.kubernetes.driver.volumes.persistentVolumeClaim.data.mount.path=/data \
  --conf spark.kubernetes.executor.volumes.persistentVolumeClaim.data.options.claimName=PVC \
  --conf spark.kubernetes.executor.volumes.persistentVolumeClaim.data.mount.path=/data \
  local:///app/spark_runner.py --config /data/config/config.json --mode run
```

The service account needs create/get/list/watch/delete on pods, services, configmaps and
persistentvolumeclaims in the namespace. Driver and executors must see the same data, so use a
shared volume (a ReadWriteOnce volume works only when all pods run on the same node) or HDFS.

## Verified

- 17 unit tests: step chaining, blocked steps, resume after a late failure (only
  the failed step reruns), duplicate-marker race, reference cache, a fake `hadoop fs`
  backend, and real `local[2]` Spark jobs with fake tool executables
  (`tests/test_spark.py`, skipped without pyspark).
- The `local` and `spark-k8s` images build and run the full pipeline with the real tools.
- End to end on Spark on Kubernetes (Spark 3.5.3, 3 nodes, 2 executors with 1 core and
  2048 MB each, one ReadWriteOnce volume): all five steps ran as separate Spark jobs spread
  over both executors, the Kraken2 report matched a local run, and a second run skipped every
  task. This is a functional check of the code path, not a measure of classification quality
  or speed.

## Limits

- Cluster runs so far used Spark on Kubernetes with a node-local volume. YARN, standalone
  Spark and HDFS storage are not covered, and neither is performance at scale.
- Intermediates stay on shared storage and are not deleted. The SAM from `map` is
  the largest; prune `steps/<fingerprint>/map` and `unmapped` yourself once
  `classify` has finished.
- Each step copies its inputs to scratch again, and reference files are fetched per
  task (use `reference_cache`, per node, no eviction). Expect more transfer than the single-task variant.
- `publish.fastq` is ignored: the `fastq` step always keeps its FASTQ outputs.
- A task lost to an executor failure reruns its step from the start.
