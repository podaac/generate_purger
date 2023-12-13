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
import requests

# Constants
ARCHIVE_DIR = pathlib.Path("/mnt/data/archive")
TOPIC_STRING = "batch-job-failure"
DATASETS = {
    "aqua": "MODIS_A-JPL-L2P-v2019.0", 
    "terra": "MODIS_T-JPL-L2P-v2019.0", 
    "viirs": "VIIRS_NPP-JPL-L2P-v2016.2"
}

# Functions
def purger_handler(event, context):
    """Handles events from EventBridge and orchestrates file deletion."""
    
    start = datetime.datetime.now()
        
    operations = event["operations"]
    prefix = event["prefix"]
    logger = get_logger()
    
    # EFS
    if "efs" in operations:
        logger.info("Running operations to remove data from EFS.")
        purge_efs(prefix, logger)
    
    # S3
    if "s3" in operations:
        logger.info("Running operations to remove data from S3.")
        purge_s3(prefix, logger)
        
    end = datetime.datetime.now()
    logger.info(f"Execution time: {end - start}.")

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
        
def purge_efs(prefix, logger):
    """Purge files from the EFS."""
    
    try:
        purger_dict = read_config(prefix)
        logger.info(f"Read config file: s3://{prefix}/config/purger.json")
        
        # Generate lists of files to archive or delete
        generate_file_lists(purger_dict, logger)
        logger.info(f"Gathered list of files to archive and delete for Generate components from the EFS.")
        
        # Archive and delete files
        deleted, archived, zipped = archive_and_delete(purger_dict, logger)
        
        # Upload archives to S3 bucket
        upload_archives(zipped, prefix, logger)
        
        # Report on operations
        report_ops(deleted, archived, logger)
    
    except Exception as error:
        message = f"The purger encountered the following error: {error}.\n"
        logger.error(message)
        logger.error(traceback.format_exc())
        publish_event(message, logger)

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
    
    # Determine status of holding tank files and update list
    sort_holding_tank(purger_dict, today, logger)
 
def sort_holding_tank(purger_dict, today, logger):
    """Sort the holding tank files by processing type: quicklook or refined."""
        
    # Combine quicklook and refined file lists
    holding_tank = [*set(purger_dict["combiner"]["holding_tank_quicklook"]["file_list"] + purger_dict["combiner"]["holding_tank_refined_modis"]["file_list"] + purger_dict["combiner"]["holding_tank_refined_viirs"]["file_list"])]

    # Clear dictionary file lists
    purger_dict["combiner"]["holding_tank_quicklook"]["file_list"] = []
    purger_dict["combiner"]["holding_tank_refined_modis"]["file_list"] = []
    purger_dict["combiner"]["holding_tank_refined_viirs"]["file_list"] = [] 
        
    # Sort lists by processing type
    for nc_file in holding_tank:
        file_date = datetime.datetime.strptime(nc_file.name.split('.')[1], "%Y%m%dT%H%M%S")
        file_date = file_date.replace(tzinfo=datetime.timezone.utc)
        file_mod = datetime.datetime.fromtimestamp(os.path.getmtime(nc_file), datetime.timezone.utc)
        # Check if file age is in current month first day of the month as OBPG releases the first of the month differently
        if file_date.month == today.month and file_date.day == 1:
            check_processing_type(nc_file, file_mod, today, purger_dict)
        # Refined files for previous months
        elif file_date.month < today.month:
            sort_refined_holding(nc_file, today, file_mod, purger_dict)
        # Quicklook files for current month
        elif file_date.month == today.month:
            purger_dict["combiner"]["holding_tank_quicklook"]["file_list"].append(nc_file)
        else:
            logger.info(f"Could not determine file age and threshold to delete: {nc_file}.")
            logger.info(f"File date: {file_mod}. Today's date: {today}.")
            logger.info("File not deleted.")
        
def check_processing_type(nc_file, file_mod, today, purger_dict):
    """Determine processing type of NetCDF file and if passed threshold add to 
    dictionary."""
    
    ds = Dataset(nc_file)
    product_name = ds.product_name
    if "NRT" in product_name:
        purger_dict["combiner"]["holding_tank_quicklook"]["file_list"].append(nc_file)
    else:
        sort_refined_holding(nc_file, today, file_mod, purger_dict)
    ds.close()
    
def sort_refined_holding(nc_file, today, file_mod, purger_dict):
    """Determine whether file is MODIS or VIIRS and if outside of threshold."""
    
    file_age = today - file_mod
    age_hours = (file_age.total_seconds()) / (60 * 60)
    
    if "MODIS" in nc_file.name:
        if (age_hours >= purger_dict["combiner"]["holding_tank_refined_modis"]["threshold"]):
            purger_dict["combiner"]["holding_tank_refined_modis"]["file_list"].append(nc_file)
            
    if "VIIRS" in nc_file.name:
        if (age_hours >= purger_dict["combiner"]["holding_tank_refined_viirs"]["threshold"]):
            purger_dict["combiner"]["holding_tank_refined_viirs"]["file_list"].append(nc_file)
        
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
                    logger.info(f"Deleted: {file}.")
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
                        logger.info(f"Archived: {file}.")
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

