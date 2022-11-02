# Lambda function for Auto Scaling Group Lifecycle Hook
resource "aws_lambda_function" "ha_nat_autoscaling_hook" {
  function_name = var.autoscaling_hook_function_name
  package_type  = "Image"
  memory_size   = 256
  image_uri     = "${var.ha_nat_image_uri}:${var.ha_nat_image_tag}"
  role          = aws_iam_role.nat_lambda_role.arn
  environment {
    variables = {
      PRIVATE_SUBNET_SUFFIX = var.subnet_suffix
    }
  }
  tags = merge({
    FunctionName = "ha-nat-autoscaling-lifecycle-hook",
  }, var.tags)
  timeout = 300
}

resource "aws_iam_role" "nat_lambda_role" {
  name               = var.nat_lambda_function_role_name == "" ? null : var.nat_lambda_function_role_name
  name_prefix        = var.nat_lambda_function_role_name == "" ? "ha-nat-lambda-role-" : null
  assume_role_policy = data.aws_iam_policy_document.nat_lambda_policy.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "nat_lambda_basic_execution_role_attachment" {
  role       = aws_iam_role.nat_lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

data "aws_iam_policy_document" "ha_nat_lambda_permissions" {
  statement {
    sid    = "HANATDescribePermissions"
    effect = "Allow"
    actions = [
      "ec2:DescribeNatGateways",
      "ec2:DescribeRouteTables",
      "ec2:DescribeSubnets",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "HANATDescribeASG"
    effect = "Allow"
    actions = [
      "autoscaling:DescribeAutoScalingGroups"
    ]
    resources = ["*"]
  }

  statement {
    sid    = "HANATLambdaPermissions"
    effect = "Allow"
    actions = [
      "lambda:GetFunction"
    ]
    resources = ["*"]
  }

  statement {
    sid    = "HANATModifyRoutePermissions"
    effect = "Allow"
    actions = [
      "ec2:ReplaceRoute"
    ]
    resources = [
      for route_table in var.private_route_table_ids
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

resource "aws_iam_role_policy" "ha_nat_lambda_permissions" {
  name   = "ha-nat-lambda-permissions-policy"
  policy = data.aws_iam_policy_document.ha_nat_lambda_permissions.json
  role   = aws_iam_role.nat_lambda_role.name
}

resource "aws_lambda_permission" "sns_topic_to_ha_nat_lambda" {
  statement_id  = "AllowExecutionFromSNS"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ha_nat_autoscaling_hook.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.ha_nat_topic.arn
}

resource "aws_sns_topic_subscription" "nat_lambda_topic_subscription" {
  topic_arn = aws_sns_topic.ha_nat_topic.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.ha_nat_autoscaling_hook.arn
}

# Lambda function for monitoring connectivity through the NAT instance
resource "aws_lambda_function" "ha_nat_connectivity_tester" {
  count = length(var.vpc_private_subnet_ids)

  function_name = "${var.connectivity_tester_function_name}-${count.index}"
  package_type  = "Image"
  memory_size   = 256
  timeout       = 300
  image_uri     = "${var.ha_nat_image_uri}:${var.ha_nat_image_tag}"

  image_config {
    command = ["app.connectivity_test_handler"]
  }

  role = aws_iam_role.nat_lambda_role.arn

  vpc_config {
    subnet_ids         = [var.vpc_private_subnet_ids[count.index]]
    security_group_ids = [aws_security_group.nat_lambda.id]
  }

  tags = merge({
    FunctionName = "ha-nat-connectivity-tester-${count.index}",
  }, var.tags)
}

resource "aws_security_group" "nat_lambda" {
  name_prefix = "ha-nat-lambda"
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
  count = length(var.vpc_private_subnet_ids)

  rule      = aws_cloudwatch_event_rule.every_minute.name
  target_id = "connectivity-tester-${count.index}"
  arn       = aws_lambda_function.ha_nat_connectivity_tester[count.index].arn
}

resource "aws_lambda_permission" "allow_cloudwatch_to_call_connectivity_tester" {
  count = length(var.vpc_private_subnet_ids)

  statement_id  = "AllowExecutionFromCloudWatch"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ha_nat_connectivity_tester[count.index].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.every_minute.arn
}
