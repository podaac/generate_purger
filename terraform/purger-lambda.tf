# AWS Lambda function
resource "aws_lambda_function" "aws_lambda_purger" {
  image_uri     = "${data.aws_ecr_repository.partition_submit.repository_url}:latest"
  function_name = "${var.prefix}-purger"
  role          = aws_iam_role.aws_lambda_execution_role.arn
  package_type  = "Image"
  timeout       = 900
  memory_size   = 3072
  vpc_config {
    subnet_ids         = data.aws_subnets.private_application_subnets.ids
    security_group_ids = data.aws_security_groups.vpc_default_sg.ids
  }
  file_system_config {
    arn              = data.aws_efs_access_point.fsap_purger.arn
    local_mount_path = "/mnt/data"
  }
}

# Upload purger configuration file to S3 bucket
resource "aws_s3_object" "aws_s3_bucket_job_configuration" {
  bucket                 = data.aws_s3_bucket.generate_data.id
  key                    = "config/purger.json"
  server_side_encryption = "aws:kms"
  source                 = "purger.json"
  etag                   = filemd5("purger.json")
}

# AWS Lambda role and policy
resource "aws_iam_role" "aws_lambda_execution_role" {
  name = "${var.prefix}-lambda-purger-execution-role"
  assume_role_policy = jsonencode({
    "Version" : "2012-10-17",
    "Statement" : [
      {
        "Effect" : "Allow",
        "Principal" : {
          "Service" : "lambda.amazonaws.com"
        },
        "Action" : "sts:AssumeRole"
      }
    ]
  })
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/NGAPShRoleBoundary"
}

resource "aws_iam_role_policy_attachment" "aws_lambda_execution_role_policy_attach" {
  role       = aws_iam_role.aws_lambda_execution_role.name
  policy_arn = aws_iam_policy.aws_lambda_execution_policy.arn
}

resource "aws_iam_policy" "aws_lambda_execution_policy" {
  name        = "${var.prefix}-lambda-purger-execution-policy"
  description = "Write to CloudWatch logs, list and delete from S3, publish to SQS."
  policy = jsonencode({
    "Version" : "2012-10-17",
    "Statement" : [
      {
        "Sid" : "AllowCreatePutLogs",
        "Effect" : "Allow",
        "Action" : [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ],
        "Resource" : "arn:aws:logs:*:*:*"
      },
      {
        "Sid" : "AllowVPCAccess",
        "Effect" : "Allow",
        "Action" : [
          "ec2:CreateNetworkInterface"
        ],
        "Resource" : concat([for subnet in data.aws_subnet.private_application_subnet : subnet.arn], ["arn:aws:ec2:${var.aws_region}:${local.account_id}:*/*"])
      },
      {
        "Sid" : "AllowVPCDelete",
        "Effect" : "Allow",
        "Action" : [
          "ec2:DeleteNetworkInterface"
        ],
        "Resource" : "arn:aws:ec2:${var.aws_region}:${local.account_id}:*/*"
      },
      {
        "Sid" : "AllowVPCDescribe",
        "Effect" : "Allow",
        "Action" : [
          "ec2:DescribeNetworkInterfaces"
        ],
        "Resource" : "*"
      },
      {
        "Sid" : "AllowEFSAccess",
        "Effect" : "Allow",
        "Action" : [
          "elasticfilesystem:ClientMount",
          "elasticfilesystem:ClientWrite",
          "elasticfilesystem:DescribeMountTargets"
        ],
        "Resource" : "${data.aws_efs_access_point.fsap_purger.file_system_arn}"
        "Condition" : {
          "StringEquals" : {
            "elasticfilesystem:AccessPointArn" : "${data.aws_efs_access_point.fsap_purger.arn}"
          }
        }
      },
      {
        "Sid" : "AllowListBucket",
        "Effect" : "Allow",
        "Action" : [
          "s3:ListBucket"
        ],
        "Resource" : [
          "${data.aws_s3_bucket.generate_data.arn}",
          "${data.aws_s3_bucket.generate_l2p.arn}"
        ]
      },
      {
        "Sid" : "AllowGetPutObject",
        "Effect" : "Allow",
        "Action" : [
          "s3:GetObject",
          "s3:PutObject"
        ],
        "Resource" : "${data.aws_s3_bucket.generate_data.arn}/*"
      },
      {
        "Sid" : "AllowGetDeleteObject",
        "Effect" : "Allow",
        "Action" : [
          "s3:GetObject",
          "s3:DeleteObject"
        ],
        "Resource" : "${data.aws_s3_bucket.generate_l2p.arn}/*"
      },
      {
        "Sid" : "AllowListTopics",
        "Effect" : "Allow",
        "Action" : [
          "sns:ListTopics"
        ],
        "Resource" : "*"
      },
      {
        "Sid" : "AllowPublishTopic",
        "Effect" : "Allow",
        "Action" : [
          "sns:Publish"
        ],
        "Resource" : "${data.aws_sns_topic.batch_failure_topic.arn}"
      },
      {
        "Sid" : "GetParameter",
        "Effect" : "Allow",
        "Action" : [
          "ssm:GetParameter*"
        ],
        "Resource" : "${data.aws_ssm_parameter.edl_token.arn}"
      },
      {
        "Sid" : "DecryptKey",
        "Effect" : "Allow",
        "Action" : [
          "kms:DescribeKey",
          "kms:Decrypt"
        ],
        "Resource" : "${data.aws_kms_key.ssm_key.arn}"
      }
    ]
  })
}

# EventBridge schedule
resource "aws_scheduler_schedule" "aws_schedule_purger" {
  name       = "${var.prefix}-purger"
  group_name = "default"
  flexible_time_window {
    mode = "OFF"
  }
  schedule_expression = "cron(00 11,23 * * ? *)"
  target {
    arn      = aws_lambda_function.aws_lambda_purger.arn
    role_arn = aws_iam_role.aws_eventbridge_purger_execution_role.arn
    input = jsonencode({
      "prefix" : "${var.prefix}",
      "operations" : ["efs", "s3"]
    })
  }
}

# EventBridge execution role and policy
resource "aws_iam_role" "aws_eventbridge_purger_execution_role" {
  name = "${var.prefix}-eventbridge-purger-execution-role"
  assume_role_policy = jsonencode({
    "Version" : "2012-10-17",
    "Statement" : [
      {
        "Effect" : "Allow",
        "Principal" : {
          "Service" : "scheduler.amazonaws.com"
        },
        "Action" : "sts:AssumeRole"
      }
    ]
  })
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/NGAPShRoleBoundary"
}

resource "aws_iam_role_policy_attachment" "aws_eventbridge_purger_execution_role_policy_attach" {
  role       = aws_iam_role.aws_eventbridge_purger_execution_role.name
  policy_arn = aws_iam_policy.aws_eventbridge_purger_execution_policy.arn
}

resource "aws_iam_policy" "aws_eventbridge_purger_execution_policy" {
  name        = "${var.prefix}-eventbridge-purger-execution-policy"
  description = "Allow EventBridge to invoke a Lambda function."
  policy = jsonencode({
    "Version" : "2012-10-17",
    "Statement" : [
      {
        "Sid" : "AllowInvokeLambda",
        "Effect" : "Allow",
        "Action" : [
          "lambda:InvokeFunction"
        ],
        "Resource" : "${aws_lambda_function.aws_lambda_purger.arn}"
      }
    ]
  })
}