"""AWS Lambda that checks deletes files older than a threshold from EFS mount.

Defines a dictionary of components and paths to delete from alongside a 
specified threshold value for each file type.

Logs a delete report and publishes to batch job failure SNS Topic if errors
are encountered.
"""

# Standard imports
import datetime
import glob
import json
import logging
import os
import pathlib
import shutil
import sys
import traceback
import zipfile

# Third-party imports
import boto3
import botocore
import fsspec
from netCDF4 import Dataset

# Constants
ARCHIVE_DIR = pathlib.Path("/mnt/data/archive")
TOPIC_STRING = "batch-job-failure"

# Functions
def purger_handler(event, context):
    """Handles events from EventBridge and orchestrates file deletion."""
    
    try:
        logger = get_logger()
        purger_dict = read_config(event["prefix"])
        logger.info(f"Read config file: s3://{event['prefix']}/config/purger.json")
        
        # Generate lists of files to archive or delete
        generate_file_lists(purger_dict, logger)
        logger.info(f"Gathered list of files to archive and delete for Generate components from the EFS.")
        
        # Archive and delete files
        deleted, archived, zipped = archive_and_delete(purger_dict, logger)
        
        # Upload archives to S3 bucket
        upload_archives(zipped, event["prefix"], logger)
        
        # Report on operations
        report_ops(deleted, archived, logger)
    
    except Exception as error:
        message = f"The purger encountered the following error: {error}.\n"
        logger.error(message)
        logger.error(traceback.format_exc())
        publish_event(message, logger)
    
def get_logger():
    """Return a formatted logger object."""
    
    # Remove AWS Lambda logger
    logger = logging.getLogger()
    for handler in logger.handlers:
        logger.removeHandler(handler)
    
    # Create a Logger object and set log level
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)

    # Create a handler to console and set level
    console_handler = logging.StreamHandler()

    # Create a formatter and add it to the handler
    console_format = logging.Formatter("%(module)s - %(levelname)s : %(message)s")
    console_handler.setFormatter(console_format)

    # Add handlers to logger
    logger.addHandler(console_handler)

    # Return logger
    return logger

def read_config(prefix):
    """Read in JSON config file for AWS Batch job submission."""
    
    s3_url = f"s3://{prefix}/config/purger.json"
    with fsspec.open(s3_url, mode='r') as fh:
        purger_dict = json.load(fh)
    return purger_dict

def generate_file_lists(purger_dict, logger):
    """Generate lists of files for each component that will be deleted or
    archived."""
        
    # Gather list of files past threshold value
    today = datetime.datetime.now(datetime.timezone.utc)
    for component, paths in purger_dict.items():
        for path_name, path_dict in paths.items():
            purger_dict[component][path_name]["file_list"] = []
            # Locate files and filter by threshold value
            for glob_op in path_dict["glob_ops"]:
                
                files = glob.glob(f"{path_dict['path']}/{glob_op}")
                for file in files:
                    file_mod = datetime.datetime.fromtimestamp(os.path.getmtime(file), datetime.timezone.utc)
                    file_age = today - file_mod
                    age_hours = (file_age.total_seconds()) / (60 * 60)
                    if (age_hours >= path_dict["threshold"]):
                        purger_dict[component][path_name]["file_list"].append(pathlib.Path(file))
                        logger.info(f"File to {purger_dict[component][path_name]['action']}: {pathlib.Path(file)}.")
    
    # Determine status of holding tank files and update list
    sort_holding_tank(purger_dict, today)
 
def sort_holding_tank(purger_dict, today):
    """Sort the holding tank files by processing type: quicklook or refined."""
    
    # Combine quicklook and refined file lists
    holding_tank = [*set(purger_dict["combiner"]["holding_tank_quicklook"]["file_list"] + purger_dict["combiner"]["holding_tank_refined"]["file_list"])]
    
    # Clear dictionary file lists
    purger_dict["combiner"]["holding_tank_quicklook"]["file_list"] = []
    purger_dict["combiner"]["holding_tank_refined"]["file_list"] = []
    
    # Sort lists by processing type
    for nc_file in holding_tank:
        ds = Dataset(nc_file)
        product_name = ds.product_name
        if "NRT" in product_name:
            purger_dict["combiner"]["holding_tank_quicklook"]["file_list"].append(nc_file)
        else:
            file_mod = datetime.datetime.fromtimestamp(os.path.getmtime(nc_file), datetime.timezone.utc)
            file_age = today - file_mod
            age_hours = (file_age.total_seconds()) / (60 * 60)
            if (age_hours >=  purger_dict["combiner"]["holding_tank_refined"]["threshold"]):
                purger_dict["combiner"]["holding_tank_refined"]["file_list"].append(nc_file)
        ds.close()
        