def purge_s3(prefix, logger):
    """Purge L2P S3 buckets of granules that have been ingested."""
    
    try:
        bucket = f"{prefix}-l2p-granules"
        s3 = boto3.client("s3")
        
        # Generate a dictionary of L2P granules organized by dataset
        s3_dict = generate_s3_list(s3, bucket)
        logger.info(f"Gathered list of L2P granules still present in the staging bucket: 's3://{prefix}-l2p-granules'.")
        
        # Check if granules were found
        gran_count = 0
        for dataset in DATASETS.keys():
            gran_count += len(s3_dict[dataset])        
        
        if gran_count != 0:
            # Search CMR for ingested L2P granules
            del_dict = query_cmr_and_delete(s3, bucket, s3_dict, prefix, logger)
            logger.info(f"Deleted L2P granules that have been ingested.")
        else:
            del_dict = {}
            for dataset in DATASETS.keys(): del_dict[dataset] = []
        
        # Report on operations
        report_s3(s3_dict, del_dict, logger)
        
    except Exception as error:
        message = f"The purger encountered the following error: {error}.\n"
        logger.error(message)
        logger.error(traceback.format_exc())
        publish_event(message, logger)
        
def generate_s3_list(s3, bucket):
    """Generate list of L2P granules in S3 bucket."""
    
    s3_dict = {}
    for dataset in DATASETS.keys():
        s3_dict[dataset] = []
        try:
            response = s3.list_objects_v2(Bucket=bucket,
                                          Prefix=dataset)
            if "Contents" in response.keys():
                    if len(response["Contents"]) > 0:
                        s3_dict[dataset].extend(parse_s3_response(response))
                        
                        # Page through results
                        if "NextContinuationToken" in response.keys():
                            next_token = response["NextContinuationToken"]
                        else:
                            next_token = ""
                        while next_token:
                            response = s3.list_objects_v2(Bucket=bucket, 
                                                        Prefix=dataset, 
                                                        ContinuationToken=next_token)
                            if len(response["Contents"]) > 0:
                                s3_dict[dataset].extend(parse_s3_response(response))
                            if "NextContinuationToken" in response.keys():
                                next_token = response["NextContinuationToken"]
                            else:
                                next_token = ""
        except botocore.exceptions.ClientError as e:
            raise e
        
    return s3_dict

def parse_s3_response(response):
    """Retrieve L2P granule names from response."""
    
    s3_list = []
    for key in response["Contents"]:
        l2p = key["Key"].split('/')[-1]
        if ".nc.md5" in l2p: continue
        if ".nc" in l2p: s3_list.append(l2p.split('.nc')[0])
    return s3_list

def query_cmr_and_delete(s3, bucket, s3_dict, prefix, logger):
    """Query CMR to determine if granules have been ingested."""
    
    if prefix.endswith("-sit") or prefix.endswith("-uat"):
        cmr_url = "https://cmr.uat.earthdata.nasa.gov/search/granules.umm_json"
    else:
        cmr_url = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
        
    token = get_edl_token(prefix, logger)
    
    del_dict = {}
    for dataset, l2ps in s3_dict.items():
        logger.info(f"Located {len(l2ps)} {dataset} granules in S3 bucket.")
        logger.info(f"Querying and deleting data for: {DATASETS[dataset]}.")
        del_dict[dataset] = []
        for l2p in l2ps:
            try:
                # Search for granule
                headers = { "Authorization": f"Bearer {token}" }
                params = {
                    "short_name": DATASETS[dataset],
                    "readable_granule_name": l2p
                }
                res = requests.post(url=cmr_url, headers=headers, params=params)      
                coll = res.json()

                # Parse response
                if "errors" in coll.keys():
                    logger.error(f"Search error response - {coll['errors']}")
                elif "hits" in coll.keys() and coll["hits"] > 0:
                    files = coll["items"][0]["umm"]["DataGranule"]["ArchiveAndDistributionInformation"]
                    for file in files:
                        if file["Name"] == f"{l2p}.nc": 
                            del_dict[dataset].append(l2p)
                            delete_l2p(s3, bucket, dataset, l2p ,logger)
                elif "hits" in coll.keys() and coll["hits"] == 0:
                    logger.info(f"{l2p} exists in S3 but was not ingested.")
            except botocore.exceptions.ClientError as e:
                raise e
            except Exception as e:
                logger.error(f"Error encountered - {e}")
                logger.info(f"Response - {res}")  
                logger.info(f"L2P granule could not be located: {l2p}.")
                logger.info("Discontinuing search and removal. Deletion operations will continue at next run.")
                return del_dict
    return del_dict

def get_edl_token(prefix, logger):
    """Retrieve EDL bearer token from SSM parameter store."""
    
    try:
        ssm_client = boto3.client('ssm', region_name="us-west-2")
        token = ssm_client.get_parameter(Name=f"{prefix}-edl-token", WithDecryption=True)["Parameter"]["Value"]
        logger.info("Retrieved EDL token from SSM Parameter Store.")
        return token
    except botocore.exceptions.ClientError as error:
        logger.error("Could not retrieve EDL credentials from SSM Parameter Store.")
        raise error
    
def delete_l2p(s3, bucket, dataset, l2p, logger):
    """Delete ingested L2P granules from S3 bucket."""
    
    try:
        response = s3.delete_object(Bucket=bucket,
                                    Key=f"{dataset}/{l2p}.nc")
        logger.info(f"Deleted: {bucket}/{dataset}/{l2p}.nc.")
        response = s3.delete_object(Bucket=bucket,
                                    Key=f"{dataset}/{l2p}.nc.md5")
        logger.info(f"Deleted: {bucket}/{dataset}/{l2p}.nc.md5.")
    except botocore.exceptions.ClientError as e:
        raise e

def report_s3(s3_dict, del_dict, logger):
    """Report on granules that were deleted from S3 bucket."""
    
    datasets = s3_dict.keys()
    to_delete = 0
    deleted = 0
    for dataset in datasets:
        to_delete += len(s3_dict[dataset])
        deleted += len(del_dict[dataset])

    logger.info(f"{to_delete} L2P granules were found.")
    logger.info(f"{deleted} L2P granules were deleted.")
    
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
