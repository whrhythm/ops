#!/bin/sh
set -eu
cd /tmp/starbridge-v1-01a-codeapi
docker compose --env-file .env -f service/src/office-recovery.compose.yaml up -d redis minio files tools egress
# The microVM does not inherit Docker's embedded DNS. Resolve only this test
# service after creation; production Kubernetes service discovery is unchanged.
office_egress_ip=$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' starbridge-v101a-egress-1)
export OFFICE_TEST_EGRESS_URL="http://${office_egress_ip}:3190"
export OFFICE_TEST_FORWARD_TARGET="${office_egress_ip}:3190"
docker compose --env-file .env -f service/src/office-recovery.compose.yaml up -d api worker runner dispatch
