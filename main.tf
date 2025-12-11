## NAT instance configuration
locals {
  initial_lifecycle_hooks = [
    {
      name                    = "NATInstanceTerminationLifeCycleHook"
      default_result          = "CONTINUE"
      heartbeat_timeout       = var.lifecycle_heartbeat_timeout
      lifecycle_transition    = "autoscaling:EC2_INSTANCE_TERMINATING"
      notification_target_arn = aws_sns_topic.alternat_topic.arn
      role_arn                = aws_iam_role.alternat_lifecycle_hook.arn
    }
  ]
  launch_script_lifecycle_hook_name = "NATInstanceLaunchScript"

  nat_instance_image_id = var.nat_ami == "" ? "resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-minimal-kernel-default-${var.architecture}" : var.nat_ami

  nat_instance_ingress_sgs = concat(var.ingress_security_group_ids, [aws_security_group.nat_lambda.id])

  all_route_tables = flatten([
    for obj in var.vpc_az_maps : obj.route_table_ids
  ])

  # One private subnet in each AZ to use for the VPC endpoints
  az_private_subnets = [for obj in var.vpc_az_maps : element(obj.private_subnet_ids, 0)]
  ec2_endpoint = (
    var.enable_ec2_endpoint
    ? {
      ec2 = {
        service             = "ec2"
        private_dns_enabled = true
        subnet_ids          = local.az_private_subnets
        tags                = { Name = "ec2-vpc-endpoint" }
      }
    }
    : {}
  )

  # Must provide exactly 1 EIP per AZ
  # var.nat_instance_eip_ids ignored if doesn't match AZ count
  reuse_nat_instance_eips = length(var.nat_instance_eip_ids) == length(var.vpc_az_maps)
  nat_instance_eip_ids    = local.reuse_nat_instance_eips ? var.nat_instance_eip_ids : (var.prevent_destroy_eips ? aws_eip.protected_nat_instance_eips[*].id : aws_eip.nat_instance_eips[*].id)
  nat_instance_eips       = var.prevent_destroy_eips ? aws_eip.protected_nat_instance_eips : aws_eip.nat_instance_eips
  nat_gateway_eips        = var.prevent_destroy_eips ? aws_eip.protected_nat_gateway_eips : aws_eip.nat_gateway_eips

  created_ngw_eip_alloc_ids   = try({ for az, e in aws_eip.nat_gateway_eips : az => e.id }, {})
  protected_ngw_eip_alloc_ids = try({ for az, e in aws_eip.protected_nat_gateway_eips : az => e.id }, {})
  explicit_ngw_eip_alloc_ids  = var.fallback_ngw_eip_allocation_ids

  # NAT Gateway EIP allocation IDs to use for fallback routes
  # Explicit preferred, then protected, then created
  ngw_alloc_ids = merge(
    local.created_ngw_eip_alloc_ids,
    local.protected_ngw_eip_alloc_ids,
    local.explicit_ngw_eip_alloc_ids
  )
}

resource "aws_eip" "protected_nat_instance_eips" {
  count = (local.reuse_nat_instance_eips
    ? 0
  : var.prevent_destroy_eips ? length(var.vpc_az_maps) : 0)

  tags = merge(var.tags, {
    "Name" = "alternat-instance-${count.index}"
  })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_eip" "nat_instance_eips" {
  count = (local.reuse_nat_instance_eips
    ? 0
  : (var.prevent_destroy_eips ? 0 : length(var.vpc_az_maps)))

  tags = merge(var.tags, {
    "Name" = "alternat-instance-${count.index}"
  })
}

resource "aws_sns_topic" "alternat_topic" {
  name_prefix       = "alternat-topic"
  kms_master_key_id = "alias/aws/sns"
  tags              = var.tags
}

