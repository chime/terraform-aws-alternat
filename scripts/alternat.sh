#!/bin/bash

# Send output to a file and to the console
# Credit to the alestic blog for this one-liner
# https://alestic.com/2010/12/ec2-user-data-output/
exec > >(tee /var/log/user-data.log|logger -t user-data -s 2>/dev/console) 2>&1

shopt -s expand_aliases

panic() {
  [ -n "$1" ] && echo "$1"
  echo "alterNAT setup failed"
  exit 1
}

load_config() {
   if [ -f "$CONFIG_FILE" ]; then
      . "$CONFIG_FILE"
   else
      panic "Config file $CONFIG_FILE not found"
   fi
   validate_var "eip_allocation_ids_csv" "$eip_allocation_ids_csv"
   validate_var "route_table_ids_csv" "$route_table_ids_csv"
}

validate_var() {
   var_name="$1"
   var_val="$2"
   if [ ! "$2" ]; then
      echo "Config var \"$var_name\" is unset"
      exit 1
   fi
}

# configure_nat() sets up Linux to act as a NAT device.
# See https://docs.aws.amazon.com/vpc/latest/userguide/VPC_NAT_Instance.html#NATInstance
configure_nat() {
   echo "Beginning NAT configuration"

   echo "Determining the MAC address on eth0"
   local eth0_mac="$(cat /sys/class/net/eth0/address)" || panic "Unable to determine MAC address on eth0."
   echo "Found MAC $eth0_mac for eth0."

   local vpc_cidr_uri="http://169.254.169.254/latest/meta-data/network/interfaces/macs/$eth0_mac/vpc-ipv4-cidr-block"
   echo "Metadata location for vpc ipv4 range: $vpc_cidr_uri"

   local vpc_cidr_range=$(CURL_WITH_TOKEN "$vpc_cidr_uri")
   if [ $? -ne 0 ]; then
      panic "Unable to obtain VPC CIDR range from metadata."
   else
      echo "Retrieved VPC CIDR range $vpc_cidr_range from metadata."
   fi

   echo "Enabling NAT..."
   # Read more about these settings here: https://www.kernel.org/doc/Documentation/networking/ip-sysctl.txt
   sysctl -q -w net.ipv4.ip_forward=1 net.ipv4.conf.eth0.send_redirects=0 net.ipv4.ip_local_port_range="1024 65535" && (
      iptables -t nat -C POSTROUTING -o eth0 -s "$vpc_cidr_range" -j MASQUERADE 2> /dev/null ||
      iptables -t nat -A POSTROUTING -o eth0 -s "$vpc_cidr_range" -j MASQUERADE ) ||
          panic

   sysctl net.ipv4.ip_forward net.ipv4.conf.eth0.send_redirects net.ipv4.ip_local_port_range
   iptables -n -t nat -L POSTROUTING

   echo "NAT configuration complete"
}

# Disabling source/dest check is what makes a NAT instance a NAT instance.
# See https://docs.aws.amazon.com/vpc/latest/userguide/VPC_NAT_Instance.html#EIP_Disable_SrcDestCheck
disable_source_dest_check() {
   echo "Disabling source/destination check"
   aws ec2 modify-instance-attribute --instance-id $INSTANCE_ID --source-dest-check "{\"Value\": false}"
   if [ $? -ne 0 ]; then
      panic "Unable to disable source/dest check."
   fi
   echo "source/destination check disabled for $INSTANCE_ID"
}

