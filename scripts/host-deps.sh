#!/usr/bin/env bash
# Install Yocto (scarthgap) build host dependencies on Debian/Ubuntu.
# Run once inside the OrbStack Debian machine:  ./scripts/host-deps.sh
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

$SUDO apt-get update
$SUDO apt-get install -y --no-install-recommends \
    gawk wget git diffstat unzip texinfo gcc g++ build-essential chrpath socat \
    cpio python3 python3-pip python3-pexpect python3-git python3-jinja2 \
    python3-subunit xz-utils debianutils iputils-ping zstd lz4 file locales \
    libacl1 bzip2 ca-certificates mesa-common-dev

# Yocto requires a UTF-8 locale.
$SUDO sed -i 's/^# *\(en_US.UTF-8 UTF-8\)/\1/' /etc/locale.gen
$SUDO locale-gen

# OrbStack ships git with core.ignorecase=true by default. The filesystem is
# actually case-sensitive, and kernel-yocto's `git add` of the Linux source
# trips over case-only-differing headers (e.g. xt_connmark.h vs xt_CONNMARK.h)
# when ignorecase is on, producing an empty initial commit and a wiped source
# tree. Force it off for correct kernel checkout.
git config --global core.ignorecase false

# OrbStack also enables commit.gpgsign=true globally. kernel-yocto makes an
# internal "git commit" of the unpacked Linux source; with signing on and no
# gpg installed, the commit fails ("cannot run gpg" -> "failed to write commit
# object"), leaving an invalid HEAD and breaking do_patch. Disable signing for
# these throwaway build-internal commits.
git config --global commit.gpgsign false
echo
echo "Host deps installed. Set 'export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8' in your shell."
