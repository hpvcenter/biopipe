# Standalone Biopipe with Snakemake

Run one complete metatranscriptomics processing job per sample. Each job trims
FASTQ reads with Trimmomatic and optional Cutadapt, removes host reads with
NextGenMap and samtools, and classifies the remaining reads with Kraken2.
Single-end and paired-end samples can appear in the same configuration.

Snakemake runs samples in parallel and manages completion and reruns. A failed
sample restarts from trimming. Intermediate files stay in job-local scratch;
final results go to a local or shared POSIX filesystem. Processing ends at the
Kraken2 report; Bracken is not included.

This directory is self-contained. It does not import another Biopipe backend or
require Spark, Kubernetes, or Hadoop. You can copy the directory elsewhere and run
it independently. The processing commands were adapted from the single-task
executor at commit `d3c2ecd0ea7fd2374ca056f5d445001c8a02f604`.

## Install

On Linux, create the environment containing Snakemake and all processing tools:

```sh
conda env create -n biopipe-snakemake -f environment.yml
conda activate biopipe-snakemake
```

The environment pins Snakemake 9.13.7, Python 3.11, Java 17, Trimmomatic 0.39,
NextGenMap 0.5.5, samtools 1.22.1, Kraken2 2.1.6, and Cutadapt 5.2. Snakemake is
provided by the `snakemake-minimal` Conda package; the default local executor needs
no executor plugin. Commands below assume this directory is the working directory.

## Configure and validate

```sh
cp example.local.json config.json
# Edit config.json with your paths and settings.
python scripts/biopipe.py --config config.json
```

Validation checks configuration without reading FASTQs or references or creating
output directories. It is also available explicitly as `--mode validate`.

Use absolute local paths for reads, reference files/directories, `output_root`,
and `scratch_root`. Set `storage` to `local`; this includes mounted shared POSIX
storage. Use a separate output root from other Biopipe versions and keep sample
output directories separate from reads, references, and the reference cache.

The scientific fields match the existing Biopipe JSON configuration:

- `samples`: each sample has a unique safe `id`, a nonempty `revision`, `r1`,
  and optionally `r2`. Omit `r2` or set it to `null` for single-end reads.
- `resources`: adapter files, `human_reference_dir`, `human_fasta_name`, and
  `kraken_db`; optionally set `trimmomatic_jar` to use Java directly.
- `tools`, `trim`, `mapping`, and `kraken`: tool paths/names and scientific
  options. Optional `trim.cutadapt_r2_bases` trims only paired-end R2 reads.
- `publish.fastq` and `publish.kraken_output`: enable non-human FASTQ files and
  per-read Kraken classifications, respectively. Both default to false in the
  example; the Kraken report is always published.
- `resources_version`: identifies the reference release. Change it whenever a
  reference bundle changes, especially when reference caching is enabled.

Create a Kraken2 database containing `hash.k2d`, `opts.k2d`, and `taxo.k2d` before
running. The workflow tracks these files, the selected adapter, the human FASTA,
any prebuilt NGM indexes matching `<human_fasta_name>*.ngm`, and an optional
Trimmomatic JAR as inputs. Other reference-bundle changes require an updated
`resources_version`; directory modification times alone are not sufficient.

`threads` sets the maximum cores per sample. The optional `scheduling` block sets
`mem_mb` and `disk_mb` per sample (defaults: 16000 and 100000). Size these estimates
for your reads and reference databases. Snakemake passes the actual allocated
thread count to the processing tools if it reduces the configured maximum.

## Run

```sh
snakemake --configfile config.json --cores 8 --dry-run
snakemake --configfile config.json --cores 8 \
  --keep-going --rerun-incomplete
```

`--keep-going` lets independent samples finish after a sample fails. The overall
command still exits nonzero when a job fails. `--rerun-incomplete` restarts jobs
whose outputs Snakemake recorded as incomplete. By default samples compete for
the specified cores; to also constrain total local memory and disk estimates:

