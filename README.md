# Biopipe
Taxonomy classification pipeline (bacteria, virus, fungi)

Biopipe is a collection of bioinformatics programs setup to process sequencing data parallely on a Hopsworks cluster. 

### Flow chart

![Biopipe](/biopipe_flow.png?raw=true "Biopipe logical flow chart")


### Contents
The content of the repository is as follows:

- src - contains the source Jupyter notebooks files for each bioinformatic program
- settings - consists configuration file for arguments to the program in YAML format
- jobs_config - Hopsworks Jobs configuration JSONs
- airflow_pipeline - Python script for the running the pipeline as Airflow DAG

To run the pipeline we need a running Hospworks cluster.