def archive_and_delete(purger_dict, logger):
    """Archive and/or delete files found in list for each component path.
    
    Returns list of deleted files.
    """    
    
    deleted = []
    archive = {}
    for component, paths in purger_dict.items():
        archive[component] = {}
        for path_name, path_dict in paths.items():
            archive[component][path_name] = []
            for file in path_dict["file_list"]:
                if path_dict["action"] == "delete":
                    if file.is_file(): file.unlink()    # Delete file
                    if file.is_dir(): shutil.rmtree(file)    # Delete directory
                    deleted.append(str(file))
                else:
                    archive[component][path_name].append(file)
                    
    # Compress archive files and place in archive directory
    archived = {}
    zipped = {}
    date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    for component, paths in archive.items():
        archived[component] = {}
        zipped[component] = {}
        for path_name, file_list in paths.items():
            if len(file_list) > 0:
                zip_file = ARCHIVE_DIR.joinpath(component, f"{path_name}_{date}.zip")
                logger.info(f"Zip created: {zip_file}.")
                zip_file.parent.mkdir(parents=True, exist_ok=True)
                archived[component][path_name] = []
                zipped[component][path_name] = []
                with zipfile.ZipFile(zip_file, mode='w') as archive:
                    for file in file_list: 
                        archive.write(file, arcname=file.name)
                        if file.is_file(): file.unlink()   # Remove after zipping
                        if file.is_dir(): shutil.rmtree(file)
                        archived[component][path_name].append(file.name)
                zipped[component][path_name].append(zip_file)

    return deleted, archived, zipped
    
def report_ops(deleted, archived, logger):
    """Report on files that were deleted and/or archived."""
    
    logger.info(f"{len(deleted)} files have been removed from the file system.")

    count = 0
    for ptype_dict in archived.values():
        for file_list in ptype_dict.values():
            count += len(file_list)
    logger.info(f"{count} files have been archived and then removed from the file system.")
        
def upload_archives(archived_dict, prefix, logger):
    """Upload text files to S3 bucket."""
    
    year = datetime.datetime.now().year
    try:
        s3_client = boto3.client("s3")
        for component, ptype_dict in archived_dict.items():
            for zip_files in ptype_dict.values():
                for zip_file in zip_files:
                    # Upload file to S3 bucket
                    response = s3_client.upload_file(str(zip_file), prefix, f"archive/{component}/{year}/{zip_file.name}", ExtraArgs={"ServerSideEncryption": "aws:kms"})
                    logger.info(f"File uploaded: s3://{prefix}/archive/{component}/{year}/{zip_file.name}.")
                    # Delete file from EFS
                    zip_file.unlink()
                    logger.info(f"File deleted: {zip_file}.")
    except botocore.exceptions.ClientError as e:
        logger.error(f"Could not upload archived zip files to: s3://{prefix}/archive/{component}/{year}/.")
        raise e
    
def publish_event(message, logger):
    """Publish event to SNS Topic."""
    
    sns = boto3.client("sns")
    
    # Get topic ARN
    try:
        topics = sns.list_topics()
    except botocore.exceptions.ClientError as e:
        logger.error("Failed to list SNS Topics.")
        logger.error(f"Error - {e}")
        sys.exit(1)
    for topic in topics["Topics"]:
        if TOPIC_STRING in topic["TopicArn"]:
            topic_arn = topic["TopicArn"]
            
    # Publish to topic
    subject = f"Generate Purger Lambda Failure"
    try:
        response = sns.publish(
            TopicArn = topic_arn,
            Message = message,
            Subject = subject
        )
    except botocore.exceptions.ClientError as e:
        logger.error(f"Failed to publish to SNS Topic: {topic_arn}.")
        logger.error(f"Error - {e}")
        sys.exit(1)
    
    logger.info(f"Message published to SNS Topic: {topic_arn}.")
