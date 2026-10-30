#!/bin/sh
tok="$1"; shift
H=$(mktemp -d)
env -i PATH=/usr/bin:/bin HOME=$H COPILOT_HOME=$H/ch COPILOT_GITHUB_TOKEN="$tok" HTTPS_PROXY=http://127.0.0.1:18899 NODE_EXTRA_CA_CERTS=/tmp/cop-probe/ca/ca.pem timeout 60 /tmp/cop-probe/inst/node_modules/@github/copilot-linux-x64/copilot -p "say hi" --model gpt-5 -s --no-ask-user --log-level debug --log-dir $H/logs "$@" </dev/null >$H/out 2>$H/err
echo "exit=$?"; head -c 500 $H/out; head -c 500 $H/err; echo "logs=$H/logs"