resource "aws_autoscaling_group" "nat_instance" {
  for_each = { for obj in var.vpc_az_maps : obj.az => obj.public_subnet_id }

  name_prefix           = var.nat_instance_name_prefix
  max_size              = 1
  min_size              = 1
  max_instance_lifetime = var.max_instance_lifetime
  vpc_zone_identifier   = [each.value]

  launch_template {
    id      = aws_launch_template.nat_instance_template[each.key].id
    version = "$Latest"
  }

  dynamic "initial_lifecycle_hook" {
    for_each = local.initial_lifecycle_hooks
    content {
      name                    = initial_lifecycle_hook.value.name
      default_result          = try(initial_lifecycle_hook.value.default_result, null)
      heartbeat_timeout       = try(initial_lifecycle_hook.value.heartbeat_timeout, null)
      lifecycle_transition    = initial_lifecycle_hook.value.lifecycle_transition
      notification_metadata   = try(initial_lifecycle_hook.value.notification_metadata, null)
      notification_target_arn = try(initial_lifecycle_hook.value.notification_target_arn, null)
      role_arn                = try(initial_lifecycle_hook.value.role_arn, null)
    }
  }

  health_check_grace_period = var.enable_launch_script_lifecycle_hook ? 0 : 300

  instance_maintenance_policy {
    min_healthy_percentage = var.allow_launch_before_terminating ? 100 : 0
    max_healthy_percentage = var.allow_launch_before_terminating ? 200 : 100
  }

  dynamic "tag" {
    for_each = merge(
      var.tags,
      { Name = "${var.nat_instance_name_prefix}${each.key}" },
      data.aws_default_tags.current.tags,
    )

    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }
}

resource "aws_autoscaling_lifecycle_hook" "nat_instance_launch_script" {
  for_each = var.enable_launch_script_lifecycle_hook ? toset([for obj in var.vpc_az_maps : obj.az]) : []

  autoscaling_group_name = aws_autoscaling_group.nat_instance[each.key].name

  name                 = local.launch_script_lifecycle_hook_name
  default_result       = "ABANDON"
  heartbeat_timeout    = 900
  lifecycle_transition = "autoscaling:EC2_INSTANCE_LAUNCHING"
}

resource "aws_iam_role" "alternat_lifecycle_hook" {
  name        = var.nat_instance_lifecycle_hook_role_name == "" ? null : var.nat_instance_lifecycle_hook_role_name
  name_prefix = var.nat_instance_lifecycle_hook_role_name == "" ? "alternat-lifecycle-hook-" : null

  assume_role_policy = data.aws_iam_policy_document.lifecycle_hook_assume_role.json
  tags               = var.tags
}