```sh
snakemake --configfile config.json --cores 8 \
  --resources mem_mb=64000 disk_mb=400000 \
  --keep-going --rerun-incomplete
```

These are scheduler limits, not reservations of physical memory or disk space.
Each sample is one job, using its peak resource requirements across all stages.
Paired and single-end rule variants invoke the same processing code and declare
the appropriate outputs for each layout.

You can invoke the workflow from another directory with absolute `--snakefile`
and `--configfile` paths. Snakemake stores its `.snakemake/` metadata in the
invocation's working directory; keep using that directory for subsequent runs.

## Results, scratch, and reruns

```text
<output_root>/<sample>/
├── kraken_report.txt
├── metrics.json
├── pipeline.log
├── nonhuman_R1.fastq.gz    # publish.fastq
├── nonhuman_R2.fastq.gz    # publish.fastq, paired samples only
└── kraken_output.txt      # publish.kraken_output
```

The report, metrics, and enabled optional products are declared rule outputs.
Result files are copied beside their destination and published by rename.
`metrics.json` records stage/command times, transfer sizes, observed scratch usage,
child-process peak RSS, and the allocated threads. Timings are measurements of the
individual job, not a performance benchmark.

Rerunning the same command reuses completed samples. A missing declared product,
changed input file, changed sample revision, or changed scientific setting reruns
the affected samples. Changing operational settings such as thread count,
scheduling limits, or scratch location alone does not rerun completed samples.
Processing implementation and environment files are also tracked as inputs.

There are no Biopipe completion markers or internal skip checks. To force all
samples to run again:

```sh
snakemake --configfile config.json --cores 8 --forceall --rerun-incomplete
```

Use a sample's report path as a target to run or force just that sample. Turning
an optional publication setting off stops tracking that product; files from an
earlier run can remain on disk and are not current requested outputs.

Each sample uses a unique directory beneath `scratch_root`. Consumed intermediates
are deleted eagerly by default. `cleanup_intermediates: false` defers this deletion
until the job ends; the job's scratch directory is always removed, including on
failure. Original reads and reference files are not deleted. Logs remain in the
sample's output directory after a failure, together with `failure.json` describing
the error. A successful retry clears the failure record.

Optionally set `reference_cache` to an absolute node-local directory. A file lock
allows concurrent jobs to populate each reference once, and subsequent jobs reuse
it. Cached resources are keyed by source path and `resources_version`, with no
automatic eviction. Keep reference releases immutable and bump
`resources_version` when changing their contents: a rerun alone does not refresh
an existing cache entry. Prebuild NextGenMap indexes in the human reference
directory before enabling shared reference caching to avoid concurrent index
creation. Keep scratch and cache directories outside reference source directories.

## Verify

Run the automated tests in the activated environment:

```sh
python -m unittest discover -s tests -v
```

The tests use real Snakemake with fake processing tools. They check mixed layouts,
optional outputs and Cutadapt, sample concurrency, no-op and forced reruns,
missing/changed inputs and outputs, parameter changes, allocated threads, failure
recovery, diagnostics, cleanup, reference caching, and an exported directory.
Workflow tests skip when Snakemake is not installed; executor tests still run.

For an offline check using the real tools and generated tiny references/reads:

```sh
python tests/real_tools_smoke.py --work-dir /tmp/biopipe-real-smoke
```

The directory must be empty or absent. The script builds a tiny Kraken database,
processes paired and single-end data, and verifies that host reads were removed
and the synthetic microbial reads classified correctly. To additionally compare
the report, per-read classifications, and decompressed non-human FASTQs with an
existing sample executor, add `--compare-with /path/to/single-task/biopipe.py`.
That comparison is optional and is not a runtime dependency of this version.

Verified with Snakemake 9.13.7: all 9 automated tests passed. An offline real-tool
comparison for both read layouts matched the existing single-task executor's
Kraken report and per-read output byte for byte, with matching decompressed
non-human FASTQs. These tiny fixtures verify behavior, not performance or
classification accuracy on real samples.
