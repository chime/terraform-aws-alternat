locals {
  autoscaling_func_env_vars = {
    # Lambda function env vars cannot contain hyphens
    for obj in var.vpc_az_maps
    : replace(upper(obj.az), "-", "_") => join(",", obj.route_table_ids)
  }
  has_ipv6_env_var = { "HAS_IPV6" = var.lambda_has_ipv6 }
  lambda_runtime   = "python3.12"
}

data "archive_file" "lambda" {
  count       = var.lambda_package_type == "Zip" ? 1 : 0
  type        = "zip"
  source_dir  = "${path.module}/functions/replace-route"
  excludes    = ["__pycache__"]
  output_path = var.lambda_zip_path
}

# Lambda function for Auto Scaling Group Lifecycle Hook
resource "aws_lambda_function" "alternat_autoscaling_hook" {
  function_name = var.autoscaling_hook_function_name
  architectures = var.lambda_function_architectures
  package_type  = var.lambda_package_type
  memory_size   = var.lambda_memory_size
  timeout       = var.lambda_timeout
  role          = aws_iam_role.nat_lambda_role.arn

  layers = var.lambda_layer_arns

  image_uri = var.lambda_package_type == "Image" ? "${var.alternat_image_uri}:${var.alternat_image_tag}" : null

  runtime          = var.lambda_package_type == "Zip" ? local.lambda_runtime : null
  handler          = var.lambda_package_type == "Zip" ? var.lambda_handlers.alternat_autoscaling_hook : null
  filename         = var.lambda_package_type == "Zip" ? data.archive_file.lambda[0].output_path : null
  source_code_hash = var.lambda_package_type == "Zip" ? data.archive_file.lambda[0].output_base64sha256 : null

  environment {
    variables = merge(
      local.autoscaling_func_env_vars,
      { NAT_GATEWAY_ID = var.nat_gateway_id },
      var.lambda_environment_variables,
    )
  }

  tags = merge({
    FunctionName = "alternat-autoscaling-lifecycle-hook",
  }, var.tags)
}

resource "aws_iam_role" "nat_lambda_role" {
  name               = var.nat_lambda_function_role_name == "" ? null : var.nat_lambda_function_role_name
  name_prefix        = var.nat_lambda_function_role_name == "" ? "alternat-lambda-role-" : null
  assume_role_policy = data.aws_iam_policy_document.nat_lambda_policy.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "nat_lambda_basic_execution_role_attachment" {
  role       = aws_iam_role.nat_lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

data "aws_iam_policy_document" "alternat_lambda_permissions" {
  statement {
    sid    = "alterNATDescribePermissions"
    effect = "Allow"
    actions = [
      "ec2:DescribeNatGateways",
      "ec2:DescribeRouteTables",
      "ec2:DescribeSubnets",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "alterNATDescribeASG"
    effect = "Allow"
    actions = [
      "autoscaling:DescribeAutoScalingGroups"
    ]
    resources = ["*"]
  }

  statement {
    sid    = "alterNATModifyRoutePermissions"
    effect = "Allow"
    actions = [
      "ec2:ReplaceRoute"
    ]
    resources = [
      for route_table in local.all_route_tables
      : "arn:aws:ec2:${data.aws_region.current.name}:${data.aws_caller_identity.current.id}:route-table/${route_table}"
    ]
  }
}

data "aws_iam_policy_document" "nat_lambda_policy" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    effect = "Allow"
  }
}

resource "aws_iam_role_policy" "alternat_lambda_permissions" {
  name   = "alternat-lambda-permissions-policy"
  policy = data.aws_iam_policy_document.alternat_lambda_permissions.json
  role   = aws_iam_role.nat_lambda_role.name
}

resource "aws_lambda_permission" "sns_topic_to_alternat_lambda" {
  statement_id  = "AllowExecutionFromSNS"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.alternat_autoscaling_hook.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.alternat_topic.arn
}

resource "aws_sns_topic_subscription" "nat_lambda_topic_subscription" {
  topic_arn = aws_sns_topic.alternat_topic.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.alternat_autoscaling_hook.arn
}

# Lambda function for monitoring connectivity through the NAT instance
resource "aws_lambda_function" "alternat_connectivity_tester" {
  for_each = { for obj in var.vpc_az_maps : obj.az => obj }

  function_name = "${var.connectivity_tester_function_name}-${each.key}"
  architectures = var.lambda_function_architectures
  package_type  = var.lambda_package_type
  memory_size   = var.lambda_memory_size
  timeout       = var.lambda_timeout
  role          = aws_iam_role.nat_lambda_role.arn

  layers = var.lambda_layer_arns

  image_uri = var.lambda_package_type == "Image" ? "${var.alternat_image_uri}:${var.alternat_image_tag}" : null

  runtime          = var.lambda_package_type == "Zip" ? local.lambda_runtime : null
  handler          = var.lambda_package_type == "Zip" ? var.lambda_handlers.connectivity_tester : null
  filename         = var.lambda_package_type == "Zip" ? data.archive_file.lambda[0].output_path : null
  source_code_hash = var.lambda_package_type == "Zip" ? data.archive_file.lambda[0].output_base64sha256 : null

  dynamic "image_config" {
    for_each = var.lambda_package_type == "Image" ? [var.lambda_handlers.connectivity_tester] : []

    content {
      command = [image_config.value]
    }
  }

  environment {
    variables = merge(
      {
        ROUTE_TABLE_IDS_CSV = join(",", each.value.route_table_ids),
        PUBLIC_SUBNET_ID    = each.value.public_subnet_id
        CHECK_URLS          = join(",", var.connectivity_test_check_urls)
        NAT_GATEWAY_ID      = var.nat_gateway_id,
      },
      local.has_ipv6_env_var,
      var.lambda_environment_variables,
    )
  }

  vpc_config {
    subnet_ids         = each.value.private_subnet_ids
    security_group_ids = [aws_security_group.nat_lambda.id]
  }

  tags = merge({
    FunctionName = "alternat-connectivity-tester-${each.key}",
  }, var.tags)
}

resource "aws_security_group" "nat_lambda" {
  name_prefix = "alternat-lambda"
  vpc_id      = var.vpc_id
  tags        = var.tags
}

resource "aws_security_group_rule" "nat_lambda_egress" {
  type              = "egress"
  protocol          = "-1"
  from_port         = 0
  to_port           = 0
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.nat_lambda.id
}

resource "aws_cloudwatch_event_rule" "every_minute" {
  name                = var.connectivity_test_event_rule_name
  description         = "Fires every minute"
  schedule_expression = "rate(1 minute)"
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "test_connection_every_minute" {
  for_each = { for obj in var.vpc_az_maps : obj.az => obj }

  rule      = aws_cloudwatch_event_rule.every_minute.name
  target_id = "connectivity-tester-${each.key}"
  arn       = aws_lambda_function.alternat_connectivity_tester[each.key].arn
}

resource "aws_lambda_permission" "allow_cloudwatch_to_call_connectivity_tester" {
  for_each = { for obj in var.vpc_az_maps : obj.az => obj }

  statement_id  = "AllowExecutionFromCloudWatch"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.alternat_connectivity_tester[each.key].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.every_minute.arn
}
