# Biopipe v5: step-wise Spark jobs, no Hopsworks

v5 runs the same tools as v4 (Trimmomatic, NextGenMap, samtools, Kraken2), but each
pipeline **step is its own Spark job**, with one task per sample. This is the
execution pattern of the first Hopsworks version (one job per step, files in
shared storage between steps), rebuilt without any Hopsworks API.

| | v4 | v5 |
|---|---|---|
| Unit of Spark work | one task = whole pipeline for one sample | one job per step, one task per sample |
| Intermediate files | local scratch only | shared storage (local path or HDFS) between steps |
| Resume after failure | whole sample | first unfinished step |
| Data transfer | inputs in once, results out once | every step reads its inputs and writes its outputs |
| Per-step timing | inside each sample's metrics | also per Spark job in the driver summary |

Use v4 when you want the least data movement. Use v5 when you want step-level
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
anything after the Kraken2 report (BIOM conversion, statistics) are outside v5.

## Run

```sh
# all steps, one Spark job each
spark-submit --conf spark.task.cpus=4 v5/spark_runner.py --config cfg.json --mode run
# a subset, e.g. in separate invocations (different resources per step)
spark-submit ... v5/spark_runner.py --config cfg.json --mode run --steps trim,map
# local smoke test
python v5/spark_runner.py --config cfg.json --mode run --master 'local[2]'
# without Spark, one sample at a time
python v5/biopipe.py --config cfg.json --mode run --sample-index 0
```

The config is the v4 JSON format (`example.local.json`, `example.hdfs.json`).
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

Same image layout as v4 (`Dockerfile`, targets `local` and `hdfs`; see v4 README
for the run command and mounts):

```sh
docker build -t biopipe-v5 v5
docker run --rm --user root -v my-config:/config:ro -v data:/data -v scratch:/scratch \
  biopipe-v5 --config /config/config.json --mode run --master 'local[4]'
```

## Verified

- 17 unit tests: step chaining, blocked steps, resume after a late failure (only
  the failed step reruns), duplicate-marker race, reference cache, a fake `hadoop fs`
  backend, and real `local[2]` Spark jobs with fake tool executables
  (`tests/test_spark.py`, skipped without pyspark).
- The `local` image builds, and a two-sample Spark run (all steps, then a `--steps`
  subset that was skipped) completed inside it with fake tool scripts.

## Limits

- Not run with the real tools on real data, and not on a real Spark, YARN, Kubernetes
  or HDFS cluster.
- Intermediates stay on shared storage and are not deleted. The SAM from `map` is
  the largest; prune `steps/<fingerprint>/map` and `unmapped` yourself once
  `classify` has finished.
- Each step copies its inputs to scratch again, and reference files are fetched per
  task (use `reference_cache`, per node, no eviction). Expect more transfer than v4.
- `publish.fastq` is ignored: the `fastq` step always keeps its FASTQ outputs.
- A task lost to an executor failure reruns its step from the start.
