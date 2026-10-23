# Biopipe v2: one task per sample

This is an opt-in replacement for the v1 Airflow chain. One PySpark job sends
each sample to one Spark task. Within that task, a small `Pipe` runs trimming,
human mapping, unmapped-read extraction, FASTQ conversion, and Kraken2 in
sequence. Inputs and references are staged to task-local scratch with the
existing `hops.hdfs` API. Intermediate SAM, BAM, and FASTQ files stay local;
the task uploads the Kraken report, a command log, and any selected final
files. A `done.json` marker is uploaded last and makes a matching rerun skip
the sample.

The Spark executor needs no `/hopsfs` POSIX mount. The SDK source explicitly
says Spark does not mount it; see `hopsworks-api/python/hsfs/storage_connector.py`.
The [Hopsworks PySpark guide](https://docs.hopsworks.ai/latest/user_guides/projects/jobs/pyspark_job/)
documents Spark filesystem reads, but this version uses `hops.hdfs` for file
transfers because that is the API used by v1's executor functions. A standalone
Kubernetes pod instead uses the Hopsworks dataset API over HTTPS. Both paths
still require a separate, minimal cluster check.

## Reusable container and Kubernetes Job

[`Dockerfile`](Dockerfile) builds on the maintained
[micromamba image](https://micromamba-docker.readthedocs.io/en/latest/quick_start.html).
The single [Conda environment](environment.yml) installs prebuilt Bioconda
packages for Trimmomatic 0.39, NextGenMap 0.5.5, samtools 1.22.1, Kraken2
2.1.6, and Cutadapt 5.2, plus OpenJDK 17 and Python 3.11. The Hopsworks SDK
5.2.1 is installed from PyPI for project dataset access. No bioinformatics
tool is compiled from source. One coherent environment keeps the libraries
needed by these tools together; copying isolated executables from separate
tool images would omit their runtime dependencies.

Build and validate the image locally:

```sh
docker build -t biopipe-v2:local v2
docker run --rm \
  -v "$PWD/v2/kube-example.json:/app/config.json:ro" \
  biopipe-v2:local --config /app/config.json --mode validate
```

The image does not bake an API key, reads, reference databases, or project
config into a layer. To use the image from the cluster, push it to a registry
its nodes can pull from and set that image in
[`kubernetes-job.example.yaml`](kubernetes-job.example.yaml).

The example Job uses [Kubernetes Indexed Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/):
each pod receives `JOB_COMPLETION_INDEX` and processes only the matching entry
in the `samples` array. This provides parallelism without a Spark runtime.
The pod uses `storage: hopsworks_api`, so no HopsFS mount or host Hadoop
configuration is required. It needs outbound HTTPS access to Hopsworks and a
project API key with dataset read/write access. Place the key in a Kubernetes
Secret named `biopipe-hopsworks` with key `api-key`; never put it in the image
or ConfigMap. The SDK reads `HOPSWORKS_HOST`, `HOPSWORKS_PROJECT`, and
`HOPSWORKS_API_KEY` from the pod environment. Host is a hostname or IP without
`https://`.

Create a ConfigMap from a project-specific copy of
[`kube-example.json`](kube-example.json). Set `completions` in the Job to the
number of samples and `parallelism` to the number of pods the cluster can
handle. `scratch_root` must point to the mounted `emptyDir`; adjust its size,
ephemeral storage request, memory, and CPU for the largest sample and both
reference directories. The output root must already exist in the project.
For example, after setting the namespace and editing the manifest:

```sh
kubectl -n YOUR_PROJECT_NAMESPACE create configmap biopipe-v2-config \
  --from-file=config.json=v2/your-config.json
kubectl -n YOUR_PROJECT_NAMESPACE create secret generic biopipe-hopsworks \
  --from-file=api-key=.api_key
kubectl apply -f v2/kubernetes-job.example.yaml
```

The dataset API recursively downloads reference directories **once per pod**,
so large databases may make network and scratch usage the limiting factor.
For large sample lists, a ConfigMap's size limit may require a different
config delivery mechanism.
The built image is about 1.0 GB unpacked; account for pull time when many
pods start on nodes without the image cached.

This standalone image runs `--mode local` for a Kubernetes Indexed Job, or a
single sample locally. It is **not** a drop-in Spark executor image for a
Hopsworks `PYSPARK` job: Spark's image and configuration are managed by the
Hopsworks job environment. Use the tool package versions in `environment.yml`
when building that environment.

## Configure and run

1. Copy `example.json` to a project-specific config. List each sample and its
   R1/R2 HopsFS paths explicitly. This avoids the positional R1/R2 pairing in
   v1. Single-end samples omit `r2`.
2. Point `human_reference_dir` to a directory containing `human_fasta_name`
   and any NGM index files. Point `kraken_db` to a complete Kraken2 database
   directory. Set tool commands to executables installed in the Hopsworks
   Spark environment. Ensure `output_root` already exists in HopsFS and has
   enough free space. Set `threads` no higher than Spark executor cores; allow
   RAM for the Kraken DB and scratch for the largest input plus intermediates.
3. Validate locally without contacting the cluster:

   ```sh
   .venv/bin/python v2/biopipe_v2.py --config v2/example.json --mode validate
   .venv/bin/python -m unittest discover -s v2/tests
   ```

4. For deployment, upload `biopipe_v2.py` and the project config to HopsFS.
   Create **one PYSPARK job** with `appPath` set to the script and attach the
   JSON as a Spark `files` dependency. Set job arguments to
   `--config <config basename> --mode spark`. The job environment must contain
   PySpark, `hops`, and the listed bioinformatics tools. Set executor cores
   and memory for one sample task, then choose the number of executors based
   on available capacity. The `partitions` value is only the number of Spark
   task partitions, not a resource reservation.

For the container path, set `resources.trimmomatic_jar` to `null` and
`tools.trimmomatic` to `trimmomatic`, as in `kube-example.json`. That uses the
installed Trimmomatic launcher instead of downloading a jar per sample.

For a filesystem-only integration run, change `storage` to `local`, use local
input/resource/output paths, and run `--mode local`. This invokes the real
tools and needs their binaries, references, and suitable test reads.

## Boundaries and limitations

- No cluster job or cluster data was run while implementing v2. The earlier
  `/hopsfs` probe found no executor mount, and its Spark Hadoop read failed
  with an incomplete error capture. This implementation still needs a tiny
  end-to-end HopsFS transfer check before a real dataset run.
- Large reference directories are copied once **per sample task**. A safe
  executor cache or preinstalled reference volume would be the next important
  scalability improvement after measuring this first version.
- For paired reads, unmapped mates without a matching unmapped partner are
  discarded by `samtools fastq -s /dev/null`. This follows the paired-output
  shape of v1 but should be reviewed for the study's biological requirements.
- `done.json` identifies a run by config and input **paths**, not input file
  contents. Inputs should be immutable. Use `--force` to rerun changed data at
  the same paths. A failed forced rerun removes the old marker but can leave
  partial output files; use a fresh output root for data revisions that must
  be isolated.
- `hops.hdfs.copy_to_hdfs` is used for final files; unlike the local storage
  backend, it does not provide an atomic whole-sample commit. The marker is
  published last, and incomplete attempts need inspection before rerun.
- Bracken and alternative assembly branches are outside this first v2 path.
  The pipe supports adding a later stage once this core path is validated.
