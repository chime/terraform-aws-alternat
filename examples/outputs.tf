output "vpc_id" {
  description = "VPC ID"
  value       = module.vpc.vpc_id
}

output "nat_instance_security_group_id" {
  description = "NAT Instance Security Group ID"
  value       = module.alternat.nat_instance_security_group_id
}
