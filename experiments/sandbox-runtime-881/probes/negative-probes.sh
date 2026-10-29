#!/usr/bin/env bash
# Safe, read-only probes for spike #881. No exploit code, no escape attempts.
# Prints "probe<TAB>observation" lines.
set -u
p() { printf '%s\t%s\n' "$1" "$2"; }
p kernel_release "$(uname -r)"
p dev_listing "$(ls /dev 2>&1 | tr '\n' ' ')"
p dev_kvm "$(ls -l /dev/kvm 2>&1 | head -1)"
for f in /proc/version /proc/kallsyms /proc/modules; do
  if head -c 1 "$f" >/dev/null 2>&1; then p "readable:$f" yes; else p "readable:$f" no; fi
done
for d in /sys/kernel /sys/firmware; do
  p "entries:$d" "$(ls "$d" 2>/dev/null | wc -l)"
done
p mountinfo_lines "$(wc -l </proc/self/mountinfo)"
p pid1_cgroup "$(head -1 /proc/1/cgroup 2>&1)"
p cpu_vmx_svm "$(grep -cE 'vmx|svm' /proc/cpuinfo 2>/dev/null || echo 0)"
