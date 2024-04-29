# Biopipe
Taxonomy classification pipeline (bacteria, virus, fungi)

Biopipe is a collection of bioinformatics programs setup to process sequencing data parallely on a Hopsworks cluster. 

### Flow chart

<img src="/biopipe_flow.png" height="85%" width="65%" >



### Contents
The content of the repository is as follows:

- src - contains the source Jupyter notebooks files for each bioinformatic program
- settings - consists configuration file for arguments to the program in YAML format
- jobs_config - Hopsworks Jobs configuration JSONs
- airflow_pipeline - Python script for the running the pipeline as Airflow DAG

To run the pipeline we need a running Hospworks cluster. To know more about open-source version of Hopsworks and installation check the [github repo](https://github.com/dhananjay-mk/hopsworks).