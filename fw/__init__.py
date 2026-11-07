"""Custom bootloader + application firmware for the HP DeskJet 2620 (fw/).

Python side: image format (fw.image), toolchain driver (fw.build), XMODEM
sender (fw.xmodem), emulator harness (fw.harness) and host serial tool
(fw.tool). C sources live in fw/common, fw/bl and fw/app.
"""
