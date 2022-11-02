output "nat_instance_eips" {
  description = "List of Elastic IP addresses used by the NAT instances."
  value       = aws_eip.nat_instance_eips[*].public_ip
}

output "nat_gateway_eips" {
  description = "List of Elastic IP addresses used by the standby NAT gateways."
  value       = aws_eip.nat_gateway_eips[*].public_ip
}
