terraform {
  backend "s3" {
    encrypt = true
  }
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 4.0"
    }
  }
}

# Configure the AWS Provider
provider "aws" {
  default_tags {
    tags = local.default_tags
  }
  ignore_tags {
    key_prefixes = ["gsfc-ngap"]
  }
  region = var.aws_region
}

# Data sources
data "aws_caller_identity" "current" {}

data "aws_ecr_repository" "partition_submit" {
  name = "${var.prefix}-purger"
}

data "aws_efs_access_point" "fsap_purger" {
  access_point_id = var.fsap_id
}

data "aws_kms_key" "aws_s3" {
  key_id = "alias/aws/s3"
}

data "aws_kms_key" "ssm_key" {
  key_id = "alias/aws/ssm"
}

data "aws_s3_bucket" "generate_data" {
  bucket = var.prefix
}

data "aws_s3_bucket" "generate_l2p" {
  bucket = "${var.prefix}-l2p-granules"
}

data "aws_security_groups" "vpc_default_sg" {
  filter {
    name   = "group-name"
    values = ["default"]
  }
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.application_vpc.id]
  }
}

data "aws_sns_topic" "batch_failure_topic" {
  name = "${var.prefix}-batch-job-failure"
}

data "aws_ssm_parameter" "edl_token" {
  name = "${var.prefix}-edl-token"
}

data "aws_subnet" "private_application_subnet" {
  for_each = toset(data.aws_subnets.private_application_subnets.ids)
  id       = each.value
}

data "aws_subnets" "private_application_subnets" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.application_vpc.id]
  }
  filter {
    name   = "tag:Name"
    values = ["Private application*"]
  }
}

data "aws_vpc" "application_vpc" {
  tags = {
    "Name" : "Application VPC"
  }
}

# Local variables
locals {
  account_id = data.aws_caller_identity.current.account_id
  default_tags = length(var.default_tags) == 0 ? {
    application : var.app_name,
    environment : var.environment,
    version : var.app_version
  } : var.default_tags
}