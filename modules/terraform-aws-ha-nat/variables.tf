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

variable "ha_nat_image_tag" {
  description = "The tag of the container image for the HA NAT Lambda functions."
  type        = string
  default     = "latest"
}

variable "ha_nat_image_uri" {
  description = "The URI of the container image for the HA NAT Lambda functions."
  type        = string
}

variable "ingress_security_group_ids" {
  description = "A list of security group IDs that are allowed by the NAT instance."
  type        = list(string)
}

variable "max_instance_lifetime" {
  description = "Max instance life in seconds. Defaults to 14 days. Set to 0 to disable."
  type        = number
  default     = 1209600
}

variable "nat_instance_type" {
  description = "Instance type to use for NAT instances."
  type        = string
  default     = "c6gn.8xlarge"
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
