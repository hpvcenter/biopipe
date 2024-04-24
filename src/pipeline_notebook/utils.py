#!/usr/bin/env python
# coding: utf-8
import argparse
import logging
import os
import traceback

import yaml
from hops import hdfs

R_IDENTIFIER = '_R'
R1 = '_R1'
R2 = '_R2'
R1_SUFFIX_EXTENSION = '_R1.fq.gz'
R2_SUFFIX_EXTENSION = '_R2.fq.gz'
LANE_SEPARATOR = '_L'
PART_SEPARATOR = 'part'
UNDERSCORE_SEPARATOR = '_'
SAMPLE_SEPARATOR = '_S'
TRIM_PAIRED = 'paired_'
TRIM_UNPAIRED = 'unpaired_'
SORTED_PREFIX = 'sorted_'
UNMAPPED_BAM = '_unmapped.bam'
SAMTOOLS = 'samtools'
SPACE = ' '
EMPTY = ''
INPUT_OVERRIDE='INPUT_OVERRIDE'
OUTPUT_OVERRIDE='OUTPUT_OVERRIDE'
OUTPUT_DATASET='OUTPUT_DATASET'
INPUT_ROOT_PATH='INPUT_ROOT_PATH'
RUN_FOLDER='RUN_FOLDER'

### YAML file keys start ###
KEY_TRIMMOMATIC = 'Trimmomatic'
KEY_NGM = 'Nextgenmap'
KEY_SAM = 'FilterSAM'
KEY_MERGE = 'Merge'
KEY_SORTCONVERT = 'SortConvert'
KEY_DIAMOND = 'Diamond'
KEY_UPLOAD = 'Upload'
KEY_SPLITFASTQ = 'Split_Fastq'
KEY_FILTER_DIAMOND = 'Filter_Diamond'
KEY_REMOVE_HUMAN = 'Unhuman'
### YAML file keys end ###

### Error messages ###
NO_CONFIG_ERR = 'No settings file provided'
DIAMOND_ERR = 'Diamond Installation failed'
TRIMMOMATIC_NOT_FOUND = 'Trimmomatic Jar file not found'
TRIMMOMATIC_ADAPTER_NOT_FOUND = 'Trimmomatic Adapter file not found'
SKIP_FILE = 'Skipping as output file already exists for input '
NOT_VALID_SETTINGS='Not valid settings HDFS file path'
### end errors ###


def group_R1R2(files):
    '''
    pairs r1 and r2 as tuple into a list
    '''
    global sample_name_r1, sample_name_r2
    r1 = list(filter(lambda x: R1 in x, files))
    r2 = list(filter(lambda x: R2 in x, files))
    paired = list(zip(r1, r2))
    # validate each pair
    for x in paired:
        sample_name_r1 = get_sampleName_with_lane(os.path.basename(x[0]))
        sample_name_r2 = get_sampleName_with_lane(os.path.basename(x[1]))
        if sample_name_r1 != sample_name_r2:
            raise ValueError('Could not group input as valid R1 and R2 pairs', sample_name_r1, sample_name_r2)

    return paired

def find_unique_names(files):
    '''
    get unique sample names from list of file names
    '''
    duplicates = []
    for f in files:
        f = os.path.split(f)[1]
        f = f.split(LANE_SEPARATOR)[0]
        if 'part' in f:  # if part exists then split from 'part' keyword. This would also exclude paired or unpaired keywors
            f = f.split(PART_SEPARATOR)[1]
            f = f.split(UNDERSCORE_SEPARATOR, 1)[1]
        else:  # check if paired or unpaired exists and exclude
            l = f.split(TRIM_PAIRED)
            f = l[-1]  # always  use the last element which has the sample name

        duplicates.append(f)

    return list(set(duplicates))  # return only unique names


def find_samples_with_lane(files):
    '''
    get unique sample names with lane from list of file names
    '''
    duplicates = []
    for f in files:
        f = os.path.split(f)[1]

        if 'part' in f:  # if part exists then split from 'part' keyword. This would also exclude paired or unpaired keywors
            f = f.split(PART_SEPARATOR)[1]
            f = f.split(UNDERSCORE_SEPARATOR, 1)[1]
        else:  # check if paired or unpaired exists and exclude
            l = f.split(TRIM_PAIRED)
            f = l[-1]  # always  use the last element which has the sample name

        duplicates.append(f)

    return list(set(duplicates))  # return only unique names


