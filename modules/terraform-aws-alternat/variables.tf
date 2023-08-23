variable "additional_instance_policies" {
  description = "Additional policies for the HA NAT instance IAM role."
  type = list(object({
    policy_name = string
    policy_json = string
  }))
  default = []
}

variable "alternat_image_tag" {
  description = "The tag of the container image for the HA NAT Lambda functions."
  type        = string
  default     = "latest"
}

variable "alternat_image_uri" {
  description = "The URI of the container image for the HA NAT Lambda functions."
  type        = string
  default     = ""
}

variable "architecture" {
  description = "Architecture of the NAT instance image. Must be compatible with nat_instance_type."
  type        = string
  default     = "arm64"
}

variable "autoscaling_hook_function_name" {
  description = "The name to use for the autoscaling hook Lambda function."
  type        = string
  default     = "alternat-autoscaling-hook"
}

variable "create_nat_gateways" {
  description = "Whether to create the NAT Gateway and the NAT Gateway EIP in this module. If false, you must create and manage NAT Gateways separately."
  type        = bool
  default     = true
}

variable "connectivity_test_check_urls" {
  description = "List of URLs to check with the connectivity tester function."
  type        = list(string)
  default     = ["https://www.example.com", "https://www.google.com"]
}

variable "connectivity_test_event_rule_name" {
  description = "The name to use for the event rule that invokes the connectivity test Lambda function."
  type        = string
  default     = "alternat-test-every-minute"
}

variable "connectivity_tester_function_name" {
  description = "The prefix to use for the name of the connectivity tester Lambda function. Because there is a function created in each ASG, the name will be suffixed with an index."
  type        = string
  default     = "alternat-connectivity-tester"
}

variable "enable_ec2_endpoint" {
  description = "Whether to create a VPC endpoint to EC2 for Internet Connectivity testing."
  type        = bool
  default     = true
}

variable "enable_lambda_endpoint" {
  description = "Whether to create a VPC endpoint to Lambda for Internet Connectivity testing."
  type        = bool
  default     = true
}

variable "enable_ssm" {
  description = "Whether to enable SSM on the HA NAT instances."
  type        = bool
  default     = true
}

variable "ingress_security_group_ids" {
  description = "A list of security group IDs that are allowed by the NAT instance."
  type        = list(string)
  default     = []
}

variable "lifecycle_heartbeat_timeout" {
  description = "The length of time, in seconds, that autoscaled NAT instances should wait in the terminate state before being fully terminated."
  type        = number
  default     = 180
}

variable "max_instance_lifetime" {
  description = "Max instance life in seconds. Defaults to 14 days. Set to 0 to disable."
  type        = number
  default     = 1209600
}

variable "nat_ami" {
  description = "The AMI to use for the NAT instance. Defaults to the latest Amazon Linux 2 AMI."
  type        = string
  default     = ""
}

variable "key_name" {
  description = "The SSH KEY to use for the NAT instance."
  type        = string
  default     = ""

variable "nat_instance_block_devices" {
  description = "Optional custom EBS volume settings for the NAT instance."
  type        = any
  default     = {}
}

variable "nat_instance_iam_profile_name" {
  description = "Name to use for the IAM profile used by the NAT instance. Must be globally unique in this AWS account. Defaults to alternat-instance- as a prefix."
  type        = string
  default     = ""
}

variable "nat_instance_iam_role_name" {
  description = "Name to use for the IAM role used by the NAT instance. Must be globally unique in this AWS account. Defaults to alternat-instance- as a prefix."
  type        = string
  default     = ""
}

variable "nat_instance_lifecycle_hook_role_name" {
  description = "Name to use for the IAM role used by the NAT instance lifecycle hook. Must be globally unique in this AWS account. Defaults to alternat-lifecycle-hook as a prefix."
  type        = string
  default     = ""
}

variable "nat_instance_name_prefix" {
  description = "Prefix for the NAT Auto Scaling Group and instance names. Because there is an instance created in each ASG, the name will be suffixed with an index."
  type        = string
  default     = "alternat-"
}

variable "nat_instance_sg_name_prefix" {
  description = "Prefix for the NAT instance security group name."
  type        = string
  default     = "alternat-instance"
}

variable "nat_lambda_function_role_name" {
  description = "Name to use for the IAM role used by the replace-route Lambda function. Must be globally unique in this AWS account."
  type        = string
  default     = ""
}

variable "nat_instance_type" {
  description = "Instance type to use for NAT instances."
  type        = string
  default     = "c6gn.8xlarge"
}

variable "nat_instance_eip_ids" {
  description = <<-EOT
  Allocation IDs of Elastic IPs to associate with the NAT instances. If not specified, EIPs will be created.

  Note: if the number of EIPs does not match the number of subnets specified in `vpc_public_subnet_ids`, this variable will be ignored.
  EOT
  type        = list(string)
  default     = []
}

variable "nat_instance_user_data_post_install" {
  description = "Post-install shell script to run at boot after configuring alternat."
  type        = string
  default     = ""
}

variable "tags" {
  description = "A map of tags to add to all supported resources managed by the module."
  type        = map(string)
  default     = {}
}

variable "vpc_az_maps" {
  description = "A map of az to private route tables that the NAT instances will manage."
  type = list(object({
    az                 = string
    private_subnet_ids = list(string)
    public_subnet_id   = string
    route_table_ids    = list(string)
  }))
}

variable "vpc_id" {
  description = "The ID of the VPC."
  type        = string
}

variable "lambda_package_type" {
  description = "The lambda deployment package type. Valid values are \"Zip\" and \"Image\". Defaults to \"Image\"."
  type        = string
  default     = "Image"
  nullable    = false

  validation {
    condition     = contains(["Zip", "Image"], var.lambda_package_type)
    error_message = "Must be a supported package type: \"Zip\" or \"Image\"."
  }
}

variable "lambda_memory_size" {
  description = "Amount of memory in MB your Lambda Function can use at runtime. Defaults to 256."
  type        = number
  default     = 256
}

variable "lambda_timeout" {
  description = "Amount of time your Lambda Function has to run in seconds. Defaults to 300."
  type        = number
  default     = 300
}

variable "lambda_handlers" {
  description = "Lambda handlers."
  type = object({
    connectivity_tester       = string,
    alternat_autoscaling_hook = string,
  })
  default = {
    connectivity_tester       = "app.connectivity_test_handler",
    alternat_autoscaling_hook = "app.handler"
  }
}

variable "lambda_environment_variables" {
  description = "Environment variables to be provided to the lambda function."
  type        = map(string)
  default     = null
}

variable "lambda_zip_path" {
  description = "The location where the generated zip file should be stored. Required when `lambda_package_type` is \"Zip\"."
  type        = string
  default     = "/tmp/alternat-lambda.zip"
}
variable "nat_allowed_cidrs" {
}