data "aws_iam_policy_document" "lifecycle_hook_assume_role" {
  statement {
    sid = "AutoScalingAssumeRole"

    actions = [
      "sts:AssumeRole",
    ]

    principals {
      type        = "Service"
      identifiers = ["autoscaling.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "lifecycle_hook_policy" {
  statement {
    sid    = "alterNATLifecycleHookPermissions"
    effect = "Allow"
    actions = [
      "sns:Publish",
    ]
    resources = [aws_sns_topic.alternat_topic.arn]
  }
}

resource "aws_iam_role_policy" "alternat_lifecycle_hook" {
  name   = "lifecycle-publish-policy"
  policy = data.aws_iam_policy_document.lifecycle_hook_policy.json
  role   = aws_iam_role.alternat_lifecycle_hook.name
}

data "cloudinit_config" "config" {
  for_each = { for obj in var.vpc_az_maps : obj.az => obj.route_table_ids }

  gzip          = true
  base64_encode = true

  dynamic "part" {
    for_each = var.nat_instance_user_data_pre_install != "" ? [1] : []

    content {
      content_type = "text/x-shellscript"
      content      = var.nat_instance_user_data_pre_install
    }
  }

  part {
    content_type = "text/x-shellscript"
    content = templatefile("${path.module}/alternat.conf.tftpl", {
      eip_allocation_ids_csv  = join(",", local.nat_instance_eip_ids),
      route_table_ids_csv     = join(",", each.value),
      enable_ssm              = var.enable_ssm,
      enable_cloudwatch_agent = var.enable_cloudwatch_agent
    })
  }

  part {
    content_type = "text/x-shellscript"
    content      = file("${path.module}/scripts/alternat.sh")
  }

  dynamic "part" {
    for_each = var.enable_cloudwatch_agent ? [1] : []

    content {
      content_type = "text/x-shellscript"
      content = templatefile("${path.module}/cwagent.json.tftpl", {
        cloudwatch_namespace  = var.cloudwatch_namespace,
        cloudwatch_interfaces = jsonencode(var.cloudwatch_interfaces)
      })
    }
  }

  dynamic "part" {
    for_each = var.nat_instance_user_data_post_install != "" ? [1] : []

    content {
      content_type = "text/x-shellscript"
      content      = var.nat_instance_user_data_post_install
    }
  }
}

resource "aws_launch_template" "nat_instance_template" {
  for_each = { for obj in var.vpc_az_maps : obj.az => obj.route_table_ids }

  name_prefix = var.nat_instance_name_prefix

  image_id = local.nat_instance_image_id

  # Conditional block device mapping for AL2023 Minimal AMI.
  # By default the root volume is only 2GB and not enough free space
  # to safely install and use the CloudWatch Agent.
  dynamic "block_device_mappings" {
    for_each = (try(strcontains(local.nat_instance_image_id, "al2023-ami-minimal"), false) && var.enable_cloudwatch_agent) ? [1] : []

    content {
      device_name = "/dev/xvda"

      ebs {
        volume_size = 3
        volume_type = "gp3"
        encrypted   = true
      }
    }
  }

  dynamic "block_device_mappings" {
    for_each = try(var.nat_instance_block_devices, {})

    content {
      device_name = try(block_device_mappings.value.device_name, null)

      dynamic "ebs" {
        for_each = try([block_device_mappings.value.ebs], [])

        content {
          encrypted   = try(ebs.value.encrypted, null)
          volume_size = try(ebs.value.volume_size, null)
          volume_type = try(ebs.value.volume_type, null)
        }
      }
    }
  }

  iam_instance_profile {
    name = aws_iam_instance_profile.nat_instance.name
  }

  instance_type = var.nat_instance_type

  key_name = var.nat_instance_key_name

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  monitoring {
    enabled = true
  }

  network_interfaces {
    associate_public_ip_address = true
    security_groups             = [aws_security_group.nat_instance.id]
  }

  tags = var.tags
  tag_specifications {
    resource_type = "instance"

    tags = merge(var.tags, {
      alterNATInstance = "true",
    })
  }

  tag_specifications {
    resource_type = "volume"

    tags = merge(var.tags, {
      alterNATInstance = "true",
    })
  }
  user_data = data.cloudinit_config.config[each.key].rendered
}

resource "aws_security_group" "nat_instance" {
  name_prefix = var.nat_instance_sg_name_prefix
  vpc_id      = var.vpc_id
  tags        = var.tags
}

resource "aws_security_group_rule" "nat_instance_egress" {
  type              = "egress"
  protocol          = "-1"
  from_port         = 0
  to_port           = 0
  cidr_blocks       = ["0.0.0.0/0"]
  ipv6_cidr_blocks  = ["::/0"]
  security_group_id = aws_security_group.nat_instance.id
}

resource "aws_security_group_rule" "nat_instance_ingress" {
  count = length(local.nat_instance_ingress_sgs)

  type                     = "ingress"
  protocol                 = "-1"
  from_port                = 0
  to_port                  = 0
  security_group_id        = aws_security_group.nat_instance.id
  source_security_group_id = local.nat_instance_ingress_sgs[count.index]
}

resource "aws_security_group_rule" "nat_instance_ip_range_ingress" {
  count = length(var.ingress_security_group_cidr_blocks) > 0 ? 1 : 0

  type              = "ingress"
  protocol          = "-1"
  from_port         = 0
  to_port           = 0
  security_group_id = aws_security_group.nat_instance.id
  cidr_blocks       = var.ingress_security_group_cidr_blocks
}

resource "aws_security_group_rule" "nat_instance_ipv6_range_ingress" {
  count = length(var.ingress_security_group_ipv6_cidr_blocks) > 0 ? 1 : 0

  type              = "ingress"
  protocol          = "-1"
  from_port         = 0
  to_port           = 0
  security_group_id = aws_security_group.nat_instance.id
  ipv6_cidr_blocks  = var.ingress_security_group_ipv6_cidr_blocks
}

### NAT instance IAM

resource "aws_iam_instance_profile" "nat_instance" {
  name        = var.nat_instance_iam_profile_name == "" ? null : var.nat_instance_iam_profile_name
  name_prefix = var.nat_instance_iam_profile_name == "" ? "alternat-instance-" : null

  role = aws_iam_role.alternat_instance.name
  tags = var.tags
}

resource "aws_iam_role" "alternat_instance" {
  name        = var.nat_instance_iam_role_name == "" ? null : var.nat_instance_iam_role_name
  name_prefix = var.nat_instance_iam_role_name == "" ? "alternat-instance-" : null

  assume_role_policy = data.aws_iam_policy_document.nat_instance_assume_role.json
  tags               = var.tags
}

data "aws_iam_policy_document" "nat_instance_assume_role" {
  statement {
    sid = "NATInstanceAssumeRole"

    actions = [
      "sts:AssumeRole",
    ]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy_attachment" "ssm" {
  count      = var.enable_ssm ? 1 : 0
  role       = aws_iam_role.alternat_instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "cloudwatch" {
  count      = var.enable_cloudwatch_agent ? 1 : 0
  role       = aws_iam_role.alternat_instance.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

data "aws_iam_policy_document" "alternat_ec2_policy" {
  statement {
    sid    = "alterNATInstancePermissions"
    effect = "Allow"
    actions = [
      "ec2:ModifyInstanceAttribute",
      "ec2:DescribeInstanceAttribute"
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/alterNATInstance"
      values = [
        "true"
      ]
    }
  }

  statement {
    sid    = "alterNATDescribeRoutePermissions"
    effect = "Allow"
    actions = [
      "ec2:DescribeRouteTables"
    ]
    resources = ["*"]
  }

  statement {
    sid    = "alterNATModifyRoutePermissions"
    effect = "Allow"
    actions = [
      "ec2:CreateRoute",
      "ec2:ReplaceRoute"
    ]
    resources = [
      for route_table in local.all_route_tables
      : "arn:aws:ec2:${data.aws_region.current.id}:${data.aws_caller_identity.current.id}:route-table/${route_table}"
    ]
  }

  statement {
    sid    = "alterNATEIPPermissions"
    effect = "Allow"
    actions = [
      "ec2:DescribeAddresses",
      "ec2:AssociateAddress"
    ]
    resources = ["*"]
  }

  statement {
    sid    = "alterNATASGLifecyclePermissions"
    effect = "Allow"
    actions = [
      "autoscaling:CompleteLifecycleAction",
    ]
    resources = [
      "arn:aws:autoscaling:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:autoScalingGroup:*:autoScalingGroupName/${var.nat_instance_name_prefix}*",
    ]
  }
}

resource "aws_iam_role_policy" "alternat_ec2" {
  name   = "alternat-policy"
  policy = data.aws_iam_policy_document.alternat_ec2_policy.json
  role   = aws_iam_role.alternat_instance.name
}

resource "aws_iam_role_policy" "alternat_additional_policies" {
  count = length(var.additional_instance_policies)

  name   = var.additional_instance_policies[count.index].policy_name
  policy = var.additional_instance_policies[count.index].policy_json
  role   = aws_iam_role.alternat_instance.name
}

## NAT Gateway used as a backup route
resource "aws_eip" "protected_nat_gateway_eips" {
  for_each = {
    for obj in var.vpc_az_maps
    : obj.az => obj.public_subnet_id
    if var.create_nat_gateways && var.prevent_destroy_eips && !contains(keys(var.fallback_ngw_eip_allocation_ids), obj.az)
  }
  tags = merge(var.tags, {
    "Name" = "alternat-gateway-eip"
  })
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_eip" "nat_gateway_eips" {
  for_each = {
    for obj in var.vpc_az_maps
    : obj.az => obj.public_subnet_id
    if var.create_nat_gateways && !var.prevent_destroy_eips && !contains(keys(var.fallback_ngw_eip_allocation_ids), obj.az)
  }
  tags = merge(var.tags, {
    "Name" = "alternat-gateway-eip"
  })
}

resource "aws_nat_gateway" "main" {
  for_each = {
    for obj in var.vpc_az_maps
    : obj.az => obj.public_subnet_id
    if var.create_nat_gateways
  }
  allocation_id = local.ngw_alloc_ids[each.key]
  subnet_id     = each.value
  tags = merge(var.tags, {
    Name = "alternat-${each.key}"
  })
}

data "aws_vpc" "vpc" {
  id = var.vpc_id
}

locals {
  all_vpc_cidr_ranges = [
    for cidr_assoc in data.aws_vpc.vpc.cidr_block_associations
    : cidr_assoc.cidr_block
  ]
}

resource "aws_security_group" "vpc_endpoint" {
  count = length(local.ec2_endpoint) > 0 ? 1 : 0

  name_prefix = "ec2-vpc-endpoints-"
  description = "Allow TLS from the VPC CIDR to the AWS API."
  vpc_id      = var.vpc_id

  ingress {
    description = "TLS from within the VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = local.all_vpc_cidr_ranges
  }

  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  tags = var.tags
}

module "vpc_endpoints" {
  count = length(local.ec2_endpoint) > 0 ? 1 : 0

  source             = "terraform-aws-modules/vpc/aws//modules/vpc-endpoints"
  version            = "~> 3.14.0"
  vpc_id             = var.vpc_id
  security_group_ids = [aws_security_group.vpc_endpoint[0].id]
  endpoints          = local.ec2_endpoint
  tags               = var.tags
}

data "aws_default_tags" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
