# purger

The purger program is an AWS Lambda function that delete data from EFS mount.

It deletes files from the archive, downloader, combiner, and processor components that are older than a specific threshold. The purger does archive input file lists for each component.

Top-level Generate repo: https://github.com/podaac/generate

## purger config

The purger program executes using a configuration file called `purger.json`. It can be hosted in an S3 bucket so you only need to upload a new configuration to make changes to how the program operates. This can include: removing or adding additional files to archive and delete or changing what gets archived.

Description of configuration file parameters:

`path`: The path to the files that should be archived and/or deleted.

`threshold`: The age of the files/directories at which the file or directory should be deleted and/or archived.

`glob_ops`: The glob pathname pattern expression used to locate the files/directories found in the `path` which should be archived and/or deleted.

`action`: The action to take on the files/directories found at the `path`. 
- `delete` removes the files/directories from the file system. 
- `archive` zips the files/directories and places them in the `archive/component/pathname` directory and then removes them from the file system.

(Note: All archives are removed after the specified threshold found at the `archive -> input_lists -> threshold` parameter.)

## aws infrastructure

The purger program includes the following AWS services:
- Lambda function to execute code deployed via zip file.
- Permissions that allow an EventBridge Schedule to invoke the Lambda function.
- IAM role and policy for Lambda function execution.
- EventBridge schedule that executes the Lambda function every 55 minutes.

## terraform 

Deploys AWS infrastructure and stores state in an S3 backend.

To deploy:
1. Initialize terraform: 
    ```
    terraform init -backend-config="bucket=bucket-state" \
        -backend-config="key=component.tfstate" \
        -backend-config="region=aws-region" \
        -backend-config="profile=named-profile"
    ```
2. Plan terraform modifications: 
    ```
    ~/terraform plan -var="environment=venue" \
        -var="prefix=venue-prefix" \
        -var="profile=named-profile" \
        -out="tfplan"
    ```
3. Apply terraform modifications: `terraform apply tfplan`