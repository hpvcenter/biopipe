# Biopipe v3: portable sample executor

V3 is a Hopsworks-free implementation of the existing Trimmomatic ->
NextGenMap -> samtools -> Kraken2 chain. One invocation processes one sample
through all stages in local scratch. Multiple samples can run sequentially on
one machine or in parallel using a Kubernetes Indexed Job. Inputs and
references come from a local POSIX filesystem or standard HDFS. HDFS access
uses the Apache Hadoop FileSystem shell; no HopsFS mount or Hopsworks API is
used.

This is an execution prototype. It has local tests with fake tool outputs and
a fake HDFS shell, but has **not** been run against a real HDFS or Kubernetes
cluster or with real sequencing data. It does not yet demonstrate a speedup
over a modular Nextflow pipeline with node-local scratch.

## Run locally

Edit [example.local.json](example.local.json) to point to real reads,
references, and an output directory. The listed reference version and each
sample revision must change when their contents change. Source paths are
assumed immutable within a revision.

```sh
python3 v3/biopipe.py --config v3/example.local.json --mode validate
python3 v3/biopipe.py --config v3/example.local.json --mode run
python3 -m unittest discover -s v3/tests -v
```

The host must have Trimmomatic, NextGenMap, samtools, Kraken2, and Cutadapt
when configured. The provided container installs prebuilt versions through
Bioconda:

```sh
docker build --target local -t biopipe-v3:local v3
docker run --rm -v /data:/data -v /local/scratch:/scratch \
  -v "$PWD/v3/your-local.json:/config.json:ro" \
  biopipe-v3:local --config /config.json --mode run
```

The local-only image omits the large Hadoop client layer. The default image
target includes the client from the [official Apache Hadoop image](https://hub.docker.com/r/apache/hadoop)
and can use [example.hdfs.json](example.hdfs.json):

```sh
docker build -t biopipe-v3:hdfs v3
docker run --rm -v /local/scratch:/scratch \
  -v /path/to/hadoop-conf:/hadoop-conf:ro \
  -e HADOOP_CONF_DIR=/hadoop-conf \
  -v "$PWD/v3/your-hdfs.json:/config.json:ro" \
  biopipe-v3:hdfs --config /config.json --mode run
```

The Hadoop configuration and any credentials are supplied by the site at
runtime. The container needs network access to its NameNode and DataNodes;
for secured HDFS it also needs the site's authentication setup. All HDFS
input, reference, and output paths must use `hdfs://` URIs.

## Kubernetes

[kubernetes-job.example.yaml](kubernetes-job.example.yaml) runs one sample
per indexed pod. Set its image, namespace, `completions` to the manifest's
sample count, and `parallelism` to a value that fits database RAM and scratch
on the nodes. The example starts with one pod at a time. Its 30 GiB
`emptyDir` and ephemeral-storage request/limit are examples; size them for
the largest sample, both reference directories and intermediate SAM/BAM
files. Database memory also counts toward the 12 GiB memory setting.

Create a ConfigMap for the job configuration and one for the site's Hadoop
client configuration, then apply the reviewed Job manifest:

```sh
kubectl -n YOUR_NAMESPACE create configmap biopipe-v3-config \
  --from-file=config.json=v3/your-hdfs.json
kubectl -n YOUR_NAMESPACE create configmap hadoop-client-conf \
  --from-file=core-site.xml=/path/to/core-site.xml \
  --from-file=hdfs-site.xml=/path/to/hdfs-site.xml
kubectl apply -f v3/kubernetes-job.example.yaml
```

Use a site-specific Secret or identity mechanism for credentials. Do not put
keys, tickets, or real sample manifests containing secrets in the image.
Large manifests can exceed the ConfigMap size limit; a shared volume is then
needed for configuration delivery.

## Outputs and retries

Each attempt writes files under
`<output_root>/<sample>/<fingerprint>/<attempt_id>/`. A small immutable
`<output_root>/<sample>/done/<fingerprint>.json` marker is written **after**
all selected outputs exist. The marker points to the winning attempt and
contains transfer, command and stage metrics:

- input and output bytes, transfer time and paths;
- elapsed time per tool and stage;
- peak child-process RSS as reported by the OS;
- scratch bytes observed at stage boundaries.

Metrics exclude the marker itself. Scratch use is sampled at stage boundaries,
so it can understate a short-lived peak. An Indexed Job may launch more than
one pod for the same index. A late failure cannot publish the marker, and a
duplicate completion uses the first valid marker. Losing attempts may leave
unreferenced files under their unique attempt paths; remove those only after
checking the marker. An existing marker whose outputs are missing causes an
error instead of silently reusing incomplete work. A failed processing stage
publishes `failure.json` and its command log under its attempt path when
storage is still available, without publishing a completion marker.

The fingerprint includes the sample revision, reference version, paths,
settings, and tool command names. It does **not** hash the contents of large
reads and databases. Publish new input/reference data under immutable paths
and advance the revision fields for a changed dataset.

## Current limits

- The full human and Kraken reference directories are copied per sample.
  Shared reference caching is a later improvement to make only after measuring
  that cost.
- Trimmomatic's unpaired outputs are not carried into classification. After
  host filtering, singleton unmapped mates are discarded, matching v2's output
  shape; review this biological choice before comparing scientific results.
- Bracken, S3, streaming between tools, and a Spark adapter are outside this
  first v3 implementation.
- The HDFS backend assumes normal HDFS rename behavior for publishing its
  small completion marker. Do not use an object-store `s3a://` output root
  through this backend: object-store rename and commit semantics differ.
