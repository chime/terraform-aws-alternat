variable "additional_instance_policies" {
  description = "Additional policies for the HA NAT instance IAM role."
  type = list(object({
    policy_name = string
    policy_json = string
  }))
  default = []
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

variable "alternat_image_tag" {
  description = "The tag of the container image for the HA NAT Lambda functions."
  type        = string
  default     = "latest"
}

variable "alternat_image_uri" {
  description = "The URI of the container image for the HA NAT Lambda functions."
  type        = string
}

variable "ingress_security_group_ids" {
  description = "A list of security group IDs that are allowed by the NAT instance."
  type        = list(string)
  default     = []
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
  description = "Name ot use for the IAM role used by the replace-route Lambda function. Must be globally unique in this AWS account."
  type        = string
  default     = ""
}

variable "nat_instance_type" {
  description = "Instance type to use for NAT instances."
  type        = string
  default     = "c6gn.8xlarge"
}

variable "nat_instance_eip_ids" {
  description = "Allocation IDs of Elastic IPs to associate with the NAT instances. If not specified, EIPs will be created."
  type        = list(string)
  default     = []
}

variable "subnet_suffix" {
  description = "Suffix in the NAT private subnet name to search for when updating routes via HA NAT Lambda functions."
  type        = string
  default     = "private"
}

variable "private_route_table_ids" {
  description = "A list of private route tables that the NAT instances will manage."
  type        = list(string)
}

variable "tags" {
  description = "A map of tags to add to all supported resources managed by the module."
  type        = map(string)
  default     = {}
}

variable "vpc_id" {
  description = "The ID of the VPC."
  type        = string
}

variable "vpc_private_subnet_ids" {
  description = "A list of private subnets IDs inside the VPC."
  type        = list(any)
}

variable "vpc_public_subnet_ids" {
  description = "A list of public subnets IDs inside the VPC."
  type        = list(any)
}
