data "aws_availability_zones" "available" {}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 4"

  name               = var.vpc_name
  cidr               = var.vpc_cidr
  private_subnets    = var.private_subnets
  public_subnets     = var.public_subnets
  azs                = local.azs
  enable_nat_gateway = var.enable_nat_gateway
}

data "aws_subnet" "subnet" {
  count = length(module.vpc.private_subnets)
  id    = module.vpc.private_subnets[count.index]
}

locals {
  vpc_az_maps = [
    for index, rt in module.vpc.private_route_table_ids
    : {
      az                 = data.aws_subnet.subnet[index].availability_zone
      route_table_ids    = [rt]
      public_subnet_id   = module.vpc.public_subnets[index]
      private_subnet_ids = [module.vpc.private_subnets[index]]
    }
  ]
}

module "alternat" {
  # To use Alternat from the Terraform Registry:
  # source = "chime/alternat/aws"
  source = "./.."

  create_nat_gateways                = var.create_nat_gateways
  ingress_security_group_cidr_blocks = var.private_subnets
  vpc_az_maps                        = local.vpc_az_maps
  vpc_id                             = module.vpc.vpc_id

  lambda_package_type = "Zip"

  nat_instance_type     = var.alternat_instance_type
  nat_instance_key_name = var.nat_instance_key_name
}
