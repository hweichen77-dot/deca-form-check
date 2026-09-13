#!/bin/bash
set -e
cd "$(dirname "$0")"
out=$(mktemp -d)
uv run deca_check.py regress --quiet --csv "$out/verdicts.csv" --out "$out/sorted" --manifest /nonexistent | tail -1
if diff <(cut -d, -f1,4 regress/expected.csv | sort) <(cut -d, -f1,4 "$out/verdicts.csv" | sort); then
    echo "regress OK"
else
    echo "regress CHANGED, see diff above (expected < > got)"
    exit 1
fi