# associate_eip() tries each provided EIP allocation id until it finds one that is not already associated.
function associate_eip() {
   echo "Associating an EIP from the pool of addresses"

   local associated_allocation_id=""
   local eip=""
   local num_retries=10
   local sleep_len=60

   IFS=',' read -r -a eip_allocation_ids <<< "${eip_allocation_ids_csv}"

   # Retry the allocation operation $num_retries times with a $sleep_len wait between retries.
   # This is to handle any delays in releasing an EIP allocation during instance termination.
   for n in $(seq 1 "$num_retries"); do
      for eip_allocation_id in "${eip_allocation_ids[@]}"
      do
         eip=$(aws ec2 describe-addresses --allocation-ids "$eip_allocation_id" --query 'Addresses[0].PublicIp' | tr -d '"')
         echo "Trying IP $eip"
         aws ec2 associate-address --no-allow-reassociation --allocation-id "$eip_allocation_id" --instance-id "$INSTANCE_ID"
         if [ $? -eq 0 ]; then
            break
         fi
         echo "Failed to associate IP $eip"
         eip=""
      done
      if [ ! -z "$eip" ]; then
         break
      else
         echo "Unable to associate an EIP ($n of $num_retries attempts)."
         sleep "$sleep_len"
      fi
   done

   if [ -z "$eip" ]; then
      panic "Unable to associate an EIP!"
   fi

   echo "Associated EIP $eip with instance $INSTANCE_ID";
}

# First try to replace an existing route
# If no route exists already (e.g. first time set up) then create the route.
configure_route_table() {
   echo "Configuring route tables"

   IFS=',' read -r -a route_table_ids <<< "${route_table_ids_csv}"

   for route_table_id in "${route_table_ids[@]}"
   do
      echo "Attempting to find route table $route_table_id"
      local rtb_id=$(aws ec2 describe-route-tables --filters Name=route-table-id,Values=${route_table_id} --query 'RouteTables[0].RouteTableId' | tr -d '"')
      if [ -z "$rtb_id" ]; then
         panic "Unable to find route table $rtb_id"
      fi

      echo "Found route table $rtb_id"
      echo "Replacing route to 0.0.0.0/0 for $rtb_id"
      aws ec2 replace-route --route-table-id "$rtb_id" --instance-id "$INSTANCE_ID" --destination-cidr-block 0.0.0.0/0
      if [ $? -eq 0 ]; then
         echo "Successfully replaced route to 0.0.0.0/0 via instance $INSTANCE_ID for route table $rtb_id"
         continue
      fi

      echo "Unable to replace route. Attempting to create route"
      aws ec2 create-route --route-table-id "$rtb_id" --instance-id "$INSTANCE_ID" --destination-cidr-block 0.0.0.0/0
      if [ $? -eq 0 ]; then
         echo "Successfully created route to 0.0.0.0/0 via instance $INSTANCE_ID for route table $rtb_id"
      else
         panic "Unable to replace or create the route!"
      fi
   done
}

# alterNAT config file containing inputs needed for initialization
CONFIG_FILE="/etc/alternat.conf"

load_config

curl_cmd="curl --silent --fail"

echo "Requesting IMDSv2 token"
token=$($curl_cmd -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 900")
alias CURL_WITH_TOKEN="$curl_cmd -H \"X-aws-ec2-metadata-token: $token\""

# Set CLI Output to text
export AWS_DEFAULT_OUTPUT="text"

# Disable pager output
# https://docs.aws.amazon.com/cli/latest/userguide/cli-usage-pagination.html#cli-usage-pagination-clientside
# This is not needed in aws cli v1 which is installed on the current version of Amazon Linux 2.
# However, it may be needed to prevent breakage if they update to cli v2 in the future.
export AWS_PAGER=""

# Set Instance Identity URI
II_URI="http://169.254.169.254/latest/dynamic/instance-identity/document"

# Retrieve the instance ID
INSTANCE_ID=$(CURL_WITH_TOKEN $II_URI | grep instanceId | awk -F\" '{print $4}')

# Set region of NAT instance
export AWS_DEFAULT_REGION=$(CURL_WITH_TOKEN $II_URI | grep region | awk -F\" '{print $4}')

echo "Beginning self-managed NAT configuration"
configure_nat
disable_source_dest_check
associate_eip
configure_route_table
echo "Configuration completed successfully!"
