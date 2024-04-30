# Biopipe
Taxonomy classification pipeline (bacteria, virus, fungi)

Biopipe is a collection of bioinformatics programs setup to process sequencing data parallely on a Hopsworks cluster. 
Biopipe was designed to comprise the following steps: quality trimming ([trimmomatic](https://github.com/usadellab/Trimmomatic) and [cutadapt](https://github.com/marcelm/cutadapt/)), human genome filtering ([nextgenmap](https://github.com/Cibiv/NextGenMap)), [samtools](https://github.com/samtools/samtools), taxonomy classification ([Kraken2](https://github.com/DerrickWood/kraken2)), cut-off settlement and downstream analysis (R scripts including: [Kraken biom](https://github.com/smdabdoub/kraken-biom/tree/master), [Phyloseq](https://github.com/joey711/phyloseq), [Metagenomeseq](https://github.com/HCBravoLab/metagenomeSeq)).

### Flow chart

<img src="/biopipe_flow.png" height="85%" width="65%" >



### Contents
The content of the repository is as follows:

- src - contains the source Jupyter notebooks files for each bioinformatic program
- settings - consists configuration file for arguments to the program in YAML format
- jobs_config - Hopsworks Jobs configuration JSONs
- airflow_pipeline - Python script for the running the pipeline as Airflow DAG
- downstream_analysis - R scripts for diversity analysis (including converstion to biom, alpha and beta diversity and differencial abundance analysis)

To setup the pipeline we first need a running Hospworks cluster. To know more about open-source version of Hopsworks and installation check the [github repo](https://github.com/dhananjay-mk/hopsworks) or visit the official [documentation](https://docs.hopsworks.ai/latest/)

Once the Hopsworks cluster is installed, the pipeline can be setup in below steps:

 1. Create datasets for output and input. Upload the data into the input dataset
 2. Clone this repo into the Hopsworks project or upload the source code 
 3. Modify the settings.yml
 4. Create jobs. You may use the jobs_config JSON files to import the job configs
 5. Upload the airflow pipeline python script. This contains the Airflow DAG. You have the modify project name, user name and provide a unique DAG name
