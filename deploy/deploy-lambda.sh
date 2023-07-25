#!/bin/bash
#
# Script to create a zipped deployment package for a Lambda function.
#
# Command line arguments:
# [1] app_name: Name of application to create a zipped deployment package for
# 
# Example usage: ./delpoy-lambda.sh "my-app-name"

APP_NAME=$1
ROOT_PATH="$PWD"

# Install dependencies
sudo apt update
sudo apt install -y netcdf-bin libnetcdf-dev
pip install --target $ROOT_PATH/package typing-extensions requests~=2.29.0 fsspec~=2023.3.0 s3fs~=2023.3.0 netCDF4~=1.6.4

# Zip dependencies
cd package/
zip -r ../$APP_NAME.zip .

# Zip script
cd ..
zip $APP_NAME.zip $APP_NAME.py
echo "Created: $APP_NAME.zip."
