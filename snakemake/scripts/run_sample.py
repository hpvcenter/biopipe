"""Snakemake script entry point; source directory is supplied by Snakemake."""

import sys

sys.path.insert(0, snakemake.scriptdir)
import biopipe

config = biopipe.validate_config(snakemake.config)
sample = next(sample for sample in config["samples"] if sample["id"] == snakemake.wildcards.sample)
expected = {str(path) for path in biopipe.output_paths(config, sample).values()}
if expected != set(snakemake.output):
    raise ValueError("rule outputs do not match the configured sample products")
biopipe.process_sample(sample, config, threads=snakemake.threads)
