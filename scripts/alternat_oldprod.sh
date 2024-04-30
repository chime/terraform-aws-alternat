#!/usr/bin/env bash
USERDATA_CONFIG_FILE="/etc/alternat.conf"
echo eip_allocation_ids_csv=eipalloc-05bed52af1132c4e5,eipalloc-0f766b3862a50a161,eipalloc-0597cfcaeac4a5a8f >> "$USERDATA_CONFIG_FILE"
echo route_table_ids_csv=	rtb-f1de2795 >> "$USERDATA_CONFIG_FILE"



