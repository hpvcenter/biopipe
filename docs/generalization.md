# V3 proposal: storage-aware sample execution

Implementation status: [v3](../v3/README.md) now has the local/HDFS runner,
transfer metrics, container image, and Kubernetes Indexed Job example. These
paths have local tests only; real HDFS/Kubernetes validation and benchmarking
remain open.

## Recommendation

Build v3 as a **reusable, containerized sample executor** for reference-heavy
bioinformatics pipelines. Its first supported backends should be local POSIX
files and standard HDFS, with Kubernetes Indexed Jobs for parallel execution.
Keep the scientific workflow narrow (trimming, host-read removal, Kraken2,
optional Bracken). Make a Nextflow process wrapper available for users who want
to embed this execution mode in an existing workflow. Defer a Spark adapter
until benchmarks show a reason for it.

The intended open-source contribution is the **data movement policy**, not
another catalogue of taxonomic tools: stage inputs once, run related commands
on local scratch, reuse versioned references where feasible, and publish only
chosen results. Expose transfer counts, bytes and timings so users can see
when it helps. This value is a hypothesis until it beats a fair baseline.

## Why this is a better fit than a new taxonomic workflow

nf-core/taxprofiler already covers preprocessing, host removal, Kraken2,
Bracken, several other classifiers, and standardized reports. Nextflow itself
supports node-local `scratch` and per-process caching. A plain DSL2 rewrite of
this repository would duplicate existing work, and one large process is not
automatically faster: it loses step-level resume and may repeat expensive
work after a late failure. The useful comparison is against **identical tools
and settings** in modular Nextflow processes with `scratch` enabled.

| Execution choice | V3 role | Reason |
| --- | --- | --- |
| Kubernetes Indexed Jobs | Primary distributed runner | Matches independent samples and existing container; per-pod CPU, memory and scratch limits; no Spark runtime |
| Spark `mapPartitions` | Optional later adapter | May help sites with HDFS and Spark already available, but CLI tools gain little from Spark's data APIs |
| Nextflow | Interoperability wrapper and benchmark baseline | Gives users existing workflow orchestration; its scratch, caching and executor features must be treated as the baseline, not a novelty claim |

## Minimal architecture

1. **Portable sample manifest.** A validated samplesheet names immutable R1/R2
   inputs, reference versions, output URI and resource requirements. It is
   independent of Kubernetes, Spark and Hopsworks.
2. **Transfer backends.** Local POSIX is first. HDFS uses Apache Hadoop's
   `hadoop fs -get/-put` commands and cluster-provided configuration and
   credentials; no Hopsworks SDK or mount assumption. Keep remote paths separate
   from local scratch paths. S3 can be a later backend, with its own commit
   semantics.
3. **Sample-affine execution.** One pod handles one sample's chosen stages in
   bounded `emptyDir` scratch. It stages large inputs once, keeps SAM/BAM/FASTQ
   intermediates local, records tool versions and transfers, and publishes
   output plus a completion manifest. Output publication must tolerate pod
   retries and duplicate indexed pods.
4. **Optional reference cache.** Start with explicit per-pod staging. Add an
   immutable cache keyed by reference version/digest only if reference transfer
   is a measured bottleneck. A node-shared cache needs locking, validation,
   quotas and an eviction policy; it is not part of the first release.
5. **Integration.** A small Nextflow process can call the container using
   task-local inputs and declared outputs. It should not hide a second task
   scheduler inside Nextflow. Keep the direct Kubernetes Job path for HDFS
   environments where a Nextflow shared work volume is unavailable.

## Evidence gate before claiming an improvement

Run the same tool versions and parameters through (a) modular Nextflow with
node-local scratch and (b) v3's fused sample executor. Compare on a local test
set first, then on an authorized small HDFS/Kubernetes installation with
small and large references. Report output equivalence, wall time, transferred
bytes, reference downloads, peak RAM, scratch use, recovery after a late
failure, and cost per sample. Test both a few samples and enough samples to
expose database re-download and pod startup overhead. If v3 is not better on
a relevant workload, publish the transport/measurement component or contribute
the result to an existing workflow instead of claiming a faster pipeline.

The v2 code is a starting point, not a finished generic engine. It currently
copies reference directories per sample, has Hopsworks-specific backends,
and fingerprints paths/config rather than input contents. Its paired-read
singleton handling also needs biological review before result equivalence
can be asserted. No cluster test is authorized for this design step.

## Sources

- [nf-core/taxprofiler](https://nf-co.re/taxprofiler/latest/)
- [Nextflow scratch](https://docs.seqera.io/nextflow/reference/process/directives/scratch)
  and [cache/resume](https://docs.seqera.io/nextflow/cache-and-resume)
- [Kubernetes Indexed Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/)
  and [emptyDir volumes](https://kubernetes.io/docs/concepts/storage/volumes/)
- [Hadoop FileSystem shell](https://hadoop.apache.org/docs/current/hadoop-project-dist/hadoop-common/FileSystemShell.html)
