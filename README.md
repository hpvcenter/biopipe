# Biopipe
Biopipe is an open-source, modular pipeline for standardized bioinformatic preprocessing and taxonomic classification of metagenomic sequencing data from environmental and clinical sources.
It enables parallelized, reproducible execution on Hopsworks, using distributed computing to handle large datasets efficiently.

This pipeline was used in the study: Mukhedkar D, et al. Stable cores and dynamic peripheries: spatial structuring dominates over temporal turnover in wastewater microbiomes across 16 Swedish cities.


**Overview**

Biopipe is a collection of bioinformatics programs setup to process sequencing data parallely on a Hopsworks cluster. 

Biopipe consists of two main modules:
  1) Preprocessing of sequencing data (adapter trimming, quality filtering, human read removal).
  2) Taxonomic classification using Kraken2 and Bracken.
Each step runs as a configurable, containerized job within the Hopsworks environment, supporting distributed execution via Apache Spark.
Biopipe was designed to comprise the following steps: 

**Pipeline Components**
1. Preprocessing of sequencing data (adapter trimming, quality filtering, human genome filtering)
   - Adapter trimming and quality filtering with Trimmomatic ([trimmomatic](https://github.com/usadellab/Trimmomatic)). Trimmomatic removes low-quality bases and adapters (min length: 36 bp; sliding window: 4:15).
   - Human read filtering with NextGenMap ([nextgenmap](https://github.com/Cibiv/NextGenMap) and [samtools](https://github.com/samtools/samtools)). Nextgenmap maps reads against human reference genome (GRCh38) using >95% identity over ≥75% read length. Samtools extracts non-human reads and reconverts to FASTQ format.
3. Taxonomic classification with Kraken2 ([Kraken2](https://github.com/DerrickWood/kraken2)) and abundance estimation with Bracken ([Bracken] (https://github.com/jenniferlu717/Bracken)) , which refines taxonomic abundances to the genus level.

### Flow chart

<img src="/flowchart.jpg" height="85%" width="65%" >


### Contents
The content of the repository is as follows:

- src - contains the source Jupyter notebooks files for each bioinformatic program
- settings - consists configuration file for arguments to the program in YAML format
- airflow_pipeline - Python script for the running the pipeline as Airflow DAG

**Installation and Setup**

To setup the pipeline we first need a running Hospworks cluster. To know more about open-source version of Hopsworks and installation check the [github repo](https://github.com/dhananjay-mk/hopsworks) or visit the official [documentation](https://docs.hopsworks.ai/latest/)

Once the Hopsworks cluster is installed, the pipeline can be setup in below steps:
 1. Create a Hopsworks project
 2. Install the packages using PyPI or conda installer in the Project Environment

    - **trimmomatic** - upload jar
    - **nextgenmap** - conda
    - **samtools** - conda
    - **pysam** - pip
    - **kraken2** - build from source
    - **braken2** - build from source

 3. Create datasets for output and input. Upload the data to be processed into the input dataset
 4. Clone this repo into the Hopsworks project or upload the source code inside the `Jupyter` dataset.
 5. Create dataset folders in Hopsworks project and upload the input data and optionally create output dataset.
 6. Modify the `settings.yml` to have the correct `OUTPUT_DATASET` path, `INPUT_ROOT_PATH` and a work directory in `RUN_FOLDER` to store all the output sub-folders. Next, check and modify the path parameters for each job step and any arguments to the programs you want to change. e.g. kraken2 reference database path.
 7. Create jobs for each program. Adjust the number of executors and its memory in `Advanced configuration` according to the resources available in the cluster. Typically executor memory of atleast 4GB and minimum 2 vcores is suggested. The number of executors can be increased upto the maximum capacity in cluster. 
 8. The jobs can be run independently from the Hopsworks Jobs from UI. This is useful if you want to run only a part or single program. Optionally to schedule or run the whole pipeline, Apache Airflow is available in the Hopsworks Project home page. Upload the airflow pipeline python script to the Hopsworks Airflow page. This contains the Airflow DAG. You have the modify `PROJECT_NAME`, `DAG_OWNER` to the Hopsworks username and provide a unique DAG name under `dag_id`. Also modify the `SETTINGS` field to the correct project path of the `settings.yml` you created. Once the configuration and file paths are set, the pipeline can be run from the Airflow home page by trigger the DAG created.

**Output**
Trimmed, filtered FASTQ files (non-human reads)
Kraken2 classification reports
Bracken genus-level abundance tables

**Citation**
If you use Biopipe, please cite:
Mukhedkar D, et al. (2025) Stable cores and dynamic peripheries: spatial structuring dominates over temporal turnover in wastewater microbiomes across 16 Swedish cities.
