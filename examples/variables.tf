variable "alternat_instance_type" {
  description = "The instance type to use for the Alternat instances."
  type        = string
  default     = "t4g.medium"
}

variable "aws_region" {
  description = "The AWS region to deploy to."
  type        = string
  default     = "us-west-2"
}

variable "create_nat_gateways" {
  description = "Whether to create NAT Gateways using the Alternat module."
  type        = bool
  default     = true
}

variable "enable_nat_gateway" {
  description = "Whether to create NAT Gateways using the VPC module."
  type        = bool
  default     = false
}

variable "enable_nat_restore" {
  description = "Whether to enable NAT restore."
  type        = bool
  default     = true
}

variable "enable_ssm" {
  description = "Whether to enable SSM."
  type        = bool
  default     = true
}

variable "nat_instance_key_name" {
  description = "The name of the key pair to use for the NAT instances."
  type        = string
  default     = ""
}

variable "private_subnets" {
  description = "List of private subnets to use in the example VPC."
  type        = list(string)
  default     = ["10.10.20.0/24", "10.10.21.0/24"]
}

variable "public_subnets" {
  description = "List of public subnets to use in the example VPC. Alternat instnaces and NAT Gateways reside in these subnets."
  type        = list(string)
  default     = ["10.10.0.0/24", "10.10.1.0/24"]
}

variable "vpc_cidr" {
  description = "The CIDR block to use for the example VPC."
  type        = string
  default     = "10.10.0.0/16"
}

variable "vpc_secondary_subnets" {
  description = "List of private subnets in the secondary cidr space."
  type        = list(string)
  default     = ["10.20.20.0/24", "10.20.21.0/24"]
}

variable "vpc_secondary_cidr" {
  description = "A secondary CIDR block to use with the example VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "vpc_name" {
  description = "The name to use for the example VPC."
  type        = string
  default     = "alternat-example"
}

variable "enable_cloudwatch_agent" {
  description = "Whether to enable CloudWatch Agent on the NAT instances."
  type        = bool
  default     = true
}
