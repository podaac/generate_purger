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
import zipfile

# Third-party imports
import boto3
import botocore
import fsspec

# Constants
ARCHIVE_DIR = pathlib.Path("/mnt/data/archive")
TOPIC_STRING = "batch-job-failure"

# Functions
def purger_handler(event, context):
    """Handles events from EventBridge and orchestrates file deletion."""
    
    try:
        logger = get_logger()
        purger_dict = read_config(event["prefix"])
        logger.info(f"Read config file: s3://{event['prefix']}-download-lists/purger/purger.json")
        
        # Generate lists of files to archive or delete
        generate_file_lists(purger_dict)
        logger.info(f"Gathered list of files to archive and delete for Generate components from the EFS.")
        
        # Archive and delete files
        deleted, archived = archive_and_delete(purger_dict, logger)
        
        # Report on operations
        report_ops(deleted, archived, logger)
    
    except Exception as error:
        message = f"The purger encountered the following error: {error}.\n"
        logger.error(message)
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
    
    s3_url = f"s3://{prefix}-download-lists/purger/purger.json"
    with fsspec.open(s3_url, mode='r') as fh:
        purger_dict = json.load(fh)
    return purger_dict

def generate_file_lists(purger_dict):
    """Generate lists of files for each component that will be deleted or
    archived."""
    
    for component, paths in purger_dict.items():
        for path_name, path_dict in paths.items():
            purger_dict[component][path_name]["file_list"] = []
            # Locate files and filter by threshold value
            for glob_op in path_dict["glob_ops"]:
                
                files = glob.glob(f"{path_dict['path']}/{glob_op}")
                today = datetime.datetime.now(datetime.timezone.utc)
                for file in files:
                    file_mod = datetime.datetime.fromtimestamp(os.path.getmtime(file), datetime.timezone.utc)
                    file_age = today - file_mod
                    if (file_age.days >= path_dict["threshold"]):
                        purger_dict[component][path_name]["file_list"].append(pathlib.Path(file))
    
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
    date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    for component, paths in archive.items():
        for path_name, file_list in paths.items():
            if len(file_list) > 0:
                zip_file = ARCHIVE_DIR.joinpath(component, f"{path_name}_{date}.zip")
                zip_file.parent.mkdir(parents=True, exist_ok=True)
                archived[zip_file] = []
                with zipfile.ZipFile(zip_file, mode='w') as archive:
                    for file in file_list: 
                        archive.write(file, arcname=file.name)
                        if file.is_file(): file.unlink()   # Remove after zipping
                        if file.is_dir(): shutil.rmtree(file)
                        archived[zip_file].append(file.name)
                logger.info(f"{zip_file} created for: {path_name['path']}.")
    
    return deleted, archived
    
def report_ops(deleted, archived, logger):
    """Report on files that were deleted and/or archived."""
    
    if len(deleted) == 0: 
        logger.info("0 files have been removed from the file system.")
    else:
        logger.info(f"{len(deleted)} files have been removed from the file system.")
    
    count = 0
    for file_list in archived.values():
        count += len(file_list)
    if count == 0: 
        logger.info("0 files have been archived and then removed.")   
    else:
         logger.info(f"{count} files have been archived and then removed from the file system.")    
    
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
