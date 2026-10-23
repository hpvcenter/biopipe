# Repository guidance

## Purpose and execution model

Biopipe processes metagenomic sequencing reads in a Hopsworks project. The tracked pipeline uses Hopsworks Jupyter jobs and Spark to parallelize work over files stored in HDFS. Airflow launches those named Hopsworks jobs. The repository does not provide a standalone local runner, dependency lockfile, or automated test suite.

## Repository map

- `README.md`: overview and Hopsworks setup instructions.
- `src/pipeline_notebook/*.ipynb`: one notebook per processing step. Jobs load YAML settings through `utils.py`, list input files in HDFS, and use Spark to process files in parallel.
- `src/pipeline_notebook/utils.py`: shared HDFS settings loader, file naming helpers, and command helpers. Notebook jobs import it as `utils`, so make it available beside the notebooks in the Hopsworks job environment.
- `settings/settings_bench10_raw.yml`: tracked example settings for the preprocessing and classification steps. Paths and tool locations are project specific.
- `airflow_pipeline/bio-pipeline-t1.py`: Airflow DAG for the tracked job chain.

The DAG chain is `Trimming` -> `Nextgenmap` -> `ConvertSam2Bam` -> `SortConvert` -> `Kraken` (with an optional `Diamond` branch). The `braken.ipynb` notebook and `Bracken` YAML section exist, but this DAG does not launch Bracken. `RemoveHuman.ipynb` is another notebook outside that DAG chain.

## Configuration and deployment

- Jobs accept `-s <HDFS settings YAML>`; `-i` and `-o` override a job's input and output root. `utils.load_arguments` reads the settings file from HDFS, not the local filesystem.
- The main YAML keys are `OUTPUT_DATASET`, `INPUT_ROOT_PATH`, `RUN_FOLDER`, and job sections such as `Trimmomatic`, `Nextgenmap`, `FilterSAM`, `SortConvert`, `Kraken`, and `Bracken`. Outputs are generally placed under `OUTPUT_DATASET/RUN_FOLDER`.
- Before deploying, set the dataset paths, reference paths, tool paths, and thread counts for the target Hopsworks project. Create the corresponding Hopsworks jobs with names matching the DAG's `job_name` values.
- Set `DAG_OWNER`, `PROJECT_NAME`, `SETTINGS`, and `SETTINGS_SORT_CONVERT_ROUND2` in the Airflow file for the target project. The latter currently contains placeholder input and output paths.
- Runtime dependencies include Hopsworks `hops`, PySpark, `pysam`, PyYAML, Airflow with `hopsworks_plugin`, and external bioinformatics tools and references. Follow `README.md` for cluster setup; installing Python packages locally does not make the pipeline runnable without Hopsworks and HDFS.

## Working on the code

- Preserve notebook JSON structure and cell order when editing `.ipynb` files. Keep each notebook's settings keys aligned with its YAML section and the DAG job name.
- Several notebook steps execute external commands and copy or delete HDFS files. Inspect those side effects before running a job against real data. In particular, `ConvertSam2Bam.ipynb` can delete its input on a `pysam` error.
- The tracked DAG does not cover every notebook or output described in the README. Check the actual DAG and settings when changing pipeline behavior.
- The checkout may contain untracked experimental notebooks or settings. Treat them as user work; do not overwrite or remove them while working on the tracked pipeline.

## Lightweight local checks

- Validate YAML with `python3 -c "import yaml; yaml.safe_load(open('settings/settings_bench10_raw.yml'))"` when PyYAML is installed.
- Validate Python syntax with `python3 -m py_compile airflow_pipeline/bio-pipeline-t1.py src/pipeline_notebook/utils.py` (this creates `__pycache__` directories).
- Validate notebook JSON and code cell syntax with a small `json` and `ast` script. These checks do not exercise Hopsworks jobs or external tools.
