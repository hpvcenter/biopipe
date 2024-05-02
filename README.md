# Biopipe
Taxonomy classification pipeline (bacteria, virus, fungi)

Biopipe is a collection of bioinformatics programs setup to process sequencing data parallely on a Hopsworks cluster. 
Biopipe was designed to comprise the following steps: 

- quality trimming ([trimmomatic](https://github.com/usadellab/Trimmomatic) and [cutadapt](https://github.com/marcelm/cutadapt/))
- human genome filtering ([nextgenmap](https://github.com/Cibiv/NextGenMap) and [samtools](https://github.com/samtools/samtools))
- taxonomy classification ([Kraken2](https://github.com/DerrickWood/kraken2)) 
- cut-off settlement and downstream analysis (R scripts including: [Kraken biom](https://github.com/smdabdoub/kraken-biom/tree/master), [Phyloseq](https://github.com/joey711/phyloseq), [Metagenomeseq](https://github.com/HCBravoLab/metagenomeSeq)).

### Flow chart

<img src="/biopipe_flow.png" height="85%" width="65%" >



### Contents
The content of the repository is as follows:

- src - contains the source Jupyter notebooks files for each bioinformatic program
- settings - consists configuration file for arguments to the program in YAML format
- airflow_pipeline - Python script for the running the pipeline as Airflow DAG
- downstream_analysis - R scripts for diversity analysis (including converstion to biom, alpha and beta diversity and differencial abundance analysis)

### Installation 

To setup the pipeline we first need a running Hospworks cluster. To know more about open-source version of Hopsworks and installation check the [github repo](https://github.com/dhananjay-mk/hopsworks) or visit the official [documentation](https://docs.hopsworks.ai/latest/)

Once the Hopsworks cluster is installed, the pipeline can be setup in below steps:
 1. Create a Hopsworks project
 1. Install the packages using PyPI or conda installer in the Project Environment

    - **trimmomatic** - upload jar
    - **cutadapt**- pip 
    - **nextgenmap** - conda
    - **samtools** - conda
    - **pysam** - pip
    - **kraken2** - build from source

 1. Create datasets for output and input. Upload the data to be processed into the input dataset
 2. Clone this repo into the Hopsworks project or upload the source code inside the `Jupyter` dataset.
 3. Modify the `settings.yml` to have the correct `OUTPUT_DATASET` path, `INPUT_ROOT_PATH` and a work directory in `RUN_FOLDER` to store all the output sub-folders. Next, check and modify the path parameters for each job step and any arguments to the programs you want to change. e.g. kraken2 reference database path.
 4. Create jobs for each program. Adjust the number of executors and its memory in `Advanced configuration` according to the resources available in the cluster.
 5. Upload the airflow pipeline python script. This contains the Airflow DAG. You have the modify `PROJECT_NAME`, `DAG_OWNER` to the Hopsworks username and provide a unique DAG name under `dag_id`. Also modify the `SETTINGS` field to the correct project path of the `settings.yml` you created.