def load_file_names(hdfs_root):
    """
    returns a list of hdfs file paths in a folder recursively
    """
    files_list = [d['path'] for d in hdfs.lsl(hdfs_root, recursive=True) if d['kind'] == 'file']
    if 'README.md' in files_list:
        files_list.remove('README.md')
    return files_list


def load_arguments(argv):
    """
    load the arguments from YAML file
    """
    parser = argparse.ArgumentParser(description="Argument Parser for Pipeline Jobs")
    parser.add_argument("-s", "--settings", help="Settings YAML file HDFS path.")
    parser.add_argument("-i", "--input", help="Input root HDFS path override (optional)")
    parser.add_argument("-o", "--output", help="Output root HDFS path override (optional)")
    options = parser.parse_args(argv[1:])
    if options.settings:
        settings_path = options.settings
    else:
        raise ValueError(NO_CONFIG_ERR)

    if not hdfs.exists(settings_path):
        raise OSError(NOT_VALID_SETTINGS,settings_path)

    logging.info('Reading configuration file at: ', settings_path)
    settings_file=hdfs.load(settings_path)
    args = yaml.full_load(settings_file)

    if options.input :
        args[INPUT_OVERRIDE]=options.input
    if options.output:
        args[OUTPUT_OVERRIDE]=options.output

    return args



def combine_all_lanes(files, nbr_of_lanes):
    l1, l2, l3, l4, combined_bam = ([],) * 5

    for i in range(0, len(files), nbr_of_lanes):
        combined_bam.append(files[i:i + nbr_of_lanes])

    return combined_bam


def build_command(tool, params):
    '''
    concatenate the arguments to form the command as a string
    '''
    if params:
        str_list = [tool]

        for key, value in params.items():
            str_list.append(SPACE)
            str_list.append(key)
            str_list.append(SPACE)
            str_list.append(str(value))

        return ''.join(str_list)
    else:
        return -1


def find_number_of_lanes(files):
    duplicates = [os.path.splitext(f)[0].split(LANE_SEPARATOR)[0] for f in files]
    return duplicates.count(duplicates[0])


def find_file_like(search):
    all_files = os.listdir()
    for i in all_files:
        if search in i:
            return i
    return None


def print_on_new_line(x):
    print(*x, sep='\n')


def skip_file(input_file_name, output_file_name, output_folder):
    if hdfs.exists(os.path.join(output_folder, output_file_name)):
        print(SKIP_FILE, input_file_name)
        return 1


def get_sampleName_with_lane(file):
    """
    Get samples name removing prefixes of '_R'.
    """
    name = os.path.splitext(file)[0]
    no_r = name.split(R_IDENTIFIER)[0]
    return no_r


def hdfs_delete_file(hdfsPath):
    """
    Delete single hdfs file
    :param hdfsPath:
    :return:
    """
    if hdfs.exists(hdfsPath):
        print('INFO: Deleting file at ', hdfsPath)
        hdfs.delete(hdfs.get_plain_path(hdfsPath))


def compareAndDeleteFiles(inputRoot, outputRoot, outputPrefixRegex, outputSuffixRefex, outputExt):
    """
    Used to reconcile number of input and output files.
    IMPORTANT: If a corresponding output file is not found, the input file is deleted
    :param inputRoot:
    :param outputRoot:
    :param outputPrefixRegex:
    :param outputSuffixRefex:
    :param outputExt:
    :return:
    """
    input_files = load_file_names(inputRoot)
    print('Total input folder files ', len(input_files))
    output_files = load_file_names(outputRoot)
    output_files = [os.path.basename(x) for x in output_files]
    print('Total output folder files', len(output_files))
    missing_files = []
    for x in input_files:
        input_file = os.path.basename(x)
        output_file = outputPrefixRegex + input_file + outputSuffixRefex + outputExt
        if output_file not in output_files:
            missing_files.append(input_file)

    print('Missing Files (Number)', len(missing_files), missing_files)
    print('Deleting input files failed in output')
    deleted = [hdfs.delete(hdfs.get_plain_path(os.path.join(inputRoot, x))) for x in missing_files]
    print('Number of files deleted at input folder:', len(deleted))
