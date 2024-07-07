#!/bin/bash

### Print help messge
usage() {
    echo "Usage: $0 [-p </path/to/patchmanagement.py>] [-c </path/to/config/file>]" 1>&2
    exit 1
}

SCRIPTPATH="$( cd -- "$(dirname "$0")" >/dev/null 2>&1 || exit; pwd -P )"

### Read command arguments
while getopts "p:c:" ARG; do
    case $ARG in
    p)
        PATCH_SCRIPT=$OPTARG
        ;;
    c)
        CONFIG=$OPTARG
        ;;
    *)
        usage
        ;;
    esac
done

### Check if commandline arguments have been set
#!  If not, set the defaults
if [[ -z $PATCH_SCRIPT ]]; then
    PATCH_SCRIPT="${SCRIPTPATH}/patchmanagement.py"
fi

if [[ -z $PATCH_SCRIPT ]]; then
    CONFIG="${SCRIPTPATH}/.env"
fi

### Export the environment variables
export $(xargs -d '\n' < "$CONFIG")

### Execute patchmanagemnet
python3 -W ignore "${PATCH_SCRIPT}"
