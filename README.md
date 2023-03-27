# purger

The purger program is an AWS Lambda function that delete data from EFS mount.

It deletes files from the archive, downloader, combiner, and processor components that are older than a specific threshold. The purger does archive input file lists for each component.

Top-level Generate repo: https://github.com/podaac/generate

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