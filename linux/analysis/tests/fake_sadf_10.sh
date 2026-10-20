#!/bin/bash
# Behaves like sysstat 10.1.5 for the arguments perfdiag and gather use.
args="$*"
case "$args" in
    *"-V"*)        echo "sysstat version 10.1.5"; exit 0 ;;
    *"-- -r ALL"*) echo "Usage: sadf [ options ] [ <interval> [ <count> ] ] [ <datafile> ]" >&2; exit 1 ;;
    *"-- -r")
        printf '# hostname;interval;timestamp;kbmemfree;kbmemused;%%memused;kbbuffers;kbcached;kbcommit;%%commit;kbactive;kbinact;kbdirty\n'
        printf 'h;10;1792163610;100000;900000;90.00;10000;400000;500000;50.00;300000;200000;1000\n'
        exit 0 ;;
    *)             echo "Requested activities not available in file" >&2; exit 1 ;;
esac
