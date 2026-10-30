# Repository guidance

Biopipe has two open source Spark setups for metagenomic FASTQ processing:

- `step-wise/`: one Spark job per stage; intermediate files are stored on shared local storage or HDFS. A failed run can resume at the first unfinished stage.
- `single-task/`: one Spark task per sample; intermediate files stay in local scratch, and the task publishes final results to shared storage.

Both setups use JSON configuration, Trimmomatic, optional Cutadapt, NextGenMap, samtools, and Kraken2. Each has a Dockerfile, Conda environment, example configurations, and tests. The root `README.md` gives a short overview; each setup README has detailed commands. `Flowchart.png` illustrates the broader pipeline design, including steps not implemented by these runners.

Keep the two implementations' configuration fields and documented behavior aligned when changing a shared concept. Check storage and cleanup side effects before running on real data. The checkout can contain untracked experimental notebooks and settings; treat them as user work.

Run local tests with `python3 -m unittest discover -s step-wise/tests` and `python3 -m unittest discover -s single-task/tests`. Spark tests may skip when PySpark is unavailable. Configuration validation can be run without input data using each runner's default `--mode validate`.
