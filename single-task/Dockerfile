# syntax=docker/dockerfile:1
# Optional: build everything (tools, Spark runner) into one image.
#
#   docker build -t biopipe-single-task single-task   # local files, Spark local mode
#   docker build --target hdfs -t biopipe-single-task:hdfs single-task  # adds the Hadoop client for hdfs://
FROM mambaorg/micromamba:2.9.0 AS base
COPY --chown=$MAMBA_USER:$MAMBA_USER environment.yml /tmp/environment.yml
RUN micromamba install --yes --name base --file /tmp/environment.yml && micromamba clean --all --yes
ARG MAMBA_DOCKERFILE_ACTIVATE=1
ARG PYSPARK_VERSION=3.5.3
RUN pip install --no-cache-dir "pyspark==${PYSPARK_VERSION}"
RUN command -v trimmomatic && command -v ngm && command -v samtools && command -v kraken2 && command -v cutadapt
COPY --chown=$MAMBA_USER:$MAMBA_USER biopipe.py spark_runner.py /app/
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 JAVA_HOME=/opt/conda
# Mount a config at /config, data at /data, and a writable /scratch (and /cache).
ENTRYPOINT ["/usr/local/bin/_entrypoint.sh", "python", "/app/spark_runner.py"]
CMD ["--help"]

FROM ghcr.io/apache/hadoop:3.5.0 AS apache-hadoop

FROM base AS hdfs
COPY --from=apache-hadoop /opt/hadoop /opt/hadoop
ENV HADOOP_HOME=/opt/hadoop HADOOP_CONF_DIR=/opt/hadoop/etc/hadoop PATH=/opt/hadoop/bin:$PATH
# Mount the site's core-site.xml/hdfs-site.xml over HADOOP_CONF_DIR at run time.
RUN /opt/hadoop/bin/hadoop version

# Spark-on-Kubernetes target: Apache Spark's own image (entrypoint, user, Spark home)
# plus the bioinformatics tools. Spark starts driver and executor pods from this image
# (spark.kubernetes.container.image); /opt/tools/bin is last on PATH so Spark keeps its python3.
#   docker build --target spark-k8s -t biopipe-single-task:spark-k8s single-task
FROM apache/spark:3.5.3-scala2.12-java17-python3-ubuntu AS spark-k8s
USER root
COPY --from=mambaorg/micromamba:2.9.0 /bin/micromamba /usr/local/bin/micromamba
COPY environment.yml /tmp/environment.yml
RUN micromamba create --yes --prefix /opt/tools --file /tmp/environment.yml && micromamba clean --all --yes \
    && mkdir -p /app /scratch && chmod 1777 /scratch
COPY biopipe.py spark_runner.py /app/
ENV PATH=$PATH:/opt/tools/bin PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1
RUN command -v trimmomatic && command -v ngm && command -v samtools && command -v kraken2 && command -v cutadapt
USER 185

# Default build target (last stage): no Hadoop client.
FROM base AS local
